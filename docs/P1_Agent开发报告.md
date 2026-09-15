# P1 开发报告：从 RAG 解释器到可测 Agent

> 对应任务书：`docs/WorkBuddy_P1_Agent开发提示词.md`
> 背景设计：`docs/AI智能审核融合方案.md` 第九节
> 上一阶段：`docs/AI复核助手接入说明.md`（P0）

---

## 一、分阶段完成情况

| 阶段 | 内容 | 状态 | 关键验证 |
| --- | --- | --- | --- |
| P1.1 | 真实模型路径可管控 | ✅ 完成 | 非法 JSON 抛可捕获异常；合法 JSON 正确解析；结果回传 prompt 版本；`--live` 无 key 时友好提示 |
| P1.2 | Agent 化（核心） | ✅ 完成 | 自主选工具、信息不足转人工、非法工具/参数被拒、步数预算收敛、两条路径同结构、工具故障降级 |
| P1.3 | Agent 测试框架 | ✅ 完成 | replay 逐字节一致；无 cassette 时必失败且不出网；对抗用例全绿；故障注入稳定触发降级 |
| P1.4 | 评测与回归门禁 | ✅ 完成 | 四层指标 CLI 离线可跑；退化 >5% 告警；judge 校准输出一致率；已接入 CI |

**测试数字变化**

| | 起点（P0 结束） | 终点（P1 结束） | 变化 |
| --- | --- | --- | --- |
| `ai_service/tests` | 75 | **260** | +185 |
| `tests`（平台侧） | 372 | **387** | +15 |
| 合计 | 447 | **647** | **+200** |

全部通过：`647 passed, 1 warning`（连跑三次一致，无抖动、无 skip、无 xfail）。
既有测试无删除、无放宽断言。

**改动规模**：34 个文件，+8029 / −206 行。

---

## 二、文件清单

### 新增：Agent 与工具

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `ai_service/agent.py` | 839 | planner + executor 多步决策循环、三重预算、trace、确定性降级 |
| `ai_service/tools.py` | 356 | 工具白名单 + 四个工具（`search_knowledge` / `get_review_record` / `recompute_quality` / `escalate_to_human`） |
| `ai_service/thresholds.py` | 193 | 影像质检阈值的机器可读副本，供 `recompute_quality` 反推 |
| `ai_service/prompts.py` | 221 | Prompt 版本化注册表（id + version + label） |
| `ai_service/structured.py` | 209 | 结构化输出解析与 pydantic 校验 |

### 新增：测试框架

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `ai_service/cassette.py` | 327 | LLM / 工具的录制回放代理，未命中即失败 |
| `ai_service/agentkit.py` | 449 | 轨迹断言、故障注入、cassette 装配 |

### 新增：评测层

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `ai_service/eval/golden.py` | 267 | golden 集构造 + doc_type 归一 + 「哪层算不了」的记录 |
| `ai_service/eval/metrics.py` | 358 | 四层指标 + 回归门禁（纯函数） |
| `ai_service/eval/judge.py` | 437 | 四维 rubric judge（离线版 / 模型版）+ 校准 |
| `ai_service/eval/report.py` | 217 | 编排成报告，写 baseline |
| `ai_service/eval/baseline.json` | — | 14 项基线指标 |
| `ai_service/eval/judge_calibration.json` | — | 10 条校准样本（**占位标注，见缺点**） |
| `scripts/evaluate_ai_review.py` | 336 | 评测 CLI，退化超阈值返回 1 |

### 新增：测试

`ai_service/tests/` 下 9 个文件：`test_agent.py`(34) / `test_agent_adversarial.py`(32) /
`test_cassette.py`(17) / `test_eval.py`(42) / `test_prompts.py`(14) / `test_structured.py`(24) /
`test_live_smoke.py`(8) / `test_api.py`(11) / `conftest.py`。
平台侧新增 `tests/test_ai_agent_boundary.py`(4)。

### 修改

| 文件 | 改动 |
| --- | --- |
| `ai_service/explain.py` | 事实层抽成模块级函数（explain 与 agent 共用，避免两套事实）；新增 prompt 版本回传；新增 `quality_metrics` 入参 |
| `ai_service/tool_manager.py` | 换用 prompt 注册表 + 结构化解析；新增 `validate_params` 公开入口；**修复超时失效**（见下） |
| `ai_service/api.py` | 新增 `POST /agent/explain`；`/health` 暴露工具清单 |
| `ai_service/__main__.py` | 新增 `--agent` / `--live`；诊断信息改走 stderr |
| `ai_service/README.md` | 补 P1 架构、工具清单、cassette 用法、评测入口、设计取舍 |
| `tests/test_ai_corpus_consistency.py` | 新增 7 条阈值一致性测试（AI 侧副本 vs 平台源码） |
| `.github/workflows/tests.yml` | 固定 `LLM_PROVIDER=none`；新增评测门禁步骤与 Allure 产物上传 |
| `ai_service/tests/test_explain.py` | 新增 3 条 prompt 版本回传测试 |

**未改**：`app/main.py` 的审核主链路（`/bank-card/review`、`/id-card/review`）一行未动。
平台侧只做了 `ai_client` / `ai_routes` 的向后兼容增强（`ai_routes` 实际未改，
Agent 端点开在 AI 服务侧）。

---

## 三、实现中发现并修掉的真实缺陷

这些都不是「写新功能」，而是写着写着发现原来的东西是错的。按严重程度排：

### 1. 工具超时形同虚设（P0 遗留，高危）

`tool_manager._invoke` 在**事件循环线程上直接调用同步 handler**，
`asyncio.wait_for` 拿到的只是一个已经算完的值 —— 超时根本没生效。
一个慢工具会把整个异步服务卡住，所有并发请求一起挂。

改为 `asyncio.to_thread` 后再套 `wait_for`。这个 bug 之所以一直没暴露，
是因为 P0 的工具都很快；P1 加了故障注入，一测就现形。

### 2. 「证据不足必须转人工」可被模型绕过（P1 引入）

规则最初只写在确定性路径里。结果是模型（或被 prompt injection 影响的模型）
回一句 `finish` 就把规则跳过去了。已改为两条路径在收敛前都必须过这道规则。

### 3. LLM 重排成功时没有记录 prompt 版本

只在降级路径记了 `prompt` 标签，成功路径反而没有 —— 正好记反。
导致「这条解释用了哪版 prompt」在最需要查的成功路径上查不到。

### 4. `recompute_quality` 的一致性核对永远不触发

`_needs_human` 依赖 `state.unknown_codes`，而它正是事实层的产物。
早期实现先判收敛、后算事实，于是「出现未收录原因码就转人工」这条规则
永远不会触发。已把事实层计算提到收敛判定之前。

### 5. 回归门禁的 5% 边界会被浮点误差误判

`(0.95 - 1.0) / 1.0 = -0.05000000000000004`，比 `-0.05` 略小，
于是「正好退化 5%」被报成超限。门禁误报比漏报更伤 ——
误报几次之后所有人都会习惯性忽略它。已加显式比较容差。

### 6. judge 的准确性指标在测错东西

原公式用 `\d+` 抓所有数字。模板里那句「共命中 **1** 个原因码」的「1」
被当成不可追溯数字，把一条完全准确的答复打到 0 分；
而另一条碰巧因为别处出现过「1」就拿满分。已改为只抓阈值类数字
（带小数点或两位以上）—— 没有阈值声明就没有可出错的阈值声明。

### 7. 评测取样按字母序截断，漏掉最大的两个桶

原实现「按桶名排序后逐桶取满」，`id_card_front` / `id_card_back`
排在字母序后面，40 条采完都轮不到它们 —— 而它们占了标注文件的大头。
改为轮转取样，保证覆盖所有桶。

### 8. 喂给服务不认识的 doc_type

标注文件里是 `id_card_front` / `id_card_back` / `application_form`，
而平台只有 `/bank-card/review` 与 `/id-card/review` 两条链路。
直接透传会让检索的 doc_type 过滤全部落空，指标莫名偏低还找不出原因。
已做归一（前两者 → `id_card`，`application_form` 排除）。

### 9. `evidence_rate` 惩罚正确行为

无原因码的记录本来就不该检索，也就不该有引用。原指标把它们算进分母，
等于因为「正确地没去检索」而扣分。已改为只在「本来就该有引用」的样本上计算。

---

## 四、设计取舍

### Agent 只读本次请求那一条记录

`get_review_record` 的数据源是**请求级上下文**，不是平台数据库。
跨进程直连平台 SQLite 会同时破坏三件事：服务边界（AI 服务要挂载平台数据文件）、
安全边界（AI 进程变成能读全库的新面）、部署边界（两个服务没法独立扩缩）。

代价是 Agent 无法「顺着记录查历史」。本阶段本来就只做单轮内多步决策，
这个代价划算；跨记录挖掘与多轮追问留给 P4。

### 工具全部无副作用

四个工具全是只读或纯计算。这既是安全边界，也让「非法调用被拒绝且无副作用」
这条验收标准**天然成立** —— 因为根本没有副作用可产生，而不是靠代码里记得别写库。

改判审核结论属 P2，且必须与 `llm_override` 落库一起做：只改判不记录，
事后无法评估 AI 是帮忙还是添乱。

### 白名单是硬约束，参数校验放在调用前

prompt 里写「你只能用这些工具」是软约束，模型可以不听。白名单在代码里，
不听就执行不了。参数校验也放在**调用前** —— 参数不合法连工具都不碰，
避免「参数错了但工具已经产生副作用」。

`AgentDecision.action` 刻意用 `str` 而不是 `Literal[...]`：用 `Literal` 的话，
非法工具名会在 pydantic 校验阶段变成「解析失败」，trace 里只剩一条
「模型输出不合规」，看不出它想调哪个越权工具。用 `str` 接住再显式校验，
才能留下可审计的拒绝记录。

### cassette 未命中必须响亮失败

`CassetteMissError` 派生自 `BaseException` 而不是 `Exception`。
工具层有一圈 `except Exception`，如果它是普通异常会被安静吃掉 ——
Agent 退化成「工具坏了」继续跑完，测试**绿着骗人**。
派生自 `BaseException` 和 `KeyboardInterrupt` 同一个思路。

同理，replay 模式下 `available` 恒为 `True`：「录制说了算」。
否则没配 key 时会悄悄降级成确定性序列，CI 照样绿。

### 「调用过」与「成功」在 prompt 版本上要分开

`collect_labels` 的语义是「这版 prompt 被送去模型了」，不是「成功了」：

* LLM 未配置 → 根本未调用，不报版本（报了是假信息）；
* 调用了但返回垃圾 / 报错 → **报版本**，因为这正是排查要找的线索，
  同一步还会带 `prompt_failed`。

### 事实层抽成模块级函数

P0 的事实层是 `ReviewExplainer` 的私有方法。P1 的 Agent 需要同一套事实，
如果各写一份，「事实与措辞分离」会退化成「两套事实各自表述」，
阈值和处置建议迟早对不上。已抽成 `build_reason_details` / `build_actions` /
`to_citation` / `render_template_explanation` 四个模块级函数，两条路径共用。

### 离线 judge 的定位

`DeterministicRubricJudge` **不是模型**，它算的是与 rubric 同形的可解释信号
（原因码覆盖率、阈值可追溯率、三要素齐不齐、处置建议覆盖情况）。
它的价值是让指标管线、回归门禁、报告格式在没有外部依赖时可测试、可运行。
真要对外报分数必须用 `LLMJudge` 并跑校准 —— 这一点在模块 docstring 里写死了。

---

## 五、未完成项与原因

| 项 | 状态 | 原因 |
| --- | --- | --- |
| **决策层指标**（真实结论正确率） | 缺数据 | `labels.json` 不标审核结论。与其编一份假标注，不如把缺口写进报告。需要人工结论标注才能算，是 P2 的输入 |
| **judge 校准集** | 占位 | 当前 10 条是项目作者按 rubric 自评，能验证管线、能暴露偏差方向，但**无统计意义**。需替换为真实审核员标注 |
| **Agent 接进管理端 UI** | 未做 | `/agent/explain` 已可用，但前端仍渲染 P0 的解释结构。接进去要新增 trace / 预算面板，超出本阶段「平台主链路零侵入」的范围，且前端改动属独立工作面 |
| **真实模型端到端** | 未验证 | 本机没有可用的 LLM key，`--live` 只验证了「无 key 时友好提示」这条分支。`cassette` 的 live 录制路径有测试覆盖（用假模型），但真实 provider 的协议兼容性未实测 |
| **成本指标** | 未接入 | token 计量是字符估算（`LLMClient` 协议拿不到 provider 的 usage），所以 baseline 里没有成本项。要真做需要扩展 LLM 客户端协议以回传 usage |
| **`/metrics` 与 Prometheus** | 未做 | 属 P3，任务书未要求 |
| **双判与 `llm_override` 落库** | 未做 | 属 P2，且任务书明确禁止本阶段让 Agent 改判 |

---

## 六、怎么跑

```bash
# 全量测试（Windows 沙箱内需关掉安全删除垫片，见下）
CODEBUDDY_SAFE_DELETE_ENABLED=0 python -m pytest -q          # 647 passed

# 只看 AI 服务
CODEBUDDY_SAFE_DELETE_ENABLED=0 python -m pytest ai_service/tests -q   # 260 passed

# Agent 冒烟（离线，走确定性工具序列）
python -m ai_service --agent

# 真实模型冒烟（需配 LLM_API_KEY，会出网）
python -m ai_service --live

# 评测 + 回归门禁（离线；退化超 5% 返回 1）
python -m scripts.evaluate_ai_review --calibrate --allure
```

> **Windows 环境提示**：若出现 `Path.unlink` / `SHFileOperationW` 相关 teardown 报错，
> 加 `CODEBUDDY_SAFE_DELETE_ENABLED=0`。这是沙箱垫片把 `Path.unlink` 改道到回收站
> 导致的（非系统盘上 `SHFileOperationW` 返回 0x2），与本项目代码无关；
> 正常命令行环境不需要该变量。

---

## 七、当前评测基线（供 P2 对比）

```
golden 集：40 条（bank_card / id_card × normal/blur/dark/bright/glare，每桶 4 条）

工具层   工具序列完全匹配率 1.0000    参数正确率 1.0000    平均步数 2.8
任务层   原因码命中率 1.0000（代理）  引用率 1.0000       处置率 1.0000
         转人工判定准确率 1.0000      被截断率 0.0000
解释层   相关性 5.0  准确性 5.0  完整性 5.0  有用性 4.6

judge 校准（离线确定性 judge，10 条占位标注）
         完全一致 45.0%   ±1 分内 67.5%   平均绝对误差 0.875
```

指标全部为 1.0 是**因为离线路径是确定性的**，不是因为系统完美：
它证明的是「规则与语料这条路径做到了设计意图」，而不是「AI 效果好」。
真正有信息量的数字是 judge 校准的一致率 —— 45% / 67.5% 说明
离线 judge 与人工判断存在实质差距，这正是必须用模型 judge 并持续校准的理由。

`explain.usefulness = 4.6` 是唯一没满分的项：答复正文是摘要，
完整处置建议在 `actions` 字段里单独返回，正文只带首条。
这是 P0 以来的有意设计（正文要短、结构化字段要全），不是缺陷。

---

## 八、P1.5 收口：真实模型链路与成本

P1 报告第五节自陈了两个缺口，其中「真实模型端到端未验证」和「成本是字符估算」
最影响面试。本阶段专门收掉它们。

### 8.1 收掉了什么

| P1 遗留 | P1.5 处理 | 状态 |
| --- | --- | --- |
| 真实模型端到端未验证 | 传输 / 鉴权 / 解析 / usage 抽取由协议靶子离线覆盖 | **协议层已验证**；真模型仍待 key |
| 成本是字符估算 | provider 回传 usage 优先，估算降级为兜底 | **已收口** |
| cassette 只回放假模型 | 录制在真实 HTTP 上、回放完全离线，已进测试 | **已收口** |

测试：**647 → 680**（AI 服务侧 260 → 293），全绿，零回归。

### 8.2 四条不可破的设计约束（都有测试）

1. **`take_usage` 不是协议成员。** 用 `getattr` 探测。要求所有实现都提供它，
   等于「加一个成本指标」要改几十个测试替身 —— 而拿不到用量本来就有兜底。
2. **用量累计取走，不是「仅上一次」。** 一次请求要调好几次模型，
   只留最后一次会把成本低估好几倍。**低估比缺失更危险：缺失看得见，低估看不见。**
3. **`--agent` 默认离线。** 哪怕 shell 里配了 key 也不调模型。
   测试断言 `request_count == 0` —— 一次请求都不许发出去。
   「跑一次冒烟」不该有意外花钱的可能。
4. **用量跟着 cassette 一起录制。** 否则回放时记账从真实值掉回估算，
   同一份录制在 CI 里报出的成本和录制时不一致，很容易被误读成「成本涨了」。

### 8.3 新增的验证手段：协议靶子

`ai_service/devtools/mock_llm.py`。

进程内假 LLM 测得到 Agent 逻辑，但 `HttpLLMClient` 的请求构造、鉴权头、
响应解析、错误映射、usage 抽取**只在连真实 provider 时才跑到**。
本机当时没有 key，这段代码就是测试盲区。靶子用本地 HTTP 服务把它补上：
按真实协议返回、带 `usage`、校验鉴权头、可注入故障。不联网、不花钱、无 key。

**边界写清楚**：靶子证明「协议通、链路通、用量能取到」，
**不证明「模型答得好」**。混为一谈就是拿靶子的成绩当自己的成绩。

### 8.4 本阶段发现并修掉的缺陷

1. **靶子的默认响应器被每条请求重建**（自造故障）。它内部有「先查再答」的计数状态，
   放在请求处理里现建则计数每次归零，Agent 一直收到同一个决策，
   最后撞上「重复调用即收敛」提前结束 —— 靶子自己制造出一个假故障。
   改为整个服务只建一次。
2. **header 名的大小写**（测试侧发现，值得记）。`urllib` 的 `add_header` 会把名字
   `capitalize()`，`x-api-key` 实际发出去是 `X-api-key`。
   用精确匹配断言会得出「key 没发出去」的**错误结论**，
   而 HTTP 里 header 名本来就不区分大小写。

第 2 条不是代码 bug 而是测试方法 bug，但它展示的东西更有价值：
**一个写错的断言会伪造出一个不存在的问题**，比漏测更难发现。

### 8.5 实测数据

模型侧用本地协议靶子（协议级验证，非模型能力验证）：

```
--agent --live   决策引擎=llm   degraded=false   停止原因=finished
                 工具序列=['search_knowledge']
                 token_usage: source=provider  prompt 562 / completion 62 / 2 次调用
                 prompt 版本=agent_decide@v1

live 评测（40 条 golden）  cost.avg_tokens=499.325   provider_usage_rate=1.0
离线评测（CI 默认）        cost.avg_tokens=0（不调模型，真实值不是缺失）
```

一个值得留意的副产品：live 评测里 `tools.sequence_accuracy = 0`，离线是 `1.0`。
这不是 bug —— 靶子只会调 `search_knowledge`，而 golden 期望
`get_review_record → search_knowledge → recompute_quality`。
**指标真的在测东西**，比一个恒为 1.0 的指标有说服力得多。

### 8.6 仍未做

- **真实 provider 未实测**：需要一个 key。命令已写在 `ai_service/README.md` 第 5 小节，
  跑完把「本地协议靶子」换成实际模型名与数字即可。
  **没有真跑过的数字不填** —— 一份写着「已验证」但数字是编的报告，
  比诚实地标着「未验证」糟糕得多。
- **人工结论标注**（P1.5 任务 3，可选）：决策层指标的前置输入，
  需要**人**来标，不是代码能补的。它同时是 P2.3 的前置。
- **成本指标只覆盖 Agent 路径**：`--live` 会报出解释链路的当次合计用量，
  但评测里的 `cost.*` 只统计 Agent。
- **没有向仓库提交 cassette 文件**：计划里写的是「录一份真实模型的 cassette」，
  而本机没有真实模型，用靶子录一份再改名叫「真实」属于伪造。
  现有测试采用的是**当场录制 + 当场回放**（`tmp_path`），
  既验证录制也验证回放，比提交一份静态录制更强。等真实模型跑通后，
  再把那份录制提交进 `ai_service/tests/cassettes/` 才有意义。
