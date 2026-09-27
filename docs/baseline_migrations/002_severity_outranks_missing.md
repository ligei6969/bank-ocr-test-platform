# Baseline Migration 002：严重退化压过字段缺失

> 日期：2026-09-27
> 相关：CTE-3、`app/rule_check.py`、`EVT-004`、`CTE-004`
> 门禁变化：`task.verdict_accuracy` 0.675 → **0.725**（改善，非退化）

## 为什么 baseline 变了

**这是一次真实的缺陷修复，不是测量口径变化** —— 与 001 号迁移相反。

`review_bank_card_with_reasons` 把字段缺失检查排在严重度检查**之前**：

```python
if missing_reasons:                      # ← 先返回
    return "review", missing_reasons + quality_reasons
...
severe_reasons = quality.get("severe_reasons") or []
if severe_reasons:
    return "reject", ...                 # ← 永远到不了
```

于是「严重模糊 + 字段读不出」返回 `review`，而 `severe_image_blur`
被**整条丢弃**。但字段读不出**正是**严重模糊造成的 —— 症状覆盖了病因。

## 指标影响

| 指标 | 修复前 | 修复后 | 说明 |
| --- | --- | --- | --- |
| `task.verdict_accuracy` | 0.675 | **0.725** | 9 条该拒的样本中修好 2 条 |
| `tools.sequence_accuracy` | 0.900 | 0.900 | 不变 |
| `tools.avg_steps` | 2.900 | 2.900 | 不变 |

## 为什么只修好 2 条，剩下 7 条还是不一致

剩下 7 条**全部是反光样本**，卡在另一个问题上：

| 样本 | glare_component_ratio |
| --- | --- |
| `golden-bank_card-glare-02` | 0.0234 |
| `golden-bank_card-glare-03` | 0.0232 |
| `golden-bank_card-bright-03` | 0.0301 |
| `golden-id_card-glare-01` | 0.0489 |
| `golden-id_card-glare-03` | 0.0617 |
| `golden-id_card-glare-00` | 0.0720 |
| `golden-id_card-glare-02` | 0.0881 |

反光的严重度阈值是**刻意停用**的（`SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD = None`），
因为当初的标定间隙只有 9%（最高「该复核」0.0213 vs 最低「该拒绝」0.0232）——
据此判拒绝属于过拟合。

**本次记录提供了一个新证据**：这批样本的比值分布在 0.0232–0.0881，
比当初标定用的范围更宽、更连续。样本量足够时可以重新标定该阈值。

## 那个把它藏住的测试

`tests/test_rule_check.py` 里有一条：

```python
def test_missing_fields_still_outrank_severity() -> None:
    """字段缺失应先给出具体缺失项，严重度判拒也要带上原因码。"""
    ...
    assert result == "review"
    assert "missing_card_number" in reasons
```

**docstring 说「严重度判拒也要带上原因码」，断言却在为一个丢掉严重度的实现背书。**
它只检查 `missing_card_number` 在不在，从没检查 `severe_image_blur` 是否幸存 ——
于是这个缺陷一直是绿的。

已重写为 `test_severity_outranks_missing_fields_but_keeps_them_in_reasons`，
并新增一条真实快照样本的回归。

> 教训：**断言要检查 docstring 承诺的那件事**。测试名和注释都在说「严重度优先」，
> 代码在做相反的事，而断言弱到发现不了矛盾 —— 这比没有测试更危险，
> 因为它给了「这块测过了」的错觉。

## 复现方式

```bash
python -m scripts.run_cte --event EVT-004        # 走 CTE 闭环
python -m scripts.evaluate_ai_review             # 看 verdict_accuracy
```

## 附：本次迁移前的基线（留档）

`task.verdict_accuracy = 0.675`（真实 OCR 字段口径 + 严重度顺序缺陷）。
