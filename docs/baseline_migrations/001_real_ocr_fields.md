# Baseline Migration 001：字段输入改为真实 OCR 观测

> 日期：2026-09-27
> 相关：CTE-2、`test_evolution/ocr_snapshot.py`、`data/annotations/ocr_outputs.json`
> 门禁打红的项目：3 项（见下）

## 为什么 baseline 变了

**这不是回归，是测量口径变真实了。**

| | 之前（口径 A） | 现在（口径 B） |
| --- | --- | --- |
| 字段输入 | `labels.json` 的**标注真值** | 真实 PaddleOCR 的**实际解析结果** |
| 「字段全部解析成功」 | **恒成立** | 会失败，且确实失败了 |
| 结论正确率 | 0.775 | **0.675** |

口径 A 里字段是人标的，所以规则引擎永远拿到完整正确的字段，
`missing_*` 这一族原因码**结构上产不出来** —— 那正是 P5 方案说的
「这个信号没有区分度」，也是 OCR / 双判两个 surface 被卡住的根因。

口径 B 用的是 `data/annotations/ocr_outputs.json`：50 张 golden 图过真实
PaddleOCR（`paddleocr=3.7.0; paddle=3.3.0`）录下的观测，含原始文本行、
解析字段与图像质量指标。

## 指标收到了什么影响

| 指标 | 口径 A | 口径 B | 变化 | 解读 |
| --- | --- | --- | --- | --- |
| `task.verdict_accuracy` | 0.775 | 0.675 | −12.90% | **真实下降**：字段真的会缺、会错 |
| `tools.sequence_accuracy` | 0.950 | 0.900 | −5.26% | 字段不同 → 触发的工具序列不同 |
| `tools.avg_steps` | 2.750 | 2.900 | +5.45% | 多了 `recompute_quality` 之类的一步 |

其余指标未动。

**为什么 `verdict_accuracy` 下降是「真实」而不是「变差」**：
它现在测的是「系统在**真实输入**下的结论与人工结论的一致率」。
之前那个 0.775 是在一个「字段永远完整」的理想化输入上算出来的 ——
那个数字对真实场景没有预测力。

## 新口径暴露的真实缺陷（这才是本阶段的产出）

录制完立刻能看见三类此前完全不可见的问题：

### 1. 模糊样本：`name` 被识别成有效期标签

```
blur/bank_card_0001.png  name='VALIDTHIRU'  (期望 'ZHUBIN')
blur/bank_card_0005.png  name='VALDTHIRU'   (期望 'HEFENG')
```

「VALID THRU」这行文字在模糊图上被 OCR 与姓名行混在一起，
`field_parser` 的姓名抽取把它当成了持卡人姓名。

### 2. 反光样本：卡号**单字符**误识

```
glare/bank_card_0005.png  card_number='5282448378463572'  (期望 ...573)
```

最后一位 3 → 2。**这正是双判机制本该抓到的那类错误** ——
此前因为字段来自标注真值，这种样本根本不存在。

### 3. 身份证正面：标签与值被 OCR 分成两行，解析不出

```
ocr_texts: ['姓名', '沈梓欣', '性别女', '民族回', '出生1990', '年8月31日', ...]
parsed:    {'name': None, 'gender': '女', 'nation': '回', 'birth': '1990-08-31',
            'address': None, 'id_number': None}
```

`app/id_card_parser.py` 的 `_value_after_label` 要求「标签与值在同一行」
（`姓名 沈梓欣`）。mock OCR 是把它们拼成一行的，所以这个假设一直没被检验；
真实 PaddleOCR 把「姓名」和「沈梓欣」检测成**两个独立的文本框**。

结果：**身份证正面的姓名 / 住址 / 身份证号 100% 解析失败**，
而 mock 模式下这 100% 成功。

> 这一条影响最大：它意味着此前所有身份证正面的字段相关结论
> （无论是评测还是单测）都建立在 mock 的拼接行为上，与真实 OCR 不符。

## 为什么这些不算「CTE 的学习结论」

按 `test_evolution/readiness.py`，`ocr` 与 `adjudication` 两个 surface
现在仍是 **blocked**。本阶段交付的是**解除阻塞的前提**（真实观测），
不是「据此应该怎么改双判」的结论。

上面三条缺陷要转成 CTE 事件（`Event`）走完整闭环，属于 CTE-3。

## 复现方式

```bash
# 录制（需真实 PaddleOCR，不进 CI）
python -m scripts.record_ocr_snapshot

# 用快照口径跑评测
python -m scripts.evaluate_ai_review

# 对照旧口径
python -m scripts.evaluate_ai_review --no-ocr-snapshot
```

## 附：本次迁移前的基线（留档）

`task.verdict_accuracy = 0.775`、`tools.sequence_accuracy = 0.950`、
`tools.avg_steps = 2.75` —— 口径 A 下的数字，换样本或换口径都不再可比。
