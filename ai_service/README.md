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
  api.py          FastAPI 接口（/explain、/agent/explain、/search、/health、/tools/stats）
  explain.py      业务编排：上下文 → 检索 → 事实 → 措辞（P0 固定流水线）
  agent.py        P1 审核 Agent：planner + executor 的多步工具决策循环
  tools.py        P1 工具白名单与四个工具的实现
  thresholds.py   影像质检阈值的机器可读副本（供 recompute_quality 反推）
  corpus.py       知识库语料（唯一事实来源）
  retrieval.py    切片 / 分词 / BM25 + 哈希向量混合检索
  tool_manager.py 工具框架：改写、并行召回、去重、重排、熔断、缓存、降级
  llm.py          可插拔 LLM 适配层（openai / anthropic / none）
  prompts.py      Prompt 版本化注册表（id@version，进 trace 与响应）
  structured.py   结构化输出解析与校验（pydantic，失败抛可捕获异常）
  cassette.py     LLM / 工具的录制回放（replay 未命中直接失败，绝不回退联网）
  agentkit.py     测试工具箱：轨迹断言、故障注入、cassette 装配
  eval/           评测层：golden 集、四层指标、judge 校准、回归门禁
  __main__.py     CLI：起服务、--demo、--agent、--live、--search、--explain
  tests/          260 个单测，全部离线可跑
```

---

## P1：从解释器到 Agent

### 为什么要做这一步

P0 交付的是**固定流水线的 RAG**：改写 → 召回 → 重排 → 生成，每条记录都走这四步。
能回答「这条记录为什么是这个结论」，但回答不了「你是怎么做决策的」。
P1 把它变成**会调工具、多步决策**的 Agent，并配上一整套「怎么测一个不确定系统」的答案。

```
记录 + 问题
    │
    ▼   ┌────────────────────────────────────────────┐
        │ 1. planner：决定下一步（LLM 结构化输出）    │
        │ 2. 白名单校验 → schema 校验 → 拒绝 / 放行    │
        │ 3. executor：调工具，拿观察结果              │
        │ 4. 预算检查（步数 / token / 工具调用次数）   │
        │ 5. 收敛判定：finish / escalate / 超预算      │
        └────────────────────────────────────────────┘
    │
    ▼
结构化答复 + 完整 trace（工具序列 / 参数 / 步数 / token / 耗时）
```

`POST /explain`（P0 流水线）与 `POST /agent/explain`（P1 Agent）**并存**，不是替换：
简单记录用前者更快更省，需要「自己决定查什么」时才走 Agent。

### 工具白名单

Agent 只能调用下面四个工具。名字不在白名单里一律拒绝，并留下拒绝记录。

| 工具 | 作用 | 降级行为 |
| --- | --- | --- |
| `search_knowledge` | 查原因码释义 / 拍摄规范 / 审核规则 | 退回原因码直查表（释义本来就在结构化表里，不依赖检索） |
| `get_review_record` | 按 request_id 取**本次**记录 | 换别的 id 一律 `not_found`（越权防线） |
| `recompute_quality` | 用线上阈值核对影像质量判定 | 拿不到原始指标时只做自洽性核对，并在输出里标注 `mode` |
| `escalate_to_human` | 证据不足时主动请求人工 | 本身就是兜底；**无副作用**，不写库、不改判 |

两条设计约束值得单独说：

**白名单是硬的，不是 prompt 里的请求。** prompt 写「你只能用这些工具」是软约束，
模型可以不听；白名单不听就执行不了。参数校验放在**调用前**，参数不合法连工具都不碰。

**工具全都不产生副作用。** 四个工具全是只读或纯计算 —— 这既是安全边界，
也让「非法调用被拒绝且无副作用」这条验收标准天然成立。改判审核结论属 P2。

### 三重预算与收敛

步数、token、工具调用次数任一超限即收敛，`truncated=true` 照常返回已有结果，
**不抛异常**。Agent 超预算是正常结局，不是故障。

还有两条反打转机制：连续被拒 3 次直接收敛；同一「工具 + 参数」重复超过 2 次
判定为原地打转并收敛。不设这两条的话，一个执着的模型能把预算烧干净。

一个细节：**「证据不足必须转人工」是规则，不是模型的判断。**
早期实现只在降级路径检查它，结果是模型（或被 prompt injection 影响的模型）
回一句 `finish` 就把规则跳过去了。现在两条路径在收敛前都必须过这道规则。
唯一例外是预算耗尽 —— 那时不再增加步骤，只如实标记 `truncated=true`，
因为「超了就停」要是能被规则打破，预算就不可信了。

### 每一步都能降级

LLM 不可用、或决策输出解析不了、或工具挂了 —— 三种情况都不会让链路中断：

| 失败点 | 行为 |
| --- | --- |
| LLM 不可用 | 走**确定性工具序列**（读记录 → 查知识 → 核质检 → 需要时举手），产出结构完全一致的结果 |
| 决策输出不是合法 JSON / 超长 / 缺字段 | 记录 `decision_failed`，同样切到确定性序列 |
| 工具超时 / 报错 | 工具层兜住，退化为 `ToolResult(success=False)`，Agent 继续走；检索挂了还会退回原因码直查 |
| 工具返回结构不对 | 不吸收为证据；随后「证据不足」规则接管，如实转人工 |

### Prompt 版本化与结构化输出

Prompt 从内联字符串抽成 `prompts.py` 里的 `PromptTemplate`，每条有稳定 `id` 和手写
`version`，`label`（形如 `agent_decide@v1`）会写进 trace 与响应 ——
「这条解释是哪版 prompt 产出的」在数据里就能查到。

版本号是手写的而不是内容哈希：哈希区分不了「改了错别字」和「改了语义」，
而评审时真正关心的是后者。

`structured.py` 负责把模型输出变成经过校验的对象，处理三类真实会遇到的输入：
Markdown 围栏、JSON 前后夹着寒暄、以及**结构合法但语义非法**（缺字段、类型不对）。
解析失败一律抛 `StructuredOutputError`，绝不返回 `None` 让调用方自己猜 ——
静默返回 `None` 会把降级路径吞掉，这个坑 P0 已经踩过一次。

---

## Agent 测试体系

这是 P1 真正的交付重点：**怎么测量一个不确定、会调工具、会多步决策的系统。**

### record / replay（cassette）

```python
from ai_service.agentkit import cassette_agent, LIVE, REPLAY

agent, cassette = cassette_agent("cassette.json", mode=LIVE)   # 录
await agent.run(context)
cassette.save()

agent, _ = cassette_agent("cassette.json", mode=REPLAY)        # 放
await agent.run(context)                                       # 不需要 API key
```

关键契约：**replay 未命中时抛 `CassetteMissError`，绝不回退到真实调用。**
一个「未命中就顺手联网」的回放层比没有回放层更危险 —— CI 依然是随机且烧钱的，
只是更难发现。

`CassetteMissError` 刻意派生自 `BaseException` 而不是 `Exception`：
工具层有一圈 `except Exception`，如果它是普通异常，会被安静吃掉 ——
Agent 退化成「工具坏了」继续跑完，测试**绿着骗人**。派生自 `BaseException`
和 `KeyboardInterrupt` 同一个思路：这不是正常业务流程里该被兜住的东西。

按「请求内容哈希」而不是「第 N 次调用」匹配：顺序匹配在 Agent 场景很脆弱，
模型多调一次工具后面全部错位，报出来的错还很难懂。

### 轨迹断言

断言的是 `outcome["trace"]`，不是 `outcome["answer"]` —— Agent 的失败方式往往是
**输出看起来对、过程是错的**：恰好蒙对结论、绕了十步、或者重复调同一个工具。

```python
from ai_service.agentkit import Trajectory

view = Trajectory(outcome.to_dict())
view.assert_sequence("get_review_record", "search_knowledge")
view.assert_called("get_review_record", request_id="req-001")
view.assert_within_budget().assert_no_infinite_loop()
view.assert_rejected("not_whitelisted")
view.assert_every_step_has_audit_fields()
```

### 故障注入与对抗用例

```python
from ai_service.agentkit import inject_fault, inject_all_faults, RAISE, TIMEOUT, BAD_PAYLOAD

inject_fault(agent, "search_knowledge", TIMEOUT, sleep_s=0.4, timeout_s=0.05)
```

五种故障模式：`RAISE` / `HTTP_500` / `TIMEOUT` / `BAD_PAYLOAD` / `EMPTY`。
注意后两种在工具层看起来是**成功**的（返回了东西，只是结构不对），
比抛错更阴险 —— 它们容易一路带进摘要、带进 prompt。

对抗用例覆盖三类，主要防线是**结构性**的而不是 prompt 里的「请勿听信」：

* **审核结论不是模型产出的** —— `review_result` 从入参原样带出，模型说什么都改不了它；
* **事实层不经过模型** —— 阈值与处置建议来自语料 + 规则，injection 改不动；
* **工具白名单** —— injection 无法让 Agent 去调一个没注册的工具。

残留风险写进了测试注释而不是假装不存在：模型仍能影响**措辞**。
这正是 P2 要做「规则 + LLM 双判 + `llm_override` 落库」的原因 ——
改判必须显式、可追溯、可回滚，而不是靠模型自觉。

---

## 评测与回归门禁

```bash
python -m scripts.evaluate_ai_review                    # 四层指标 + baseline 比对
python -m scripts.evaluate_ai_review --calibrate        # 额外跑 judge 校准
python -m scripts.evaluate_ai_review --save-baseline    # 把当前指标写成新基线
python -m scripts.evaluate_ai_review --allure           # 写 Allure 结果，复用平台报告栈
python -m scripts.evaluate_ai_review --live             # 真实模型参与（会出网）
```

默认**全离线**：Agent 走确定性序列、judge 走确定性 rubric，CI 不需要任何 API key。
指标退化超过容忍度（默认 5%）时命令返回 1，直接把 CI 打红。

### golden 集从哪来，以及为什么决策层算不了

任务书要求先确认 `data/annotations/labels.json` 标的是什么。结论是：

**它标的是字段真值 + 注入的质量退化类型，不含审核结论。** 2134 条样本里
没有 `pass/review/reject`，也没有原因码标签。这直接决定了两件事：

* **能算**：期望的**质量原因码**可由 `quality_type` 按已文档化的映射推出，
  所以「原因码集合匹配率」是可信的；
* **不能算**：期望的**审核结论**推不出来（结论取决于质量判定与字段解析的联合结果，
  而字段层没有结论标注）。所以任务层的「结论正确率」只能退化成
  「原因码集合匹配率」这个**代理指标**，报告里显式标为 proxy。

与其编一份假标注，不如把缺口写进报告。要算真正的结论层指标，必须先补人工结论标注 ——
这是 P2 的输入。

两个取样上的坑也记在这：`occlusion` / `rotate` 这类退化平台并不检测（没有对应阈值），
影响落在字段层，所以不进 golden 集；`id_card_front` / `id_card_back` 必须归一到
`id_card`、`application_form` 必须排除，否则会喂给服务一个它从没见过的 doc_type，
检索的 doc_type 过滤全部落空，指标会莫名其妙地低还找不出原因。

### LLM-as-Judge 必须校准

Judge 本身也会飘（系统性偏高、长度偏好），所以不能盲信分数。校准输出三个数字：
完全一致率、±1 分内一致率、平均绝对误差，并**按维度分解**。
主指标是 ±1 分内 —— 评审里「4 分还是 5 分」常常无差别，但「1 分还是 4 分」有区别。

当前校准集的标注是**项目作者自评的占位标注**，不是审核员标注：
它能验证校准管线跑得通、能暴露 judge 的系统性偏差方向，但**不具备统计意义**。
替换成真实审核员标注后，一致率数字才有对外引用价值。这一点在报告里也会标明。

---

## 已知边界（P1 之后仍未做的）

- **不做双判** —— 本服务只解释不改判，规则 + LLM 双判属 P2，
  且必须与 `llm_override` 落库一起做（改判不落库就无法评估 AI 是帮忙还是添乱）。
- **解释不落库** —— 避免「解释版本与记录不一致」，落库同样留到 P2。
- **单轮内多步决策，不做跨记录挖掘** —— Agent 只能读本次请求那一条记录。
  跨进程直连平台数据库会同时破坏服务边界、安全边界与部署边界，
  代价不划算；多轮会话记忆属 P4。
- **决策层指标缺失** —— 缺人工结论标注，任务层目前用「原因码集合匹配率」代理。
  这是数据缺口，不是代码缺口，见上文「golden 集从哪来」。
- **judge 校准集是占位标注** —— 管线可用，数字暂无统计意义。
- **Agent 未接进管理端 UI** —— `/agent/explain` 已可用，但前端仍渲染 P0 的解释结构。
  接进去要新增一个 trace / 预算面板，留给后续。
- **置信度是启发式** —— 由知识覆盖率、检索最高分、生成引擎三因子拼出来的，
  没有做概率校准。展示时应当配合解释正文，不要单独当作准确性背书。
- **token 计量是字符估算** —— `LLMClient` 协议只返回文本，拿不到 provider 的 usage。
  它用于预算兜底，不是计费依据。
