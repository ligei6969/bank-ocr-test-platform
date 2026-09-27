---
name: bankocr-test-evolution
description: 把一次真实失败（bug / 漏拒 / 边界案例 / OCR 错误）转化为可复用的长期测试资产的闭环流程。当需要处理一个具体的历史缺陷、判断该补什么测试、或验证一个测试改进提议是否成立时使用。
---

# BankOCR Continuous Test Evolution

CTE 是**测试资产的孵化器**，不是第二套测试系统。

它规定的流程回答一个问题：**系统每次发现的新 Bug、新失败模式、新边界场景，
能不能转化为下一轮可复用、可验证的长期测试资产？**

## 边界（先读这一节，它优先于后面所有步骤）

CTE **可以**：发现问题、生成测试、总结失败模式、提出规则修改建议、
执行测试、历史回放、生成复盘报告。

CTE **不可以**：修改 Ground Truth、降低测试门槛、修改安全不变式、
删除失败测试、修改 Holdout 数据、修改 Promotion Policy、改变生产规则、
宣布候选「验证通过」。

这条边界不靠自觉，靠两件事：

1. **代码里不存在写入口** —— `test_evolution/` 没有任何指向
   golden 真值 / evaluator / 安全不变式 / promotion policy 的写路径。
2. **人工门** —— 所有 Candidate 都必须在人签字后才能进 `validated/`，
   没有例外（`TYPES_REQUIRING_HUMAN_APPROVAL` = 全部）。

## 流程

```
Event → Blind Predict → Execute → Compare → Reflect → Candidate → Validate → Promote
```

每一步都有产物落在 `test_evolution/<阶段>/`。**顺序是硬的** ——
预测必须在执行之前落盘，落盘后不可覆盖。

### 1. Observe

收集一个真实事件，写进 `test_evolution/events/<EVT-ID>.json`。

必填 `surface`（knowledge / threat / agent / ocr / adjudication）与
`system_version`（事件发生时系统长什么样）。

> **`system_version` 不是形式字段。** 如果这个缺陷已经被修好了（多数都是），
> 「复现旧行为」必须显式声明测的是哪个版本，否则 retro 里那句「复现了漏拒」
> 就是一句不可证伪的断言。

### 2. Blind Predict

在**读取实际结果之前**记录预测。这一步的意义是防止事后声称「我一开始就知道」。
预测文件写盘后不可覆盖。

### 3. Execute

在指定版本上真的把行为跑出来 —— 调用项目真实的判定逻辑，
**不要为 CTE 另写一套**，否则测的就不是系统了。

历史版本走 `test_evolution/replay.py`。它用固定化的规则快照回放旧行为，
`fidelity` 字段如实声明「只回放规则层」。

### 4. Compare

三方对比：Prediction vs Actual vs Ground Truth。
分类落在 `COMPARISON_CLASSES` 这个闭集里。

### 5. Reflect

**不是每个事件都配得上一份复盘。** 只在预测错误、测试失败、
Ground Truth 与系统不一致、出现新 Failure Pattern、回归时触发。

复盘要回答六件事：发生了什么 / 为什么现有测试没发现 /
归因（数据 or 规则 or 模型 or 测试）/ 现有测试缺口 /
是否已有类似历史案例 / 应该补什么测试资产。

### 6. Generate Candidate

Reflection **不能直接进知识库**，必须转成 Candidate。

`evidence` 非空是硬校验 —— 没有证据支持的提议不得生成 Candidate
（这是 Candidate 数量爆炸的解药）。

### 7. Validate

**按类型验证，不是一套流程套所有**。见 `VALIDATION_MATRIX`。

`NEW_TEST` 的关键判据是四步，其中第 2 步最容易被跳过：

1. 测试本身能运行
2. **在修复前的版本上，测试 FAIL** ← 没有这一步，一个永远 `assert True`
   的测试也能 pass，但它不是资产
3. 在修复后的版本上，测试 PASS
4. 全量回归 PASS

### 8. Promote

机器验证 + **人工署名**，两条都满足才写进 `validated/`。

`validated/` 是未来 RAG 的唯一索引源 ——
`retros/` / `candidates/` / `rejected/` 一律不可索引，
因为它们包含未验证猜测、错误归因和 Agent 幻觉。

## 被数据缺口卡住的面

`ocr` 与 `adjudication` 两个 surface 在 CTE-2（真实 OCR 字段）完成前
**只能记录事件，不能产出学习结论**。理由不是「不能测」—— 安全不变式照跑 ——
而是不能从当前数据得出「双判行为应该怎么改」的学习结论：
评测集里 `fields` 是标注真值而非 OCR 实际输出，「字段全部解析成功」恒成立，
这个信号没有区分度。

`Event.learning_blocked` 会自报这个状态。

## 产出物归属

CTE **不拥有正式测试数据主权**。Promotion 之后：

| 产物 | 去向 |
| --- | --- |
| `NEW_TEST` | `tests/` |
| `THREAT_CASE` | 现有 threat 数据集 |
| `GOLDEN_CASE` | 现有 golden 数据集 |
| `BASELINE` | 现有 eval baseline |

不要在 CTE 里另建一套 regression 数据集 —— 半年后没人知道哪份才是真的。

## 怎么跑

```bash
python -m pytest test_evolution/tests -q     # CTE 自身的契约测试
python -m scripts.run_cte --event EVT-001    # 跑一条事件的完整闭环
```
