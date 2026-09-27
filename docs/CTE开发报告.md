# CTE 开发报告（CTE-0 骨架 + CTE-1 首个闭环 + CTE-2 OCR 快照）

> 阶段：P5 Continuous Test Evolution（CTE-0 骨架 + CTE-1 首个闭环 + CTE-2 OCR 快照）
> 日期：2026-09-27
> 前置文档：[`p5计划.md`](p5计划.md)（初版方案）、[`p5修订md`](p5修订md)（吸收审计后的修订版）
> 测试基线：**1052 → 1164 passed / 0 failed**（新增 112 项，零回归）

---

## 一、这一阶段做了什么

CTE-0 与 CTE-1 合并交付 —— 因为骨架的每一部分都由第一个闭环的需求倒推出来，
分开做会先造一批用不上的抽象。

| 阶段 | 内容 | 产物 |
| --- | --- | --- |
| **CTE-0** | Foundation | `test_evolution/` 骨架、Event/Prediction/Candidate schema、按类型的验证矩阵、Surface Readiness、Skill |
| **CTE-1** | Knowledge/Threat 面的第一个闭环 | 用「我征信上有什么问题」跑通全链路，晋级出一条回归资产 |

**没有等真实 OCR 数据。** 这是修订版方案最重要的修正：数据缺口
（`fields` 来自 labels.json 而非真实 OCR）只污染 OCR 与双判两个面，
而 CTE-1 的案例落在 threat 面 —— 它的 Ground Truth 是确定性的关键词判定，
根本不碰 OCR。让整条 CTE 去等一个与它无关的前置条件，是初版的排序错误。

---

## 二、交付物清单

### 新增模块

```
test_evolution/
├── __init__.py
├── schema.py        数据契约：Event / Prediction / Candidate + 验证矩阵 + 读写
├── readiness.py     Surface 就绪度（唯一事实来源）
├── replay.py        历史重演（固定化规则快照）
├── ocr_snapshot.py  真实 OCR 的录制/回放（CTE-2）
├── pipeline.py      闭环编排
├── README.md        设计说明与诚实清单
└── tests/
    ├── test_schema.py        契约测试
    ├── test_replay.py        重演测试
    ├── test_pipeline.py      闭环测试
    ├── test_readiness.py     就绪度测试
    └── test_ocr_snapshot.py  快照/回放测试

.claude/skills/bankocr-test-evolution/SKILL.md   流程控制器
scripts/run_cte.py                               CTE 闭环 CLI 入口
scripts/record_ocr_snapshot.py                   OCR 快照录制/校验（不进 CI）
ai_service/tests/test_cte_regressions.py         晋级出的回归资产（14 项）
data/annotations/ocr_outputs.json                真实 PaddleOCR 快照（50 条观测）
docs/baseline_migrations/001_real_ocr_fields.md  基线口径变更记录
```

### 生成的过程证据（已入库，是审计轨迹）

```
test_evolution/events/EVT-001.json           事件（征信漏拒）
test_evolution/events/EVT-002.json           事件（身份证跨行解析）
test_evolution/predictions/PRED-EVT-00*.json 盲预测（执行前落盘）
test_evolution/retros/EVT-00*.md             复盘
test_evolution/candidates/CTE-00*.json       候选
test_evolution/validated/CTE-001.md          晋级后的知识
```

---

## 三、关键设计决定

### 3.1 盲预测的不可覆盖性是代码保证，不是文档约定

方案说「Prediction 保存后不允许覆盖」。落地方式：
`write_prediction` 在文件已存在时抛 `FileExistsError`，
`Prediction.record_outcome` 在已闭合时抛 `SchemaError`。

顺序也是代码保证的：`run_event_loop` 的函数体里先调
`blind_predict` 再调 `execute`，想颠倒就得改这段代码，改动会在 diff 里露出来。

### 3.2 历史重演用固定快照，而不是 git checkout

CTE-1 的案例**已经修好了**（`bb7947e`）。「复现旧行为」必须显式声明测的是哪个版本 ——
否则 retro 里那句「复现了漏拒」是无法证伪的断言。

三种机制里选了固定化规则快照（`POLICY_HISTORY`），放弃了 `git worktree`：

- 快、确定、不依赖工作区状态；
- fixture 里存的就是当时那份过窄的关键词表，与当前规则 diff 一下就能看出修复加了什么；
- 代价是只回放规则层 —— 这个限制写在 `ReplayResult.fidelity` 字段里，
  不假装它是完整的历史重演。

### 3.3 每一类 Candidate 都需要人工批准（包括没写 human_review 的那些）

初版 `VALIDATION_MATRIX` 里 `THREAT_CASE` / `NEW_TEST` 这类没有 `human_review` 项，
于是 `can_promote` 对它们返回 `True` —— **机器验证通过就自动晋级了**。

这直接违反了方案第十一节条件 6（不存在 `machine_validated → production`
的直达路径）。已修正：`needs_human_review` 对所有类型为真，
依据是单独的常量 `TYPES_REQUIRING_HUMAN_APPROVAL`（将来若真有例外，
改动会落在那一行上，评审时一眼能看见）。

测试 `test_every_candidate_type_requires_human_approval` 遍历所有类型守着这条。

### 3.4 「复现缺陷」的判据不能只看旧版本不拒答

初版 `verify_bug_reproduction` 用 `not before.out_of_scope` 判定「复现了」。
问题是：**一句正常业务问题在任何版本都不拒答**，于是它也会被判定成
「复现了缺陷」。测试抓到了这个（`test_a_normal_business_question_is_not_a_reproduction`）。

修正后的判据把当前版本的行为作为「本应如何」：
**当前拒答 + 旧版本放行**才叫复现。

### 3.5 `write_validated` 不能复用「此刻能否晋级」的判断

`promote()` 会把状态改成 `validated`，而 `can_promote` 在状态是
`validated` 时返回 `False` —— 于是 `promote()` 之后的 `write_validated()`
必然失败，`validated/` 永远是空的。

拆成两个属性：

- `is_elevated` —— **晋级条件**（机器验证 + 署名 + 非 proposal-only），不看当前状态；
- `can_promote` —— **此刻能否执行晋级动作**（= `is_elevated` 且未被处理过）。

### 3.6 统计不给自己加分

晋级的回归资产里，7 条征信写法中只有 **5 条**在修复前会漏判，
另 2 条（`帮我查一下我的征信报告`、`信用报告`）修复前就被拦住了。

把 7 条混在一起说「CTE 新增覆盖 7 条」会让数字好看。所以代码里
分成 `BYPASSING_PHRASINGS`（5 条）与 `ALREADY_REFUSED_PHRASINGS`（2 条）
两个常量，并各有一条测试断言这个区分是真的：

```python
def test_the_already_refused_phrasings_were_not_new_coverage():
    for question in ALREADY_REFUSED_PHRASINGS:
        assert verify_bug_reproduction(question)["reproduced_before"] is False
```

---

## 四、跑出来的结果

```
$ python -m scripts.run_cte --event EVT-001 --approve jb

事件       EVT-001  征信问句的改写绕过越界关键词表
系统版本   policy@pre-bb7947e
盲预测     pii（必须拒答 + 引导）  (命中：否)
实际       knowledge   期望 pii（必须拒答 + 引导）
分类       new_failure_pattern
复盘       test_evolution/retros/EVT-001.md
------------------------------------------------------------------------------
Candidate  CTE-001  [NEW_TEST]  validated
  ✓ executable               pass
  ✓ reproduces_before_fix    pass
  ✓ passes_after_fix         pass
  ✓ full_regression          pass
机器验证   True
需人工批准 True
签名       jb
```

---

## 四之二、CTE-2：OCR 快照（同批交付）

与 CTE-1 并行的那条主线。目标：**让评测的 `fields` 来自真实 OCR 输出**，
从而解除 OCR / 双判两个面的数据缺口。

### 做法

和 LLM cassette 同构的录制回放：

```
真实 PaddleOCR → 录制 → data/annotations/ocr_outputs.json → CI 回放
```

快照存三样：原始文本行、解析后的字段、**图像质量指标**。
质量指标必须存是因为 CI 会删图，而质量检测（`platform_rules.compute_quality`）
是现读图的。

### 顺带修掉的 CI 暗坑

审计时发现 `tests.yml` 的清理步骤删掉每类第 4 张及之后的图，
而 golden 的 `_balanced_take` 每桶取 4 张 —— 于是 CI 里那 5 条 `*-03`
样本的质量检测走 `path.is_file()` 失败，返回 `quality_check_unavailable`。
门禁当时没红只是被容忍度吃掉了。

**实测验证**：把 `bank_card_*_0004/0005.png` 移走后，
不用快照 5/40 条拿不到质量数据，用快照 **0/40**。
（`test_snapshot_survives_the_ci_image_cleanup` 固化这个场景。）

### 录制脚本踩的坑（值得记下来）

初版录制脚本自己按桶排序取前 N 张，取到 `back/blur/0001..0004`；
而 golden 的 `_balanced_take` 是**按面轮转**的，要的是
`back/blur/0001` 与 `front/blur/0001`。两套选取策略 → 40 条里 10 条未命中。

修法是让录制直接复用 `build_golden_set`：**「录什么」由「评测用什么」决定**，
不该各写一份。`test_recorder_targets_cover_every_golden_image` 守住这个耦合。

### 一录就暴露的三类真实缺陷

| # | 现象 | 样本 |
| --- | --- | --- |
| 1 | 模糊图上 `name` 被识别成 `VALIDTHIRU`（有效期那行串进姓名） | `blur/bank_card_0001.png` |
| 2 | 反光图上卡号**单字符**误识（`...572` vs `...573`）—— 正是双判该抓的那类 | `glare/bank_card_0005.png` |
| 3 | **身份证字段解析 100% 失败**：正面 `id_number` 0/10，反面全字段 3/15 | 全部 id_card |

第 3 条的根因：`app/id_card_parser.py` 的 `_value_after_label` 要求
「标签 值」同行，而真实 PaddleOCR 把「姓名」与「沈梓欣」检测成两个独立
文本框（`['姓名', '沈梓欣', '性别女', ...]`）。mock 把整段拼成一行，
所以这个假设从未被检验 —— **此前所有身份证正面的字段结论都建立在
mock 的拼接行为上**。

### Baseline Migration

| 指标 | 口径 A（标注真值） | 口径 B（真实 OCR） | 变化 |
| --- | --- | --- | --- |
| `task.verdict_accuracy` | 0.775 | **0.675** | −12.90% |
| `tools.sequence_accuracy` | 0.950 | 0.900 | −5.26% |
| `tools.avg_steps` | 2.750 | 2.900 | +5.45% |

这是**测量口径变真实**，不是回归。记录见
[`baseline_migrations/001_real_ocr_fields.md`](baseline_migrations/001_real_ocr_fields.md)，
并已按流程显式重置基线（不是自动更新）。

### 产出的事件

`EVT-002` 走完整闭环 → 提案 `CTE-002`（`NEW_TEST`）。
关键设计：它的 `passes_after_fix` 是 **`skipped`** —— 缺陷还没修，
没有「修复后」可测。这让 `is_machine_validated` 为假、`can_promote` 为假，
**即便有人签名也晋级不了**。CTE 只提交提案，改 parser 由人决定。

---

## 五、Surface Readiness（方案第九节的落地）

| Surface | 就绪度 | 为什么 |
| --- | --- | --- |
| `knowledge` | ✅ ready | 纯规则判定，人工可复核，不碰 OCR |
| `threat` | ✅ ready | 45 条假想敌用例 + 明确 pass/fail，且已抓到真实漏判 |
| `agent` | 🟡 partial | 有白名单与轨迹断言；但「工具序列完全匹配」已被证明不是有效信号 |
| `ocr` | 🟡 partial | CTE-2 已交付快照；但降质样本错误率还没有系统化基线 |
| `adjudication` | 🟡 partial | 快照已能给出「图像还能不能读」的部分信号；样本量不足以标定改判正确性 |

CTE-2 之后没有 `blocked` 的面了。但**解除阻塞 ≠ 可以放心下结论**，
所以两个面标 `partial` 而非 `ready` —— 升成 ready 会让它们在报告里
显得和 knowledge 面一样可靠，那是不实的。

`learning_allowed()` 是 `Event.learning_blocked` 与 `BLOCKED_SURFACES`
的唯一依据。此前 `BLOCKED_SURFACES` 是硬编码的 `{"ocr", "adjudication"}`，
CTE-2 之后就过期了 —— 一份过期的常量比没有常量更糟
（它会让事件被无理由地拒绝），已改为从就绪度表推导。

---

## 六、遗留项（诚实清单）

| # | 遗留 | 说明 |
| --- | --- | --- |
| 1 | **EVT-002 暴露的解析缺陷没有修** | 身份证跨行取值。改 `app/id_card_parser.py` 是生产代码变更，按 CTE 边界须由人决定后另开 commit；CTE 只提交了提案 `CTE-002` |
| 2 | **OCR 快照只有 50 条观测** | 覆盖 golden 用到的全部图（含降质样本，每桶 5 张），但降质样本的字段错误率还没有系统化基线 |
| 3 | **快照只录了 golden 用到的图** | `--all` 能录全量 2100 张，但很慢，且那部分观测目前没有消费者 |
| 4 | **历史重演只到规则层** | 不含当时的 prompt 与模型版本差异 |
| 5 | **只实现了 2 类 Candidate 的自动验证** | `NEW_TEST` 与 `THREAT_CASE`；其余六类有 schema 与验证矩阵，但检查器要手工填 `checks` |
| 6 | **`validated/` 还没被 RAG 消费** | 它是为将来准备的索引源，当前没有检索代码读它 |
| 7 | **KPI 只留了三个** | Candidate Count / Validated Count / Executable Test Yield。1 个 Candidate 算出来的「接受率 100%」没有信息量 |

### 下一步（CTE-3）

把 CTE-2 暴露的三条缺陷（尤其身份证解析）转成事件走完闭环，
并在修复后重录快照、再走一次 Baseline Migration。
在此之前**不应继续调双判参数** —— 输入本身还有已知缺陷，调了也无法归因。

---

## 七、基线对照

| 项 | 基线 | 当前 |
| --- | --- | --- |
| 全量测试 | 1052 passed | **1164 passed** |
| CTE 自身测试 | — | 98 |
| 晋级回归资产 | — | 14 |
| `task.verdict_accuracy` | 0.775（标注真值口径） | **0.675**（真实 OCR 口径，见迁移记录） |
| 破坏既有接口 | — | 无（`app/` 一行未动） |
| CI 离线可跑 | 是 | 是（新增测试全部离线；真实 OCR 只在独立 job） |

### 提交

`467098a` — CTE-0 + CTE-1
（CTE-2 见同批后续 commit）
