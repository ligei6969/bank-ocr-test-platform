# AI 复核助手接入说明（平台侧）

> 对应融合方案的 **P0**：RAG 复核助手 + `/ai/explain` + 管理端面板。
> AI 能力本体在 `ai_service/`，本文件说明平台侧怎么接、怎么关、怎么排错。

---

## 一、两个进程

```
┌────────────────────────────────────────────┐
│  bank-ocr-test-platform   :8000            │
│  /bank-card/review  /id-card/review         │  审核主链路（未改动）
│  /ai/explain/{request_id}                   │  AI 解释入口（管理端）
└───────────────┬────────────────────────────┘
                │ app/ai_client.py
                │ HTTP + 3s 超时 + 三态熔断 + 强制脱敏
┌───────────────▼────────────────────────────┐
│  ai_service               :8100            │
│  /explain  /search  /health                 │
└────────────────────────────────────────────┘
```

```bash
# 终端 1
python -m ai_service

# 终端 2
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**AI 服务不是必须启动的。** 不启动时审核、上传、记录查询全部照常，
只是管理端详情页的 AI 面板会显示「AI 复核服务未启动」。

---

## 二、环境变量

平台**没有接 `python-dotenv`**（`requirements.txt` 里有这个包但代码没用它），
所以不要指望写 `.env` 生效 —— 直接设进程环境变量：

```bash
# Git Bash
export AI_ASSIST_ENABLED=true
export AI_SERVICE_URL=http://127.0.0.1:8100

# PowerShell
$env:AI_ASSIST_ENABLED = "true"
$env:AI_SERVICE_URL = "http://127.0.0.1:8100"
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AI_ASSIST_ENABLED` | `true` | 总开关。`false` 时完全不发请求，直接返回降级结果 |
| `AI_SERVICE_URL` | `http://127.0.0.1:8100` | AI 服务地址 |
| `AI_ASSIST_TIMEOUT_S` | `3.0` | 单次调用超时。宁可没有解释，也不让页面转圈 |
| `AI_ASSIST_FAILURE_THRESHOLD` | `3` | 连续失败几次后熔断 |
| `AI_ASSIST_RECOVERY_S` | `60` | 熔断后多少秒进入半开探测 |

---

## 三、降级行为

`app/ai_client.py` 的硬约束是**永不抛异常**。所有失败路径都返回结构化结果：

| 场景 | `reason` | 页面表现 |
| --- | --- | --- |
| 开关关闭 | `disabled` | 「AI 复核助手未启用」 |
| 服务没起 / 端口不通 | `unreachable` | 「AI 复核服务未启动或网络不可达」 |
| 超过 3 秒没响应 | `timeout` | 「响应超时」 |
| 连续失败已熔断 | `circuit_open` | 「连续失败已熔断，稍后自动重试」 |
| 返回体不是合法 JSON | `invalid_response` | 「返回了无法解析的内容」 |
| 服务返回 5xx | `http_error` | 「返回错误状态码」 |

**关键约定：降级也是 HTTP 200。** 前端必须能区分两件事：

* `404` = 记录不存在
* `200` + `available: false` = 记录存在，但 AI 没回答

熔断生效后，页面不再每次都白等一个 3 秒超时 —— 这是把熔断放在客户端而不是服务端的原因。

---

## 四、安全边界

### 出站脱敏是强制的

脱敏在 `AIAssistClient._sanitize_payload` 里执行，**不依赖调用方记得做**：

| 字段 | 处理 |
| --- | --- |
| `card_number` / `id_number` | 走 `mask_sensitive_data`，如 `622202******7890` |
| `name` / `address` / `持卡人` / `住址` 等 | 走 `mask_text_keep_prefix`，保留首字打星，如 `张*` |
| `question` / `error_message` | 通用脱敏（用户可能把证件号粘贴进问题里） |

> 这里补了一个平台原有的缺口：`logging_utils.mask_sensitive_data` 只处理数字，
> **姓名和住址这类文本 PII 原本不会被脱敏**。`REDACT_TEXT_FIELDS` 与
> `mask_text_keep_prefix` 是为 AI 出站场景新增的，日志脱敏路径未改动。

### 用户端不暴露

AI 能力**只在管理端**。`user_home.html` 明确写了「不展示 OCR 原文、完整证件号码或内部审核原因」，
这个边界没有动。`/ai/*` 全部要求管理员角色。

### 接口鉴权

| 端点 | 要求 |
| --- | --- |
| `GET /ai/status` | 管理员 |
| `GET /ai/status?probe=true` | 管理员（额外探测一次 AI 服务） |
| `POST /ai/explain/{request_id}` | 管理员 + CSRF |

依赖顺序有意为之：**鉴权排在 CSRF 之前**，匿名请求得到 401 而不是 403，
与 `/bank-card/review` 的既有约定一致。

---

## 五、管理端界面

`/admin/reviews/{request_id}` 底部新增「AI 复核助手」面板：

* 点击「让 AI 解释这条记录」触发生成，结果分四块展示：
  **综合解释** / **原因码逐条释义**（含义、触发条件、实现位置、处置建议、用户话术）/
  **处置建议** / **知识引用**
* 状态徽章区分 `未请求 / 正在生成 / 已生成 / 已降级 / 请求失败`
* 底部元信息显示：解释生成方式、查询改写策略、结果重排策略、检索后端、置信度、耗时
* 未配置大模型时，面板会明确标注「已用模板与规则检索生成解释；事实内容取自知识库，可正常参考」

前端实现约束（沿用平台既有测试）：仅用 `textContent` 与 `createElement` 构建 DOM，
不使用 `innerHTML`、不在脚本里写调试输出、不使用 `localStorage`/`sessionStorage`。

---

## 六、验证

```bash
# AI 服务侧：75 个单测，全部离线
python -m pytest ai_service/tests -q

# 平台侧：AI 相关 78 个单测 + 全量回归
python -m pytest tests/test_ai_client.py tests/test_ai_explain_api.py tests/test_ai_corpus_consistency.py -q
python -m pytest -q
```

测试全部用离线路径：`tests/conftest.py` 里的 `isolate_ai_assist` 自动清空
`AI_*` 环境变量并丢弃共享客户端，防止开发机上的配置让测试真的发出 HTTP 请求。

### 冒烟检查

```bash
# 1. AI 服务自检（不需要 API key，走降级路径）
python -m ai_service --demo

# 2. 平台侧看客户端状态
curl -s localhost:8000/ai/status?probe=true -b cookies.txt | python -m json.tool
```

---

## 七、排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 面板一直显示「未启动」 | `ai_service` 没跑，或端口不对 | `python -m ai_service`；核对 `AI_SERVICE_URL` |
| 连续几次后变成「已熔断」 | AI 服务中途挂了 | 恢复服务，等 `AI_ASSIST_RECOVERY_S` 秒自动重连；或重启平台进程 |
| 面板显示「已降级」但内容完整 | 正常行为：没配 `LLM_API_KEY` | 想要大模型措辞就配 key，事实内容本来就完整 |
| 解释里出现「语料未收录」 | 平台新增了原因码但语料没同步 | 在 `ai_service/corpus.py` 补条目，`test_ai_corpus_consistency.py` 会拦住漏改 |
| `POST /ai/explain` 返回 403 | 缺 CSRF 头或不是管理员 | 前端走 `portalSecurity.fetchWithCsrf`；核对账号角色 |

---

## 八、下一步（P0 之外）

> P0 之后的分期已按「适配 AI 应用 / Agent 测试岗位」重排，详见
> `docs/AI智能审核融合方案.md` 第九、十节。核心变化：先把固定流水线的 RAG
> 升级为**会调工具、多步决策的审核 Agent**并补齐测试体系（**P1 已完成**），
> 再新增一条**银行业务知识客服 Agent**主线（一内核多产品面），最后补双判闭环与观测。

**P1 已完成（见 `docs/P1_Agent开发报告.md`）**：Agent 化、cassette 回放、
轨迹断言、故障注入、四层指标与 CI 回归门禁均已落地；测试 447 → 647 全绿；
`app/main.py` 主链路一行未动。新增接口 `POST /agent/explain`（与 `/explain` 并存）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P1.1 | 真实模型路径可管控：Prompt 版本化、结构化输出、`--live` 冒烟 | ✅ 完成 |
| P1.2 | Agent 化：工具注册表 + tool-calling 循环 + 三重预算 + trace | ✅ 完成 |
| P1.3 | Agent 测试框架：cassette 回放、轨迹断言、故障注入、对抗输入 | ✅ 完成 |
| P1.4 | 评测与门禁：golden 集、四层指标、LLM-as-Judge 校准、baseline 回归 | ✅ 完成 |
| P1.5 | 收口：真实模型端到端验证 + usage 成本；（可选）结论标注 | 下一步 |
| P2.1 | **对内银行业务知识客服 Agent**（复用内核 + 新 corpus/工具/安全边界） | 下一步 |
| P2.3 | 规则 + LLM 双判，`llm_override` 落库（**必须落库**） | 待结论标注 |
| P3 | 监控接入：AI 改判率、回滚率、`/metrics`；Agent trace 接入管理端 UI | 待定 |
| P4 | 会话记忆 + MCP server | 待定 |

具体任务拆解与自测要求见 `docs/下一步开发计划.md`（含 Knowledge Agent 子方案）。

---

## 九、本轮实测记录（P0 验收）

### 9.1 自动化回归

```bash
CODEBUDDY_SAFE_DELETE_ENABLED=0 python -m pytest -q
# 447 passed, 1 warning
```

| 分组 | 用例数 | 说明 |
| --- | --- | --- |
| `ai_service/tests` | 75 | 检索、解释编排、工具框架（含熔断/超时/降级） |
| `tests` | 372 | 平台原有用例 + 新增 AI 相关 78 个 |

> Windows 环境下若用 WorkBuddy 的沙箱终端运行，需要加 `CODEBUDDY_SAFE_DELETE_ENABLED=0`。
> 该垫片会把 `Path.unlink` 改道到回收站，回收站在非系统盘上会抛
> `SHFileOperationW 失败: 0x2`，导致 pytest 清理 `tmp_path` 失败并产生大量
> 假 teardown error。**与本项目代码无关**，正常命令行环境无需该变量。

### 9.2 真实链路端到端

起两个进程：

```bash
# 终端 1：AI 检索服务（无需 API key，走确定性降级路径）
python -m ai_service --host 127.0.0.1 --port 8100

# 终端 2：平台
REVIEW_RECORDS_DB_PATH=./reports/e2e.db \
AI_ASSIST_ENABLED=true AI_SERVICE_URL=http://127.0.0.1:8100 \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

用真实 CSRF + 会话流程投递一张人为劣化的银行卡图（高斯模糊 + 压暗），
命中 `image_blur` 与 `image_dark` 两个原因码，再调用 `POST /ai/explain/{request_id}`：

| 字段 | 实测值 |
| --- | --- |
| `available` | `true` |
| `degraded` | `true`（未配置大模型，模板生成） |
| `engine.retrieval` | `hashing-ngram-512` |
| `engine.rewrite` / `rerank` | `rule` / `consensus` |
| `confidence` | `0.8` |
| `reason_details` | 2 条（逐条含含义 / 触发条件 / 实现位置 / 处置建议 / 用户话术） |
| `citations` | 5 条（带命中通道与分数，可回溯到语料文档） |
| `unknown_reason_codes` | `[]` |

工具链 trace：`query_built` → `rewrite(rule, 3 个子查询)` → `merge(9 候选 / 15 原始命中)` → `rerank(consensus, 保留 5)`。

管理端「AI 复核助手」面板实测渲染：状态徽章 `已生成`，释义 2 条、建议 3 条、引用 5 条，并显式标注「未配置大模型，已用规则与检索结果生成解释」。

### 9.3 本轮修掉的两个真问题

1. **`tool_manager` 同步 handler 超时失效**：原实现在事件循环线程上直接调用同步函数，
   `asyncio.wait_for` 只包住了返回后的 awaitable，慢工具会把整个异步服务卡住。
   改为 `asyncio.to_thread` 后再套 `wait_for`。
2. **`/ai/explain` 的依赖顺序**：`validate_csrf_request` 排在鉴权之前，
   导致匿名请求拿到 403 而不是 401。改为与平台其他接口一致的 `Depends` 顺序。

另外补齐了 `mask_sensitive_data` 只脱敏数字、不脱敏姓名与住址的缺口。
