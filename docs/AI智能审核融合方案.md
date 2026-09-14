# EchoMind × 银行 OCR 测试平台 —— AI 智能审核融合方案

> 目标：把 EchoMind 的多 Agent 编排、RAG 检索、记忆、监控和评测能力，接到现有 `bank-ocr-test-platform` 上，
> 让平台从「影像审核工具」升级成「**带 AI 复核与质量保障体系的智能审核平台**」。
>
> 本方案的硬约束：**平台现有 136 个测试必须全绿**，主审核链路不允许被 AI 拖慢或拖垮。

---

## 一、先想清楚：为什么要融合

光看两个项目，很容易得出「把 EchoMind 塞进平台」这种结论。但先把双方的资产摊开看，才知道哪些能接、哪些接了是浪费。

### 平台现有能力（已经很扎实）

| 模块 | 职责 | 状态 |
| --- | --- | --- |
| `app/main.py` | `/bank-card/review`、`/id-card/review` 两条审核主链路 | 串行流水线，写死 |
| `app/quality_check.py` | 模糊 / 过暗 / 过亮 / 反光检测 | 规则算法，毫秒级 |
| `app/ocr_service.py` | mock / PaddleOCR 双模式 | 已解耦，可切换 |
| `app/field_parser.py` `id_card_parser.py` | 字段解析、身份证正反面判断 | 规则 + 正则 |
| `app/rule_check.py` | 审核结论判定 | 规则 |
| `app/review_records.py` | SQLite 审核记录 + 查询 | 已闭环 |
| `app/auth_routes.py` `csrf.py` | 鉴权、CSRF | 完整 |
| `tests/` | 136 个测试 + GitHub Actions | 已冻结 |
| `scripts/evaluate_bank_card_ocr.py` | OCR 字符级准确率评估 | 已有基线 |

### 平台当前的三个真实痛点

1. **原因码不可解释。** 审核结果返回 `review_reasons: ["image_blur", "missing_card_number"]`，
   审核员要自己去翻文档才知道「这意味着什么、我该让用户怎么重拍」。
2. **规则判定是硬阈值。** 模糊/反光用阈值卡，边界样本误报率高——本该 `pass` 的被判 `review`，
   人工复核队列被灌水。
3. **评测只覆盖 OCR 层。** `evaluate_bank_card_ocr.py` 只看字段识别的字符准确率，
   对「审核结论对不对」「解释文本有没有用」完全没有度量。

### EchoMind 能补上的资产

| EchoMind 模块 | 可迁移能力 | 正好解决 |
| --- | --- | --- |
| `mcp/knowledge_base.py` | ChromaDB 文档切片 + 语义检索 | 痛点 1 |
| `mcp/tool_manager.py` | 查询改写 → 并行召回 → LLM 重排 → 熔断 → TTL 缓存 → 降级 | 痛点 1 的检索质量 |
| `agents/agent_orchestrator.py` | 意图映射路由 / 性能路由 / 降级 / 并行协作 | 痛点 2 |
| `evaluation/evaluator.py` | LLM-as-Judge 四维打分 + 基线回归检测 | 痛点 3 |
| `monitor/performance_monitor.py` | Z-score 异常检测 + 路由降权反馈 | 质量监控 |
| `memory/conversation_memory.py` | Redis 工作记忆 + ChromaDB 情景记忆 + 用户画像 | 审核会话上下文 |

**一句话结论**：平台缺的是「**语义理解层**」和「**AI 质量度量层**」，EchoMind 恰好两样都有。
融合的价值不在于"用了 Agent"，而在于把审核的**误报率降下来、可解释性提上去、质量可度量**。

---

## 二、架构选型：独立服务，不是嵌进去

这是本方案最重要的一个决策，先说清楚。

### 方案 A：嵌入式（把 EchoMind 代码搬进 `app/agent/`）

- 优点：同进程调用，无网络开销；部署只有一件事。
- 缺点：
  - EchoMind 强依赖 **Redis + ChromaDB**，一起打包会让平台的 `requirements.txt`、
    `Dockerfile`、CI 全部变重，现在"`pip install` 完就能跑 mock 测试"的轻量优势会丢。
  - 现有 136 个测试里有大量 `TestClient` 直连 app 的用例，同进程引入外部依赖会拖慢甚至污染测试。
  - AI 服务崩溃会直接拖垮审核主链路。

### 方案 B：独立服务 + 降级适配器（**推荐**）

```
┌──────────────────────────────────────────────┐
│  bank-ocr-test-platform  (FastAPI, :8001)     │
│                                              │
│  /bank-card/review  ──► 审核主链路（不变）    │
│         │                                    │
│         └─► app/ai_client.py  ──┐            │
│  /ai/explain/{id}  ◄───────────┘            │
│  /admin/ai-assistant  (新页面)                │
└──────────────────┬───────────────────────────┘
                   │ HTTP + 超时 + 熔断 + 降级
                   │ (AI_ASSIST_ENABLED 开关)
┌──────────────────▼───────────────────────────┐
│  EchoMind Service  (FastAPI, :8100)          │
│  intent_recognizer / orchestrator /          │
│  tool_manager / knowledge_base /             │
│  performance_monitor / evaluator             │
│         │                    │               │
│      Redis              ChromaDB             │
└──────────────────────────────────────────────┘
```

- 优点：
  - **主链路零侵入**。`app/ai_client.py` 全部走 try/except + 超时，AI 服务挂了审核照常跑。
  - 平台依赖不变，136 个测试不受影响（新增测试单独隔离，用 mock）。
  - EchoMind 可以独立迭代、独立部署。
- 代价：多一个服务、多一次网络跳转。对测试平台来说完全可接受。

**结论：走方案 B。** 新增的 `app/ai_client.py` 要照着 EchoMind `mcp/tool_manager.py` 的思路写——
超时、熔断、降级三件套一个都不能少。

---

## 三、六个具体结合点（按性价比排序）

### 结合点 1 · RAG 复核助手 ★★★★★（先做这个）

**做什么**

新增接口：

```http
POST /ai/explain/{request_id}
{"question": "这张卡为什么需要人工复核？该怎么让用户重拍？"}
```

返回自然语言解释 + 处置建议。

**怎么做**

1. 把四类语料灌进 ChromaDB：
   - 全部审核原因码释义（README 里那张表已经是现成的）
   - 拍摄规范（`user_home.html` 里的「边缘完整/文字清晰/光线均匀」）
   - 历史复核案例（从 `reports/review_records.db` 导出，**脱敏后**）
   - 审核规则说明（`app/rule_check.py` 的逻辑转成人话）
2. 检索用 EchoMind 的完整链路，不要只做单次向量检索：
   ```
   用户问题 → LLM 查询改写（3 个角度）→ 并行召回 → 去重 → LLM 重排 → Top-K
   ```
   这是 `MCPToolManager.search_with_rewrite()` 已经实现好的，直接迁。
3. 拿 `request_id` 从 SQLite 取回这条记录，拼进 Prompt，让模型针对**这一次**的
   `quality_reasons` 和 `review_reasons` 给结论。

**为什么先做这个**：改动最小、依赖最少、业务价值最直观，而且它是一个完整可讲的 RAG 落地案例。

---

### 结合点 2 · AI 质量评测体系统一 ★★★★★（和 1 并列）

**做什么**

把平台已有的 OCR 评估和 EchoMind 的 LLM 评测合成**一份报告**：

| 评测层 | 指标 | 来源 |
| --- | --- | --- |
| OCR 层 | 字段级字符准确率、字段召回率 | 平台现有 `evaluate_bank_card_ocr.py` |
| 决策层 | 审核结论一致率（AI 结论 vs 人工标注） | **新增**，需要标注集 |
| 解释层 | 相关性 / 准确性 / 完整性 / 有用性 | EchoMind `LLMJudge` |
| 回归层 | 各指标 vs `baseline.json`，退化 >5% 报警 | EchoMind `_detect_regressions()` |

**关键设计**

- 决策层需要一份**人工标注集**。`data/annotations/labels.json` 已经在仓库里，
  先确认它标的是字段还是结论，不够就补 50~100 条边界样本（模糊、反光、缺字段各若干）。
- 评测入口做成 CLI + pytest 双通道：
  - CLI：`python -m scripts.evaluate_ai_review`
  - pytest：`tests/test_ai_review_evaluation.py`，**必须 mock LLM**，否则 CI 会不稳定且烧钱
- 报告输出复用平台已有的 **Allure**，不要另起一套。

**为什么值得**：这是整个方案里对**测试开发岗**最有说服力的一块——
「AI 系统的质量怎么度量」是当下测开面试的高频问题，而这个报告就是完整答案。

---

### 结合点 3 · 规则 + LLM 双判，降误报 ★★★★☆

**做什么**

现在 `rule_check.py` 一判 `review` 就进人工队列。改成两级：

```
规则判定
  ├─ pass 且字段完整      → 直接 pass（不动，零成本）
  ├─ review（边界情况）    → 交给 LLM 复核 Agent
  │                          ├─ LLM 也认为要复核 → review（进人工）
  │                          └─ LLM 认为可放行   → pass（但标记 llm_override=true）
  └─ reject               → 直接 reject
```

**边界情况具体指什么**（要能说清楚，否则就是瞎用 LLM）：

- 质量分在阈值附近的（比如模糊度 0.42，阈值 0.45）
- 只有某一项原因码，且属于"可能误报"类（`glare_detected` 常因卡片本身的镭射反光误判）
- 字段解析出但校验失败，且形态接近正确（比如卡号差了 1 位，可能是 OCR 误识而非假卡）

**注意**：`llm_override` 必须落库（`review_records` 加一列），
否则事后无法评估「LLM 到底帮了忙还是添了乱」——这一点是很多 demo 漏掉的。

**成本控制**：只对边界样本调用，正常样本走规则。EchoMind 的 TTL 缓存直接复用。

---

### 结合点 4 · 监控闭环 ★★★★☆

**做什么**

Fork EchoMind 的 `PerformanceMonitor`，接平台的数据源：

- Agent 维度：解释请求的成功率、P95 延迟
- 工具维度：知识库检索的命中率、熔断状态
- 业务维度：**AI 改判率**（`llm_override` 占比）、**回滚率**（改判后又被人工打回的比例）

最后一个是关键指标——它直接回答「AI 复核到底靠不靠谱」。

Z-score 异常检测保留：如果某天 AI 改判率突然飙升，说明规则阈值漂了或者模型变了，要报警。

暴露 `/metrics`（Prometheus 格式），平台现在没有这个接口。

---

### 结合点 5 · 审核会话记忆 ★★★☆☆

**做什么**

审核员连续处理一批影像时，AI 记住上下文，支持多轮追问：

```
审核员：这张卡为什么是 review？
  AI  ：因为 image_blur，模糊度 0.42 接近阈值 0.45……
审核员：那上一张呢？
  AI  ：上一张是 glare_detected，卡片镭射区反光……
审核员：这一批这类问题多吗？
  AI  ：本批次 12 张里有 5 张同类问题，建议提醒用户……
```

复用 `memory/conversation_memory.py` 的三级结构，但**要降级使用**：

- Redis 工作记忆（最近 N 轮）—— 够用
- ChromaDB 情景记忆 —— 审核场景跨会话价值有限，**建议先不做**
- 用户画像 —— 可以改成「审核员偏好」，比如他常关注哪类问题

**为什么排后面**：投入产出比不如前几个，属于锦上添花。

---

### 结合点 6 · 前端融合 ★★★☆☆

**做什么**

- **用户端不动**。`user_home.html` 明确写了"不展示 OCR 原文、完整证件号码或内部审核原因"，
  这个安全边界必须守住，AI 能力只在管理端暴露。
- **管理端新增** `admin_ai_assistant.html` + `.js`：左侧审核记录列表，右侧对话式解释面板。
- **审核详情页**加一个「让 AI 解释」按钮，跳到助手页并带上 `request_id`。

**约束**：新增页面必须复用 `portal.css`，不能引入第二套设计系统。
现有测试断言了 `href="/static/portal/portal.css"`，新页面同样满足即可。

---

## 四、落地分期

| 阶段 | 内容 | 交付物 | 依赖 |
| --- | --- | --- | --- |
| **P0** | RAG 复核助手 + `/ai/explain` + 管理端页面 | 可用的 AI 解释功能 | EchoMind 独立服务能跑起来 |
| **P1** | AI 评测体系统一 + Allure 报告 + baseline | `scripts/evaluate_ai_review.py` | 需要补齐人工标注集 |
| **P2** | 规则 + LLM 双判 + `llm_override` 落库 | 误报率对比数据 | P1 的评测能衡量效果 |
| **P3** | 监控接入 + `/metrics` + AI 改判率看板 | Prometheus 指标 | 无 |
| **P4** | 会话记忆 | 多轮追问 | P0 |

**建议路径**：P0 → P1，先把"能用"和"能衡量"做出来。有数据之后再决定 P2 值不值得做。

---

## 五、风险清单

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| **数据安全** | 证件号、姓名、住址外发给第三方模型，合规上不可接受 | 平台已有 `logging_utils.mask_sensitive_data`，AI 侧要**复用同样的脱敏**再发请求；生产环境应换本地模型；测试数据只用合成样本 |
| **主链路被拖慢** | 审核接口 P95 劣化 | AI 调用全部异步 + 超时（建议 3s）+ 熔断；`AI_ASSIST_ENABLED=false` 时完全走原逻辑 |
| **测试不稳定** | LLM 输出不确定，CI 随机红 | 评测测试**必须 mock LLM**，沿用平台"mock 优先"的思路；真实模型只在手动 `--live` 跑 |
| **成本失控** | 每次解释都调 LLM | TTL 缓存（EchoMind 已有）+ 只对边界样本双判 + 按 `request_id` 缓存结果 |
| **评测无标注** | 决策层一致率算不出来 | P1 阶段先补齐 50~100 条边界样本标注 |
| **破坏现有测试** | 136 passed 变红 | 新增代码全部走新文件；改动用 `pytest` 全量回归 |

---

## 六、面试怎么讲（两个岗位各一套）

### 对 AI 应用岗

主线用 **P0 + P2**：`RAG 检索优化链路` + `规则与 LLM 双判`。

可以讲的点：

- 为什么检索要做**查询改写 + 并行召回 + LLM 重排**，单次向量检索的问题在哪
- 为什么**不是所有样本都走 LLM**——按边界条件筛选，成本降一个数量级
- 为什么 `llm_override` 必须落库——否则无法评估 AI 到底是帮忙还是添乱
- 熔断 / 缓存 / 降级三件套在 LLM 调用链上怎么用

### 对测试开发岗

主线用 **P1 + P3**：`AI 评测体系` + `质量监控`。

可以讲的点：

- **四层评测模型**：OCR 层 → 决策层 → 解释层 → 回归层，每层用什么指标
- **LLM-as-Judge** 怎么用、它本身的偏差怎么校准（人工标注抽检）
- **回归基线**：怎么定 baseline、退化阈值怎么设、CI 里怎么卡
- **不确定性系统的测试策略**：mock LLM 保证 CI 稳定，真实模型做离线评测
- 业务指标监控：AI 改判率、回滚率，用 Z-score 做异常检测

这套叙述的价值在于：它展示的不是"我会写 pytest"，而是"**我知道 AI 系统的质量边界在哪，并且能把它量化**"。

---

## 七、下一步

方案就到这里。要动手的话，建议第一步是 **P0 的最小切片**：

1. 把 EchoMind 的 `knowledge_base.py` + `tool_manager.py` 抽出来，去掉 FastAPI 外壳，做成独立可跑的检索服务
2. 灌入原因码释义 + 拍摄规范两份语料
3. 平台侧加 `app/ai_client.py`（带超时降级）+ `POST /ai/explain` 接口
4. 管理端加一个最简单的结果面板

跑通之后再谈评测和双判。

---

## 八、P0 落地实况

> 以下记录实际实现，**包含三处对上面方案的偏离**，以及实现过程中发现并修掉的三个真实缺陷。
> 运行时说明见 `docs/AI复核助手接入说明.md`，服务设计见 `ai_service/README.md`。

### 8.1 交付物

| 位置 | 内容 |
| --- | --- |
| `ai_service/` | 独立 FastAPI 服务（`:8100`）：`corpus / retrieval / tool_manager / llm / explain / api` |
| `ai_service/tests/` | 75 个单测，全部离线 |
| `app/ai_client.py` | 平台侧客户端：超时 + 三态熔断 + 强制脱敏 + 永不抛异常 |
| `app/ai_routes.py` | `POST /ai/explain/{request_id}`、`GET /ai/status` |
| `app/logging_utils.py` | 新增 `REDACT_TEXT_FIELDS` / `mask_text_keep_prefix` / `sanitize_review_fields` |
| `app/static/portal/admin_review_detail.{html,js}` | 详情页 AI 复核面板 |
| `tests/test_ai_*.py` | 78 个测试：客户端、接口、语料一致性 |

未改动：`app/main.py` 只加了 2 行（import + `include_router`），审核主链路一行未动。

### 8.2 三处有意偏离

**偏离一：AI 服务放在平台仓库 `ai_service/`，不放 EchoMind 仓库。**

方案里写的是「EchoMind Service（:8100）」。实际放进平台仓库，理由是：
demo 与面试场景下，一个仓、两条命令就能跑起来，跨仓运行依赖是部署味道很差的设计；
而「独立服务」的实质约束是**独立进程 + 独立依赖 + HTTP 边界**，这三点都满足了 ——
`ai_service/` 不在 `app/` 包内，平台 294 个既有测试不会 import 它，AI 崩了也不影响主链路。
EchoMind 的来源在 `retrieval.py` / `tool_manager.py` 的文件头注释里标明。

**偏离二：不引入 ChromaDB 与 Redis。**

这是三处偏离里最重要的一处。方案原本打算直接迁 ChromaDB，实际改成了
**BM25 + 字符 n-gram 哈希向量 + 原因码精确命中**的三路混合检索。理由：

1. 审核域全部知识只有几十条片段，这个量级上词法检索精度不输小模型向量，且召回可解释；
2. CI 需要可重复结果，而向量检索依赖 `all-MiniLM-L6-v2`（约 90MB 模型下载），离线直接失败；
3. 引入 chromadb 会拖进 onnxruntime 等重依赖链，破坏平台「装完就能跑 mock 测试」的轻量优势。

保留可插拔：实现 `retrieval.VectorBackend` 协议即可替换向量后端。语料上量后应当换。

**偏离三：管理端入口是详情页内嵌面板，不是独立 `admin_ai_assistant.html` 页面。**

方案结合点 6 写的是新建助手页。P0 实际做成**在审核详情页内嵌「AI 复核助手」卡片**。
理由：审核员的高频动作是「解释**这一条**」，内嵌比跳转少一次上下文切换，改动面也更小
（不动路由、不加页面）。**对话式多轮助手仍然值得做，但它依赖会话记忆（P4），
单轮解释做不成那种交互，所以拆开。**

### 8.3 实现中发现并修掉的真实缺陷

这三处不是设计取舍，是 bug。记在这里因为它们是「测试写对了才有价值」的例证。

**缺陷一：`ToolManager._invoke` 的超时形同虚设。**

原写法先把同步 handler 直接调用，再丢进 `asyncio.to_thread`：

```python
result = tool.handler(params, context)          # ← 在事件循环线程上阻塞执行
return await asyncio.wait_for(asyncio.to_thread(lambda: result), timeout=...)
```

后果是：一个慢 handler 会**阻塞整个事件循环**，超时永远不会触发 ——
这正好废掉了方案里「AI 调用全部异步 + 超时 + 熔断」的核心保护。
修法是先判断是否协程函数，同步 handler 一律走 `asyncio.to_thread(handler, ...)` 再套超时。
`test_slow_tool_times_out_instead_of_blocking` 覆盖。

**缺陷二：CSRF 依赖排在鉴权之前，匿名请求拿到 403 而不是 401。**

路由里 `Depends(validate_csrf_request)` 写在 `require_admin_api_user(request)` 之前时，
FastAPI 会先解析依赖，于是未登录请求先撞 CSRF 校验返回 403。
改为把鉴权也写成 `Depends` 并排在前面，与 `/bank-card/review` 的既有约定一致。

**缺陷三：平台的 `mask_sensitive_data` 不处理姓名与住址。**

它只有银行卡号和身份证号的正则，**姓名、住址这类文本 PII 原样通过**。
而方案的风险清单第一条就是「证件号、姓名、住址外发给第三方模型，合规上不可接受」。
新增 `REDACT_TEXT_FIELDS` + `mask_text_keep_prefix`（保留首字打星），
在 `AIAssistClient._sanitize_payload` 里强制执行，调用方不需要记得做。
**日志脱敏路径未改动**，避免影响既有行为。

### 8.4 测试

新增 153 个测试（服务侧 75 + 平台侧 78），全仓共 447 个。

AI 相关测试全部走离线路径：`tests/conftest.py` 的 `isolate_ai_assist` 自动清空 `AI_*`
环境变量并丢弃共享客户端，防止开发机配置让测试真的发出 HTTP 请求。

其中最有价值的一个是 `tests/test_ai_corpus_consistency.py`：它驱动平台**真实的**规则函数
收集全部 22 个原因码断言语料全覆盖，并从 `quality_check.py` / `rule_check.py`
的**源码里提取阈值字面量**（正则抓 `variance < 80.0` 这类），断言语料中出现。
阈值改了但语料没同步 → 测试直接红。

### 8.5 下一步

按原方案走 P1（AI 评测体系统一）。但先补一件事：
**`data/annotations/labels.json` 要确认标的是字段还是结论** ——
决策层一致率算不出来，P1 就推进不下去。

---

## 九、P1 修订版：从 RAG 解释器到「可测 Agent」（面向 AI 应用 / Agent 测试岗位）

> 本节是对原有 P1~P4 的**重排与升级**，不是推倒重来。
> 触发原因：项目要投 **AI 应用开发** 和 **Agent 测试开发** 两个方向，
> 而 P0 交付物本质是**固定流水线的 RAG**（改写 → 召回 → 重排 → 生成），
> 没有工具调用循环、没有规划、没有多步决策。面试官问「你做过 Agent 吗」，
> 目前只能答「做过 RAG」。这一节把项目补成真正的 Agent，并配一整套 Agent 测试体系。

### 9.1 能力缺口诊断

| 维度 | 岗位关注点 | 项目现状 | 缺口 |
| --- | --- | --- | --- |
| 模型接入 | 真实调过模型、Prompt 可管控 | LLM 路径可降级，但没有版本化 Prompt、无强制结构化输出 | 中 |
| Agent 架构 | 工具调用循环、规划、多步决策 | 固定流水线，无 tool-calling loop | **大（核心）** |
| Agent 测试 | 轨迹断言、确定性回放、成本/步数约束 | 只有单轮检索/解释的单测 | **大（核心）** |
| 质量评测 | golden 集、多层指标、judge 校准 | 有 OCR 层评测脚本，无决策/解释层 | 中 |
| 鲁棒性 | 工具故障注入、对抗输入、越权防护 | 有超时/熔断/降级，无对抗与注入测试 | 中 |
| 观测 | 指标、改判率、回归门禁 | 无 `/metrics`、无 baseline 门禁 | 中 |
| 协议生态 | MCP 工具化 | 无 | 小（加分项） |

一句话：**中间那两行是这道题的主线，其余是围绕主线把故事讲完整。**

### 9.2 目标形态：一个会调工具的审核 Agent

P0 是「给一条记录，生成一段解释」。P1 要变成「给一条记录和一个目标，Agent 自己决定查什么、调什么工具、走几步」。

```
审核记录 + 审核员问题
        │
        ▼
  Agent（planner + executor，最多 N 步）
        │  每一步：Thought → Tool Call → Observation
        ├─► search_knowledge      查原因码释义 / 规范 / 案例（复用 P0 检索）
        ├─► get_review_record     按 request_id 取记录详情
        ├─► recompute_quality     对图片重算质量指标（暴露真实阈值）
        ├─► rerun_ocr_region      对指定区域重跑 OCR（可选，验证字段缺失根因）
        └─► escalate_to_human     证据不足时主动请求人工
        │
        ▼
  最终答复（结构化）+ 完整 trace（工具序列 / 参数 / 步数 / token / 耗时）
```

**硬约束（沿用 P0 立场）：**

1. Agent 仍在 `ai_service/` 独立进程内，平台主链路零侵入，AI 挂了审核照常跑。
2. 事实与措辞分离的承诺不变 —— 阈值、处置建议来自语料，不由模型编。
3. **每一步都能降级**：LLM 不可用时退化为「确定性固定工具序列」，功能不中断（对应 P0 的模板生成）。
4. Agent 只能调用白名单工具，工具参数要过 schema 校验，**越权调用直接拒绝**。
5. 步数、超时、token 三重预算，超预算即收敛并标记 `truncated=true`。

### 9.3 Agent 测试体系（本阶段真正的交付重点）

这是「Agent 测试开发」岗的核心叙事：**如何测量一个不确定、会调工具、会多步决策的系统。**

#### （1）确定性回放：LLM 与工具的 record/replay

- 所有 LLM 调用与工具调用经过一层可录制代理，落盘为 cassette（JSON）。
- CI 用 `replay` 模式对同一输入重放轨迹，结果必须逐字节一致；`live` 模式只在手动 `--live` 下跑。
- 契约：**没有 cassette 不得访问网络**，从机制上杜绝 CI 抖动与烧钱。

#### （2）轨迹断言（区别于普通单元测试的关键）

- 工具调用**序列**是否符合预期（如先 `search_knowledge` 再 `escalate_to_human`）。
- 工具**参数**正确性（如 `get_review_record(request_id=正确值)`）。
- 步数、token、耗时**不超预算**；不出现死循环 / 重复调用。
- 非法工具名、schema 不合法参数、越权调用必须被拒绝且不产生副作用。

#### （3）golden 数据集与多层指标

| 层 | 指标 | 数据来源 |
| --- | --- | --- |
| 工具层 | 工具选择准确率、参数正确率、平均步数 | 人工标注的期望轨迹（先补 30~50 条边界样本） |
| 任务层 | 任务成功率（结论正确 / 依据完整 / 处置可用） | 人工标注结论 |
| 解释层 | 相关性 / 准确性 / 完整性 / 有用性 | LLM-as-Judge 四维打分 |
| 回归层 | 各指标 vs `baseline.json`，退化 >5% 报警 | 历次运行 |

#### （4）LLM-as-Judge 必须校准，不能盲信

- Judge 本身也会飘。用**人工标注子集**去校准 judge，报告 judge 与人工的一致率。
- Judge 打分固定 `temperature=0`、固定 rubric、固定模型版本，否则趋势不可比。
- 面试可讲点：judge 的偏差来源、如何用抽检估计、为什么不能把 judge 分数当绝对真值。

#### （5）鲁棒性与对抗

- 工具故障注入：工具超时 / 返回 500 / 返回非法结构时，Agent 是否降级而非崩溃。
- 模型异常：返回非法 JSON、空响应、超长输出，结构化解析是否兜底。
- Prompt injection：记录文本或知识片段里植入「忽略以上指令」等，验证 Agent 不被劫持。
- 越权与数据边界：确认 Agent 无法拿到未脱敏字段，不能调用白名单外工具。

#### （6）接入 CI 的回归门禁

- `pytest` 离线全量 + cassette 回放，全绿才算通过。
- 评测成功率、平均步数、成本写入 `baseline.json`，退化超阈值 → CI 红。
- 报告复用平台已有的 Allure，不另起一套。

### 9.4 修订后的落地分期

| 阶段 | 内容 | 主要交付物 | 验收标准 |
| --- | --- | --- | --- |
| **P1.1** | 真实模型路径可管控 | Prompt 版本化、结构化输出、`--live` 冒烟 | live 可跑通；输出过 pydantic 校验 |
| **P1.2** | Agent 化 | 工具注册表 + tool-calling 循环 + 预算控制 + trace | 能自主选工具完成任务，超预算收敛 |
| **P1.3** | Agent 测试框架 | cassette 回放、轨迹断言、故障注入、对抗用例 | 离线全绿；断网不含 cassette 必失败 |
| **P1.4** | 评测与门禁 | golden 集、四层指标、judge 校准、baseline | 生成一份 Allure/CLI 评测报告 |
| **P2** | 规则 + LLM 双判 | `llm_override` 落库、误报率对比 | 有改判率 / 回滚率数据 |
| **P3** | 观测 | `/metrics`、AI 改判率看板、Z-score 告警 | Prometheus 可抓取 |
| **P4** | 会话记忆 + MCP | 多轮追问；平台能力暴露为 MCP 工具 | 多轮上下文可用；MCP server 可挂载 |

> 与旧分期的差异：把「AI 评测体系」拆成 **P1.3 + P1.4**，并在前面插入 **P1.1 模型路径** 与 **P1.2 Agent 化**。
> 原因：没有真实 Agent，就没有可测的轨迹；没有可控的模型路径，评测就没有稳定输入。

### 9.5 面试讲法（两个方向各一条主线）

**AI 应用开发岗** —— 主线用 P1.1 + P1.2 + P2：

- 为什么用独立服务 + 超时/熔断/降级，而不是把 Agent 塞进主链路
- 为什么只对边界样本调 LLM，成本如何降一个数量级
- 结构化输出 / Prompt 版本化 / 失败重试与幂等
- `llm_override` 必须落库，否则无法评估「AI 是帮忙还是添乱」

**Agent 测试开发岗** —— 主线用 P1.3 + P1.4 + P3：

- 怎么让不确定系统在 CI 里确定（cassette 回放）
- 怎么断言轨迹而不只是断言文本
- 怎么校准 LLM-as-Judge，如何报告 judge 与人工的一致率
- 怎么把「任务成功率 / 步数 / 成本」做成回归门禁
- 对抗输入、工具故障注入、越权防护怎么测

### 9.6 本阶段不做的事

- 不引入 ChromaDB / Redis（与 P0 立场一致，语料规模不需要）。
- 不重构平台主链路，不新增 service/controller 分层（仍属 V2.0）。
- 不做多轮会话记忆（P4），Agent 本阶段先做**单轮内的多步工具决策**。
- 不让 Agent 直接改判审核结论（双判属 P2，且必须与落库一起做）。

