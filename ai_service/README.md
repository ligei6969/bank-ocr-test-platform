# AI 复核助手服务（ai_service）

审核平台的 **AI 能力进程**。给审核员解释「这条记录为什么是这个结论、根因在哪一层、接下来该做什么」。

它是独立进程，平台通过 HTTP 调用：**AI 服务崩了、超时了，审核主链路照常运行**。

---

## 一句话看懂它做了什么

平台现在返回的是原因码：

```json
{"review_result": "review", "review_reasons": ["missing_valid_date", "image_blur"]}
```

审核员看到 `image_blur` 得自己翻文档才知道这意味着什么、该让用户怎么重拍。
本服务把这条记录变成：

> 本次银行卡审核的结论是「待人工复核」。共命中 2 个原因码，根因涉及字段解析层、影像质量层。
> `missing_valid_date`：有效期用于判断卡是否过期，缺失时无法自动放行。
> `image_blur`：模糊会直接导致 OCR 识别错误……
> **处置方向**：质量原因码与字段原因码同时出现，字段缺失大概率是影像质量的下游后果 ——
> 先修影像质量，不必单独追问用户字段内容。

同时给出：原因码逐条释义（含**真实阈值**与实现位置）、处置建议、知识引用、置信度。

---

## 快速开始

```bash
# 1. 启动 AI 服务（默认 127.0.0.1:8100）
python -m ai_service

# 2. 自检：不联网、不需要 API key，跑一次完整链路
python -m ai_service --demo

# 3. 单独看检索效果
python -m ai_service --search "反光了怎么办"

# 4. 用真实记录跑（平台侧导出的 JSON）
python -m ai_service --explain payload.json
```

平台侧不需要任何额外操作，默认就连 `http://127.0.0.1:8100`。

---

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 探活，报告 LLM 是否可用、索引规模 |
| GET | `/tools/stats` | 工具的成功率、平均延迟、熔断状态 |
| POST | `/explain` | 主接口：传入脱敏后的审核上下文，返回解释 |
| POST | `/search` | 只做检索，调试召回质量用 |

`POST /explain` 请求体：

```json
{
  "request_id": "3f9a1c8e",
  "doc_type": "bank_card",
  "review_result": "review",
  "quality_result": "review",
  "quality_reasons": ["image_blur"],
  "review_reasons": ["missing_valid_date", "image_blur"],
  "fields": {"card_number": "622202******7890", "name": "张*"},
  "question": "这张卡为什么需要人工复核？该怎么让用户重拍？",
  "top_k": 5
}
```

`fields` 由平台侧脱敏后传入，本服务不接触原始证件号。

---

## 环境变量

全部可选。**一个都不配也能跑**。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AI_SERVICE_HOST` | `127.0.0.1` | 监听地址 |
| `AI_SERVICE_PORT` | `8100` | 监听端口 |
| `LLM_PROVIDER` | `openai` | `openai` / `anthropic` / `none` |
| `LLM_API_KEY` | 空 | 也接受 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |
| `LLM_BASE_URL` | 官方地址 | 中转站 / 自建网关改这里 |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `LLM_TIMEOUT_S` | `20` | 单次模型调用超时 |
| `LLM_MAX_RETRIES` | `0` | 重试次数，保持测试确定性所以默认 0 |

**没配 key 会发生什么**：不是在报错，而是自动切换到确定性策略 ——
查询改写用规则同义词扩展、重排用共识度排序、措辞用模板拼装。
**事实内容完全不受影响**（见下节）。返回体里 `degraded` 为 `true`，前端会明确标注。

---

## 核心设计：事实与措辞分离

这是本服务最重要的一个决定。

```
        ┌─────────────────────────────────────────────┐
        │  事实层  reason_details                     │
        │  ├ 原因码释义、触发条件                      │
        │  ├ 真实阈值与实现位置                        │
        │  └ 处置建议、用户话术                        │
        │  ← 全部来自 corpus.py，不经过 LLM            │
        └─────────────────────────────────────────────┘
                            +
        ┌─────────────────────────────────────────────┐
        │  措辞层  explanation                        │
        │  ← 有 LLM 时由 LLM 写，没有时由模板拼装      │
        └─────────────────────────────────────────────┘
```

**为什么这样切**：很多「LLM 降级」实现会把整段功能一起砍掉，AI 一挂就什么都没了。
这里降级只损失表达质量，不损失事实准确性 ——
没配模型时，`image_blur` 的阈值依然是「拉普拉斯方差小于 80.0」，
处置建议依然是完整可用的。测试 `test_llm_path_still_returns_corpus_facts_unchanged`
就是用来钉死这个承诺的。

---

## 检索链路

从 EchoMind 移植的完整链路，每一步都能降级：

```
查询（含原因码字面量）
  ├─ ① 查询改写    LLM 生成 3 个角度子查询   / 降级：规则同义词扩展
  ├─ ② 并行召回    多路子查询同时检索        / asyncio.gather
  ├─ ③ 合并去重    按 doc_id 归并，统计共识度
  └─ ④ 重排        LLM 打相关性分           / 降级：共识度排序
```

单次召回的打分是三路混合：

```
最终分 = 0.65 × BM25 归一化分 + 0.35 × 哈希向量余弦 + 原因码精确命中加分
```

第三项是本域特有的：**审核上下文里本来就带着原因码字面量**（`image_blur` 等），
这是最可靠的检索信号，向量检索反而会把它糊掉。

### 为什么不用 ChromaDB + 小模型 embedding

这是有意做的取舍，不是偷懒：

1. **语料规模**：审核域全部知识只有几十条文档片段。这个量级上 BM25 精度不输小模型向量，
   且召回可解释（能说清是哪个词命中的）。
2. **确定性**：CI 与单测需要可重复结果。向量检索依赖模型下载（`all-MiniLM-L6-v2` 约 90MB），
   离线环境直接失败。
3. **依赖面**：引入 chromadb 会拖进 onnxruntime 等一条重依赖链，
   而平台侧「装完就能跑 mock 测试」的轻量优势要保住。

需要换向量后端时，实现 `retrieval.VectorBackend` 协议（`embed` + `name`）注入即可。

---

## 知识库语料

`corpus.py` 是唯一事实来源，四类：

| 类别 | 内容 |
| --- | --- |
| `reason_code` | 全部 22 个原因码释义，含真实阈值与实现位置 |
| `capture_guide` | 拍摄规范，对应用户端 `user_home.html` 的三条提示 |
| `review_rule` | 审核判定顺序、质量码与字段码的归因区别 |
| `case` | 典型复核案例（**合成示例**，非真实客户数据） |

**阈值不靠人工同步。** `tests/test_ai_corpus_consistency.py` 会：
- 驱动平台真实的规则函数，收集它能产出的**全部**原因码，断言语料全覆盖；
- 从 `quality_check.py` / `rule_check.py` 的**源码里提取阈值字面量**，断言语料中出现；
- 反向断言语料里没有平台不会产出的过时原因码。

改了阈值但没改语料，测试直接红。

---

## 与 EchoMind 的关系

`retrieval.py` 和 `tool_manager.py` 移植自 EchoMind
（`mcp/knowledge_base.py`、`mcp/tool_manager.py`），保留其全部核心设计：
查询改写 / 并行召回 / 哈希去重 / LLM 重排 / 三态熔断 / TTL 缓存 / 超时 / 降级。

有意做的三处改动：

1. **LLM 全程可缺席**：EchoMind 直接依赖 `AsyncAnthropic`，
   这里改为注入 `LLMClient` 协议，每一步都能降级。
2. **去重键改为 `doc_id`**：语义比整条结果哈希更准，且能统计共识度。
3. **新增 `trace`**：记录每一步用的是 LLM 还是降级策略，回传前端与日志，
   避免降级被静默吞掉。

---

## 目录

```
ai_service/
  api.py          FastAPI 接口
  explain.py      业务编排：上下文 → 检索 → 事实 → 措辞
  corpus.py       知识库语料（唯一事实来源）
  retrieval.py    切片 / 分词 / BM25 + 哈希向量混合检索
  tool_manager.py 工具框架：改写、并行召回、去重、重排、熔断、缓存、降级
  llm.py          可插拔 LLM 适配层（openai / anthropic / none）
  __main__.py     CLI：起服务、--demo、--search、--explain
  tests/          75 个单测，全部离线可跑
```

---

## 已知边界（P0 范围外）

- **单轮解释**，不支持多轮追问 —— 需要会话记忆，属 P4。
- **解释不落库** —— 避免「解释版本与记录版本不一致」，落库留到 P2 与 `llm_override` 一起做。
- **不做双判** —— 本服务只解释不改判，规则 + LLM 双判属 P2。
- **置信度是启发式** —— 由知识覆盖率、检索最高分、生成引擎三因子拼出来的，
  没有做概率校准。展示时应当配合解释正文，不要单独当作准确性背书。
