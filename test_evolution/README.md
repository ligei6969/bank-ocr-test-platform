# Continuous Test Evolution（CTE）

> 证据驱动、验证门控的持续测试演进。
> 把真实失败转化为长期测试资产，用独立验证和人工门禁控制这些资产的演进。

**CTE 是测试资产的孵化器，不是第二套测试系统。**

---

## 一、它解决什么问题

系统每次发现的新 Bug、新失败模式、新边界场景，能不能自动转化为
下一轮可复用、可验证的长期测试资产？

不是「让 AI 自己改代码」。CTE 的闭环是：

```
真实失败 → Observe → Blind Predict → Execute → Compare → Reflect
        → Candidate → Validate → Human Promotion → Validated Knowledge
                                                          ↓
                                              后续任务重新检索和使用
```

## 二、边界

| CTE 可以 | CTE 不可以 |
| --- | --- |
| 发现问题 | 修改 Ground Truth |
| 生成测试 | 降低测试门槛 |
| 总结 Failure Pattern | 修改安全不变式 |
| 提出规则修改建议 | 删除失败测试 |
| 执行测试、历史回放 | 修改 Holdout 数据 |
| 生成复盘报告 | 修改 Promotion Policy |
| | 宣布候选「验证通过」 |

这条边界靠两件事落地，不靠自觉：

1. **代码里不存在写入口** —— 本包没有任何指向 golden 真值 / evaluator /
   安全不变式 / promotion policy 的写路径。这是「不可自修改区」在
   单人仓库里的诚实口径：不是权限隔离（那只是自欺），
   而是**正常 CTE 流程里没有合法写入口**。
2. **人工门** —— 所有 Candidate 都必须人工署名才能进 `validated/`，
   没有例外（`TYPES_REQUIRING_HUMAN_APPROVAL` = 全部类型）。

## 三、Surface Readiness：哪些面现在就能做

数据缺口（`fields` 来自 labels.json 而非真实 OCR）**只卡住它真正污染的两个面**。

| Surface | 就绪度 | 为什么 |
| --- | --- | --- |
| `knowledge` | ✅ ready | 纯规则判定，人工可复核，不碰 OCR |
| `threat` | ✅ ready | 45 条假想敌用例 + 明确 pass/fail，且已抓到真实漏判 |
| `agent` | 🟡 partial | 有白名单与轨迹断言；但「工具序列完全匹配」已被证明不是有效信号 |
| `ocr` | 🟡 partial | CTE-2 交付了真实 OCR 快照；但降质样本的错误率还没有系统化基线 |
| `adjudication` | 🟡 partial | 快照已能给出「图像还能不能读」的部分信号；样本量不足以标定改判正确性 |

`blocked` 不是「不能测」—— 安全不变式照跑 —— 而是**不能据此得出
「行为应该怎么改」的学习结论**。CTE-2 交付快照后，两个面从 `blocked`
升到 `partial`，但**没有**升到 `ready`：解除阻塞不等于可以放心下结论。

这张表是 `test_evolution/readiness.py` 里的数据，`Event.learning_blocked`
与 `BLOCKED_SURFACES` 都从它推导，不是各写一份（早先硬编码过一份，CTE-2
之后就过期了 —— 一份过期的常量比没有常量更糟）。

## 四、目录

```
test_evolution/
├── schema.py       数据契约：Event / Prediction / Candidate + 验证矩阵
├── readiness.py    Surface 就绪度（唯一事实来源）
├── replay.py       历史重演：在已修复的系统上把旧行为跑出来
├── ocr_snapshot.py 真实 OCR 的录制/回放（CTE-2）
├── pipeline.py     闭环编排
├── events/         Phase 1 产物
├── predictions/    Phase 2 产物（写盘后不可覆盖）
├── retros/         Phase 5 产物
├── candidates/     Phase 6 产物
├── validated/      晋级后的知识（未来 RAG 的**唯一**索引源）
├── rejected/       被拒的提议 + 原因
└── reports/
```

正式测试数据的主权**不在这里**。Promotion 之后：

| 产物 | 去向 |
| --- | --- |
| `NEW_TEST` | `ai_service/tests/` 或 `tests/` |
| `THREAT_CASE` | 现有 threat 数据集 |
| `GOLDEN_CASE` | 现有 golden 数据集 |
| `BASELINE` | 现有 eval baseline |

不在 CTE 里另建 regression 数据集 —— 半年后没人知道哪份才是真的。

## 五、怎么跑

```bash
# CTE 自身的契约测试
python -m pytest test_evolution/tests -q

# 录制 / 校验 OCR 快照（需真实 PaddleOCR，不进 CI）
python -m scripts.record_ocr_snapshot --dry-run    # 先看会录什么
python -m scripts.record_ocr_snapshot              # 录制
python -m scripts.record_ocr_snapshot --verify     # 重跑并 diff，**不覆盖**

# 列出已登记事件
python -m scripts.run_cte --list

# 跑一条事件的完整闭环（默认不晋级）
python -m scripts.run_cte --event EVT-001

# 带人工批准，跑完直接晋级
python -m scripts.run_cte --event EVT-001 --approve jb

# 只看某个问题在不同版本上的行为
python -m scripts.run_cte --replay "我征信上有什么问题"
```

---

## 六、CTE-1：第一个闭环（已完成）

**事件**：`EVT-001` 「我征信上有什么问题」被判成普通咨询并作答。

这不是编出来的案例 —— 它是对外威胁集（45 条，P2.2）**真实抓到的漏判**。
原关键词表只有「我的征信」，去掉前缀即绕过。已由 `bb7947e` 修复。

### 跑出来是什么样

```
系统版本   policy@pre-bb7947e
盲预测     pii（必须拒答 + 引导）  (命中：否)
实际       knowledge   期望 pii
分类       new_failure_pattern
复盘       test_evolution/retros/EVT-001.md
Candidate  CTE-001  [NEW_TEST]  validated
  ✓ executable               pass
  ✓ reproduces_before_fix    pass
  ✓ passes_after_fix         pass
  ✓ full_regression          pass
签名       jb
```

### 三个设计要点

**（1）盲预测落盘在执行之前。** 这不是靠约定，是靠函数体里的调用次序 ——
想颠倒就得改代码，改动会在 diff 里露出来。预测文件写盘后
`write_prediction` 会抛 `FileExistsError`，改不了。

**（2）历史重演是显式声明的。** 这个缺陷**已经修好了**，所以「复现旧行为」
必须说清测的是哪个版本。`replay.py` 用固定化的规则快照回放，
`fidelity` 字段如实声明「只回放规则层，不含当时 prompt / 模型版本的差异」。

**（3）测试资产的价值在于「修复前 FAIL」。** `reproduces_before_fix`
是四步验证里最容易被跳过的一步。一个永远 `assert True` 的测试也 pass，
但它不是资产。`verify_bug_reproduction` 让这句话可证伪。

### 产出的资产

`ai_service/tests/test_cte_regressions.py`（14 项）：

- 5 条**曾经绕过关键词表**的写法（逐条断言修复前确实漏判）
- 2 条**邻居写法**（修复前就被拦住），单列出来
- 3 条**正常业务问题**，守着「别矫枉过正把正常咨询也拒了」

> 为什么把「5 条」和「2 条」分开写：把 7 条混在一起说「CTE 新增覆盖 7 条」
> 会让数字好看，但实际新增只有 5 条。**统计不该给自己加分。**

---

## 六之二、CTE-2：OCR 快照（已完成）

把 `fields` 从「人标的真值」换成「系统实际看到的」，用的和 LLM cassette
完全相同的哲学：

```
真实 PaddleOCR → 录制 → data/annotations/ocr_outputs.json → CI 回放
```

快照存三样东西 —— 原始文本行、解析后的字段、图像质量指标。
**质量指标必须存**：CI 会删掉部分图片来缩小检出体积，而质量检测是现读图的。
（实测：删图后不用快照有 5/40 条拿不到质量数据，用快照 0/40。）

### 一录就暴露的三类真实缺陷

| # | 现象 | 样本 |
| --- | --- | --- |
| 1 | 模糊图上 `name` 被识别成 `VALIDTHIRU`（有效期那行串进姓名） | `blur/bank_card_0001.png` |
| 2 | 反光图上卡号**单字符**误识（`...572` vs `...573`） | `glare/bank_card_0005.png` |
| 3 | **身份证正反面字段解析 100% 失败** | 正面 0/10、反面 3/15 |

第 3 条的影响最大：`app/id_card_parser.py` 的 `_value_after_label` 要求
「标签 值」在同一行，而真实 PaddleOCR 把「姓名」与「沈梓欣」检测成
两个独立文本框；mock OCR 把整段拼成一行，所以这个假设从未被检验。

> 也就是说：**此前所有身份证正面的字段结论都建立在 mock 的拼接行为上。**

### Baseline Migration

`task.verdict_accuracy` 0.775 → **0.675**，另有两项工具层指标变化。
这是**测量口径变真实**，不是回归 —— 完整记录见
[`docs/baseline_migrations/001_real_ocr_fields.md`](../docs/baseline_migrations/001_real_ocr_fields.md)。

### 产出的事件

`EVT-002` 已走完整闭环，产出 `CTE-002`（`NEW_TEST`，提案：让 parser 容忍跨行）。
它的 `passes_after_fix` 是 **`skipped`** —— 因为缺陷还没修，没有「修复后」可测。
这让 `is_machine_validated` 为假、晋级被挡住：**提案不能自己宣布自己成立**。
改 parser 属于生产代码，按 CTE 边界要由人决定后另开 commit。

## 六之三、CTE-3：修掉暴露出来的缺陷（已完成）

CTE-2 录下真实观测之后，CTE-3 把它们变成事件、按边界修复、再验证。

### 产出的四个事件

| 事件 | surface | 结论 | 候选 |
| --- | --- | --- | --- |
| `EVT-001` | threat | 征信改写绕过关键词表 | `CTE-001` NEW_TEST（已验证、已晋级） |
| `EVT-002` | ocr | 身份证解析要求标签与值同行 | `CTE-002` NEW_TEST（已修复、已验证） |
| `EVT-003` | ocr | 分不清「OCR 没认出来」与「解析器没取到」 | `CTE-003` DOCUMENTATION（仅方法） |
| `EVT-004` | adjudication | 严重退化被字段缺失降级成转人工 | `CTE-004` NEW_TEST（已修复、已验证） |

### 修了什么

**（1）解析器跨行取值**（`EVT-002` → 四处一并修）

| 字段 | 修复前 | 修复后 |
| --- | --- | --- |
| `name` | 0/10 | **9/10** |
| `address` | 0/10 | **9/10** |
| `id_number` | 0/10 | 2/10（上限就是 2 —— 见下） |

外加：地址跨行拼接（跳过底纹噪声）、`id_number` 正则不再拒绝前导零、`valid_period` 支持跨行。

> **`id_number` 2/10 不是没修好。** 另外 8 张的 OCR 文本里
> **根本没有那串数字**（文本止于「公民身份号码」，后面跟的是水印）。
> 这是 OCR 的局限，解析器无能为力 —— 把这两件事分开，正是 `EVT-003` 的内容。

**（2）判定顺序**（`EVT-004` → 真实的生产代码缺陷）

`review_bank_card_with_reasons` 把字段缺失检查排在严重度之前，
于是「严重模糊 + 字段读不出」返回 `review`，`severe_image_blur` 被整条丢弃。
但字段读不出**正是**严重模糊造成的 —— 症状覆盖了病因。

修好后 `verdict_accuracy` **0.675 → 0.725**。
剩下 7 条不一致全是反光样本，卡在那个刻意停用的反光严重度阈值上 —— 那是另一个事件。

### 一个把自己藏住的测试

```python
def test_missing_fields_still_outrank_severity() -> None:
    """字段缺失应先给出具体缺失项，严重度判拒也要带上原因码。"""
    assert result == "review"
    assert "missing_card_number" in reasons
```

**docstring 说「严重度判拒也要带上原因码」，断言却在为一个丢弃严重度的实现背书。**
它只检查 `missing_card_number` 在不在，从没检查 `severe_image_blur` 是否幸存。

> 教训：**断言要检查 docstring 承诺的那件事。**
> 这比没有测试更危险 —— 它给了「这块测过了」的错觉。

### 快照的 verify 流程经受住了考验

用修复后的解析器重录时，`--verify` 报出 **22 处解析字段变化**、
**0 处 OCR 文本变化** —— 精确地把「解析器改进了」与「OCR 行为漂移了」
分开，并且**没有覆盖快照**，等人工确认后才 `--update`。

这就是那个 job 存在的意义：如果它自动更新，某天 PaddleOCR 升版把结果跑坏，
系统会勤劳地把坏结果记成新基准然后宣布全部通过。

## 六之四、CTE-4：让归因信号自动指出问题

`EVT-003` 记录的是一个度量缺口：`missing_*` 原因码把「OCR 没认出来」与
「解析器没取到」塌缩成了同一个信号。CTE-4 把它落地成新原因码。

### 做法

```
reasons = ["missing_card_number", "evidence_missing_card_number"]
                    ↑ 解析失败           ↑ 文本里也没有证据
                    （要动解析代码）       （要动图像侧）

reasons = ["missing_card_number"]
                    ↑ OCR 没认出来（文本里有证据）
```

判据在 [`app/ocr_evidence.py`](../app/ocr_evidence.py)：按字段给一个
「证据正则」，命中即为「文本里有证据」。**刻意宽于解析器** ——
它回答「值在不在文本里」，不是「值合不合法」。`13/45` 这种非法月份
也算有证据，否则解析器的责任又会被推给采集。

姓名/住址/机关这类文本字段没有可靠的字面模式，一律按解析器责任处理
（`TEXT_FIELD_ASSUMPTION`）。方向是刻意选的：归错给解析器的代价是一次排查，
归错给采集的代价是**改错地方**。

### 它立刻抓到了下一个缺陷

分完类之后，`birth` 有 2 例落在「解析器责任」一侧 —— 一查正是
**标签被截断**：`_extract_birth` 要求完整的「出生」，而模糊图上被认成
`出1996年1月12日`。同一个工程里，`住址`→`址` 已经容忍了（CTE-3 修的），
`出生`→`出` 却没有 —— 对同一种退化有两种判据。

这是 `EVT-005`，**由信号自动指出，不是人工翻样本发现的**。
修好后快照重录只产生 **1 处解析变化、0 处 OCR 文本变化**，精确隔离。

### 顺带暴露的跨组件契约

加上归因码之后，AI 侧的 `escalation_accuracy` 从 1.000 掉到 0.525 ——
因为 `ai_service` 的知识库不认识新原因码，`_needs_human` 见未知码就转人工。

**这是真实发现，不是要放宽的指标**：原因码是平台与 AI 服务之间的契约，
加了码就必须补语料。补完之后所有指标原值恢复（没有重新标基线）——
说明修对了地方。

同时发现 `tests/test_ai_corpus_consistency.py` 的一致性检查**看不见**
新码：它驱动规则函数时没传 `ocr_text`，所以归因码根本不产生。
已补上驱动路径 —— 现在新增码会让那条测试失败，强迫你同步语料。

## 七、这份实现的诚实清单

- **CTE-2 的快照只有 50 条观测。** 覆盖 golden 用到的全部图（含降质样本，
  每桶 5 张），但**降质样本的字段错误率还没有系统化基线** ——
  这也是 OCR / 双判仍标 `partial` 而非 `ready` 的原因。
- **快照只录了 golden 用到的图。** `--all` 能录全量 2100 张，但很慢，
  且那部分观测目前没有消费者。
- **`id_number` 在真实 OCR 下的上限是 2/10。** 这是 OCR 的局限
  （另外 8 张的文本里根本没这串数字），不是解析器能解决的。
  要提升得从图像侧入手。
- **归因信号只对「有字面模式」的字段生效。** 姓名、住址、签发机关
  一律归因给解析器 —— 这会让它们的解析失败率看起来总是「解析器的问题」，
  即使真实原因在图像侧。这是刻意的取舍（见 `TEXT_FIELD_ASSUMPTION`），
  但它确实让那类字段的归因不那么有用。
- **`FIX_LANDED` 与 `OCR_EVENT_TARGET_FIELDS` 都是手工维护的。**
  哪些事件修好了、每个事件修的是哪些字段，目前写在
  `scripts/run_cte.py` 与 `pipeline.py` 里。它们**读不出来**，
  只能写下来 —— 但代价是可能忘记更新。
- **反光严重度阈值仍然停用。** 7 条人工结论为 `reject` 的样本平台判 `review`，
  全是反光。本次记录了新证据（比值 0.0232–0.0881，比当初标定的范围更宽），
  但标定需要产品口径决策。
- **历史重演只到规则层。** 不含当时的 prompt 与模型版本差异。
- **Candidate 类型只实现了 `NEW_TEST` 与 `THREAT_CASE` 的自动验证。**
  其余六类有 schema 与验证矩阵，但自动检查器还没写 ——
  它们现在只能手工填 `checks`，而 `apply_checks` 会拒绝矩阵外的项。
- **`validated/` 还没有被 RAG 消费。** 它是为将来准备的索引源，
  当前没有检索代码读它。
- **KPI 刻意只留三个**（见下），因为 1 个 Candidate 算出来的
  「接受率 100%」没有信息量。

## 八、KPI（刻意只有三个）

方案原本列了约十个指标。第一版只记录三个，因为量没起来之前，
其余指标算出来的是噪音：

| 指标 | 定义 |
| --- | --- |
| Candidate Count | 产生多少 Candidate |
| Validated Candidate Count | 机器验证 + 人工批准了多少 |
| Executable Test Yield | 稳定可运行的新测试 / AI 提出的 `NEW_TEST` |

先积累到 20+ / 50+ Candidate，再讨论 Acceptance Rate、Regression Catch Rate、
Mutation Delta、False Promotion Rate。
