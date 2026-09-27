# CTE 开发报告（CTE-0 骨架 + CTE-1 第一个闭环）

> 阶段：P5 Continuous Test Evolution
> 日期：2026-09-27
> 前置文档：[`p5计划.md`](p5计划.md)（初版方案）、[`p5修订md`](p5修订md)（吸收审计后的修订版）
> 测试基线：**1052 → 1134 passed / 0 failed**（新增 82 项，零回归）

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
├── schema.py       数据契约：Event / Prediction / Candidate + 验证矩阵 + 读写
├── readiness.py    Surface 就绪度（唯一事实来源）
├── replay.py       历史重演（固定化规则快照）
├── pipeline.py     闭环编排
├── README.md       设计说明与诚实清单
└── tests/
    ├── test_schema.py      契约测试（23 项）
    ├── test_replay.py      重演测试（13 项）
    ├── test_pipeline.py    闭环测试（22 项）
    └── test_readiness.py   就绪度测试（10 项）

.claude/skills/bankocr-test-evolution/SKILL.md   流程控制器
scripts/run_cte.py                               CLI 入口
ai_service/tests/test_cte_regressions.py         晋级出的回归资产（14 项）
```

### 生成的过程证据（已入库，是审计轨迹）

```
test_evolution/events/EVT-001.json          事件
test_evolution/predictions/PRED-EVT-001.json 盲预测（执行前落盘）
test_evolution/retros/EVT-001.md            复盘
test_evolution/candidates/CTE-001.json      候选
test_evolution/validated/CTE-001.md         晋级后的知识
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

## 五、Surface Readiness（方案第九节的落地）

| Surface | 就绪度 | 为什么 |
| --- | --- | --- |
| `knowledge` | ✅ ready | 纯规则判定，人工可复核，不碰 OCR |
| `threat` | ✅ ready | 45 条假想敌用例 + 明确 pass/fail，且已抓到真实漏判 |
| `agent` | 🟡 partial | 有白名单与轨迹断言；但「工具序列完全匹配」已被证明不是有效信号 |
| `ocr` | ⛔ blocked | 评测集没有 OCR 实际输出，「字段解析成功」恒成立 |
| `adjudication` | ⛔ blocked | 双判正确性需要「图像还能不能读」，当前评测集答不了 |

`learning_allowed()` 是 `Event.learning_blocked` 的唯一依据 ——
就绪度只有一份事实来源，不会各说各话。

---

## 六、遗留项（诚实清单）

| # | 遗留 | 说明 |
| --- | --- | --- |
| 1 | **CTE-1 只覆盖了 Knowledge / Threat 面** | OCR 与双判面被数据缺口卡住，不是没做，是做了也不可信 |
| 2 | **历史重演只到规则层** | 不含当时的 prompt 与模型版本差异 |
| 3 | **只实现了 2 类 Candidate 的自动验证** | `NEW_TEST` 与 `THREAT_CASE`；其余六类有 schema 与验证矩阵，但检查器要手工填 `checks` |
| 4 | **`validated/` 还没被 RAG 消费** | 它是为将来准备的索引源，当前没有检索代码读它 |
| 5 | **单一事件** | 一个闭环跑通不等于机制在大样本上成立 |
| 6 | **KPI 只留了三个** | Candidate Count / Validated Count / Executable Test Yield。1 个 Candidate 算出来的「接受率 100%」没有信息量 |

### 下一步（CTE-2，与本文并行）

按修订版方案第二十一条，CTE-2 是另一条独立的主线：
**为现有 Golden 数据建立 PaddleOCR record/replay 快照**，让 CI 消费
真实 OCR 派生字段而不实时依赖 PaddleOCR。

审计时确认的具体问题（写在这里备查）：

- 快照 schema 必须同时存 `quality` 指标 —— 否则 CI 里图片不在，
  质量检测拿不到输入；
- **当前 CI 已有一个暗坑**：`tests.yml` 的清理步骤删掉每类 0004 及之后的图，
  而 golden 集的 `*-03` 五条样本正好落在 `bank_card_0004.png` 上，
  于是 CI 里这五条的 `compute_quality` 走 `path.is_file()` 失败。
  门禁没红只是被容忍度吃掉了（`golden.py:104` 的 `fields` 现在不读图，
  但 `platform_rules.py:79` 的 `compute_quality` 读）；
- 字段名用平台的 `valid_date`，不要发明 `expiry`；
- 快照**不要**脱敏卡号 —— 脱敏策略管日志与 LLM 载荷，不管仓内评测数据，
  存脱敏值会让规则引擎拿不到卡号，`invalid_card_number` 永远触发。

---

## 七、基线对照

| 项 | 基线 | 当前 |
| --- | --- | --- |
| 全量测试 | 1052 passed | **1134 passed** |
| CTE 自身测试 | — | 68 |
| 晋级回归资产 | — | 14 |
| 破坏既有接口 | — | 无（`app/` 一行未动） |
| CI 离线可跑 | 是 | 是（新增测试全部离线） |
