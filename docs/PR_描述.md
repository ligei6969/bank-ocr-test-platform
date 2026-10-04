# Pull Request 描述（可直接粘贴）

> 分支：`feature/user-admin-portal` → `main`
> 提交数：40+ ｜ 测试：**1534 passed / 0 failed** ｜ eval 门禁：exit 0

---

## 标题

```
feat: add the AI capability layer and Continuous Test Evolution (P1–P5)
```

## 正文

```markdown
在确定性的审核链路（OCR → 质量检测 → 字段解析 → 规则 → 落库）之上，
加了**一个会调工具、多步决策、输出不确定的 AI 系统，以及它的测试方法论**。

平台部分本身用传统接口测试就够；这个分支真正想回答的是后一个问题。

## 交付的五件事

| 阶段 | 内容 | 关键产物 |
| --- | --- | --- |
| P1 | 审核 Agent：4 个白名单工具、三重预算、转人工硬规则（两条路径都执行） | `ai_service/agent.py` |
| P2.1–2.3 | 知识客服 Agent（两道闸门）+ 对外化评估 + 规则/LLM 双判 | `ai_service/knowledge/`、`app/adjudication.py` |
| P3–P4b | `/metrics` + Z-score 告警；多轮会话；MCP stdio 接入 | `ai_service/metrics.py`、`knowledge/session.py`、`mcp_server.py` |
| P5 / CTE | 失败 → 测试资产的演进闭环 | `test_evolution/` |

## 最后收口

- 完成 P4b MCP server，只暴露 `review_explain` / `knowledge_ask` 两个窄工具；
  默认 stdio，不新增未认证网络入口；审核输入先脱敏，客服输出用白名单投影去掉内部审计字段。
- 补齐 5 条公开业务知识缺口，对外 45 条评估的语料未覆盖率
  **33.3% → 0.0%**，同时保持漏拒率、误拒率、内部信息泄露均为 0。
- 修复 CTE 在 Windows 仓库外临时目录记录盘符的问题，Validated Knowledge
  的 `produced_asset` 始终是可移植相对路径。
- pytest 每次使用独立临时根目录，避免中断或提权运行遗留的 ACL 污染后续验收。

## P5（CTE）是这次的重点

**它是测试资产的孵化器，不是第二套测试系统。** 边界写死在代码里：

| CTE 可以 | CTE 不可以 |
| --- | --- |
| 发现问题、生成测试、总结 Failure Pattern | 修改 Ground Truth / Evaluator |
| 提出规则修改建议、历史回放 | 修改安全不变式 / 删除失败测试 |
| 生成复盘报告 | 宣布候选「验证通过」 |

落地方式不是文件权限（单人仓库里那只是自欺），而是
**代码里不存在指向这些资产的写路径** + **所有 Candidate 都必须人工署名才能晋级**。

### 闭环实跑（六个事件，四条晋级资产）

```
Event → Blind Predict → Execute → Compare → Reflect → Candidate → Validate → 人工签名 → Validated
```

| 事件 | 结论 | 状态 |
| --- | --- | --- |
| EVT-001 | 征信改写绕过越界关键词表 | validated |
| EVT-002 | 身份证解析要求标签与值同行 | validated |
| EVT-003 | 分不清「OCR 没认出来」与「解析器没取到」 | candidate（未署名） |
| EVT-004 | 严重退化被字段缺失降级成转人工 | validated |
| EVT-005 | 出生标签被截断导致日期解析失败 | validated |
| EVT-006 | 银行卡模糊姓名被认成有效期标签 | 已修复、机器验证通过；CTE-006 待人工署名 |

### 三个设计要点

**盲预测在执行前落盘，且不可覆盖。** 不是靠约定，是靠函数体里的调用次序
加 `FileExistsError`。目的是防止事后声称「我一开始就知道」。

**回归测试的价值在于「修复前 FAIL」。** 一个永远 `assert True` 的测试同样 pass，
但它不是资产。`reproduces_before_fix` 让这句话可证伪。

**没有修复版本时如实标 `skipped`。** EVT-002 提出时缺陷还没修，
该步标 `skipped` 而非假称 pass —— 这让提案无法自己晋级，即便有人签名。

## CTE-2：把评测的字段输入换成真实 OCR 观测

此前评测的 `fields` 来自 `labels.json` 的**标注真值**，
所以「字段全部解析成功」恒成立、这个信号没有区分度。

改成真实 PaddleOCR 的录制回放（与 LLM cassette 同构）：

```
真实 PaddleOCR → 录制 → data/annotations/ocr_outputs.json → CI 回放
```

**一录就暴露三类此前完全不可见的缺陷**：

1. 模糊图上 `name` 被识别成 `VALIDTHIRU`（有效期那行串进姓名）
2. 反光图上卡号**单字符**误识（`...572` vs `...573`）
3. **身份证字段解析 100% 失败** —— `id_card_parser` 要求「标签 值」同行，
   而真实 PaddleOCR 把它们检测成两个独立文本框；mock 把整段拼成一行，
   所以这个假设从未被检验。**此前所有身份证正面的字段结论都建立在 mock 的拼接行为上。**

顺带修掉一个 CI 暗坑：清理步骤删掉每类第 4 张及之后的图，
而 golden 每桶取 4 张 —— 实测移走图后有 5/40 条拿不到质量数据，
用快照 **0/40**。

## CTE-5：全量 2100 张的字段错误率基线

每桶 5 张时一张图就是 20 个百分点，模式全被噪声淹没。扩到每桶 100 张后：

| 发现 | 证据 | 状态 |
| --- | --- | --- |
| 身份证姓名的**值在标签之前** | 316/700 张；`name` 52/100 → **97/100** | 已修 |
| 号码与相邻行数字粘连导致漏取 | 住址行尾 `...215` + 号码行 | 已修 |
| 银行卡模糊姓名被认成 `VALID THIRU` | 48%，31% 解析器责任 | EVT-006 已修；当前模糊姓名 55% |

三条结论（完整见 `docs/ocr_field_error_rates.md`）：

- **银行卡只有 blur 是真退化**：其余桶 85% 以上，blur 掉到 61%
- **身份证基线本身就低**（正常样本 73.4%）：由两个**在未退化样本上就 0/100**
  的字段主导 —— 成像问题（号码区被水印覆盖），**不是模型能力问题**
- **反直觉**：blur 反而是身份证号唯一能认出来的桶 ——
  模糊把标签与号码两个文本框糊成了一行

## 过程中修掉的自有问题（每个都有测试守着）

- 每一类 Candidate 都需人工批准 —— 初版验证矩阵让 `NEW_TEST`/`THREAT_CASE`
  机器过关就自动晋级，违反「不存在 machine_validated → production 直达路径」
- 「复现缺陷」的判据过宽 —— 初版把「旧版本不拒答」就算复现，
  于是一句正常业务问题也被判成「抓到了 bug」
- `write_validated` 复用了「此刻能否晋级」的判断，导致 `validated/` 永远是空的
- 严重度被字段缺失降级 —— `review_bank_card_with_reasons` 把字段缺失检查
  排在严重度之前，`severe_image_blur` 被整条丢弃；修好后
  `verdict_accuracy` 0.675 → 0.725
- **一条自称「严重度优先」的测试，断言却在为一个丢弃严重度的实现背书**
- 复盘模板写死了 EVT-001 的故事，导致每份复盘都印着别人的归因
- 长任务录制崩了会丢掉全部成果 —— 改为边录边写 + 断点续录

## 基线变化（三份迁移记录）

| # | 变化 | 性质 |
| --- | --- | --- |
| 001 | `fields` 改为真实 OCR 观测；`verdict_accuracy` 0.775 → 0.675 | **口径变真实**，非回归 |
| 002 | 严重度优先；0.675 → **0.725** | 真实缺陷修复 |
| 003 | 快照扩到全量 2100 张 | 样本量 |

## 测试与门禁

- **1534 passed / 0 failed**（原基线 1052，当前增加 482）
- 全部离线可跑，不需要 API key；CI 不加载 PaddleOCR
- eval 回归门禁（退化 >5% 打红）exit 0
- 真实 PaddleOCR 校验是独立 opt-in job：有差异就报警，**绝不自动覆盖快照**
- `test_evidence_consistency.py` 机械化核对「记录与代码是否一致」

## 已知边界（写在文档里而不是藏起来）

- `EVT-006` 标签误选已修复；模糊姓名 48% → 55%，残余 OCR 漏字/粘连仍在
- 反光严重度阈值仍停用 —— **需产品口径决策**，CTE-5 已提供依据
- `id_number` / `valid_period` 在合成集上接近零识别 —— 成像问题，非模型能力
- 快照只覆盖当前 2100 张合成图，不代表真实证件分布
- 决策层指标样本量小（40 条），只作趋势参考

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

## 开 PR 的命令

`gh` 当前未登录。任选一种：

```bash
gh auth login
```

然后：

```bash
gh pr create --base main --head feature/user-admin-portal --title "feat: add the AI capability layer and Continuous Test Evolution (P1–P5)" --body-file docs/PR_描述.md
```

或者直接在浏览器打开：

https://github.com/ligei6969/bank-ocr-test-platform/compare/main...feature/user-admin-portal


本次审查修复与最新验收口径见 [审查问题修复验证](审查问题修复验证.md)。离线基线通过不代表真实 AI 服务可用。
