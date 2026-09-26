"""边界样本判据 —— 决定哪些 ``review`` 记录值得交给 LLM 复核。

为什么需要单独一个模块
----------------------
规则引擎判 ``review`` 只是「需要人看一眼」，其中一部分是**规则可能误报**：
阈值边缘的、单一可误报原因码的、字段形态接近正确的。对这些用 LLM 复核一次，
成本可控且收益明确；对绝大多数正常 ``review`` 调用模型纯属浪费。

判据必须与规则层解耦，所以这里是**纯函数**：不调 AI、不碰数据库、不打日志。
它只回答「这条该不该问 LLM」，回答不了「LLM 会说什么」—— 后者在 AI 服务侧。

设计约束（来自 ``docs/AI智能审核融合方案.md`` 第 171-186 行）
-----------------------------------------------------------
* 只有 ``review`` 才可能是边界样本。``pass`` 与 ``reject`` 永远不调 LLM ——
  这是成本控制，也是权限边界：``reject`` 连被复核的机会都不该有。
* 判据是 **OR** 关系。三类边界场景性质不同，命中任一即值得复核。
* 返回**命中的判据标签列表**而不只是布尔值：否则改判率只能报一个混合总数，
  而不同类型样本「该不该改」的正确答案本就不同，混在一起算不出有效结论。

标签是**内部分类标签，不是原因码** —— 它们不进 ``review_reasons``、
不展示给用户、不需要语料释义。原因码是给用户看的处置依据，
判据标签是给评测分组的归因维度，两者不要混。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.quality_check import (
    BLUR_VARIANCE_THRESHOLD,
    BRIGHTNESS_BRIGHT_THRESHOLD,
    BRIGHTNESS_DARK_THRESHOLD,
    GLARE_COMPONENT_RATIO_THRESHOLD,
)

#: 边界标签
NEAR_BLUR_THRESHOLD = "near_blur_threshold"
NEAR_DARK_THRESHOLD = "near_dark_threshold"
NEAR_BRIGHT_THRESHOLD = "near_bright_threshold"
NEAR_GLARE_THRESHOLD = "near_glare_threshold"
FALSE_POSITIVE_CODE = "false_positive_reason_code"
NEAR_MISS_FIELD = "near_miss_field_validation"

#: C1 各轴的边界带宽，**逐轴独立**。
#:
#: 不共用一个百分比，是因为三个量的量纲与风险分布完全不同：模糊方差是
#: 0..无穷 的连续量，灰度是 0..255 的封闭量，亮斑占比是 0..1 的比例。
#: 「统一 ±20%」在代码上漂亮，在业务上没有依据。
#:
#: 当前取值是**保守的工程初值，尚无数据支撑** —— 40 条标注样本里没有一条
#: 落在模糊/灰度阈值下方 10 单位内（它们是为「跨阈值」造的梯度样本，形态
#: 是 glare 型或远离阈值）。等真实流量积累后，用「指标距阈值的距离 vs 人工
#: 结论」反推合理带宽，再回来改这几个常量。
BLUR_BOUNDARY_BAND = 10.0
DARK_BOUNDARY_BAND = 10.0
BRIGHT_BOUNDARY_BAND = 5.0
GLARE_BOUNDARY_BAND = 0.001

#: 「可能误报」类原因码。目前只有反光 —— 银行卡镭射区与证件覆膜在标准光照下
#: 就会触发它，是已知的规则误报源（融合方案第 180 行点名）。
#:
#: 刻意保持极小：每加一个都等于宣称「这个原因码不可靠」，需要评测数据支撑。
#: 模糊/偏暗/偏亮都是经过阈值标定的真实信号，不属于此类。
FALSE_POSITIVE_CODES = frozenset({"glare_detected"})


def _in_lower_band(value: Any, threshold: float, band: float) -> bool:
    """命中区间是 ``(0, threshold)``（越小越异常），边界带取其下方一段。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return threshold - band <= float(value) < threshold


def _in_upper_band(value: Any, threshold: float, band: float) -> bool:
    """命中区间是 ``(threshold, +inf)``（越大越异常），边界带取其上方一段。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return threshold < float(value) <= threshold + band


def detect_near_threshold_criteria(quality: Mapping[str, Any] | None) -> list[str]:
    """C1：原始指标落在判定阈值边缘。

    只取「阈值被跨过的那一侧」—— 高于模糊阈值的图根本不会被判模糊，
    也就不可能进入 review，把它算作边界样本没有意义。
    """
    metrics = (quality or {}).get("quality_metrics") or {}
    if not isinstance(metrics, Mapping):
        return []

    criteria: list[str] = []
    if _in_lower_band(metrics.get("blur_laplacian_variance"), BLUR_VARIANCE_THRESHOLD, BLUR_BOUNDARY_BAND):
        criteria.append(NEAR_BLUR_THRESHOLD)
    brightness = metrics.get("brightness_mean")
    if _in_lower_band(brightness, float(BRIGHTNESS_DARK_THRESHOLD), DARK_BOUNDARY_BAND):
        criteria.append(NEAR_DARK_THRESHOLD)
    if _in_upper_band(brightness, float(BRIGHTNESS_BRIGHT_THRESHOLD), BRIGHT_BOUNDARY_BAND):
        criteria.append(NEAR_BRIGHT_THRESHOLD)
    if _in_upper_band(
        metrics.get("glare_component_ratio"),
        GLARE_COMPONENT_RATIO_THRESHOLD,
        GLARE_BOUNDARY_BAND,
    ):
        criteria.append(NEAR_GLARE_THRESHOLD)
    return criteria


def detect_false_positive_criteria(review_reasons: Sequence[str] | None) -> list[str]:
    """C2：原因码只有一个，且属于已知可能误报的那一类。

    「只有一个」是关键限定 —— 多个原因码同时成立时，规则判定有交叉证据支撑，
    误报概率显著更低，不该占用 LLM 复核预算。
    """
    codes = {str(code) for code in (review_reasons or [])}
    if len(codes) == 1 and codes & FALSE_POSITIVE_CODES:
        return [FALSE_POSITIVE_CODE]
    return []


def _is_near_miss_expiry(value: Any) -> bool:
    """有效期形态对、但取值非法 —— 典型是 OCR 把月份认错。

    形如 ``"01/55"``（形状是 MM/YY，但月份越界）算近似；``"SAD//"`` 这种
    结构就不对的不算 —— 后者更可能是识别失败而非单字符误识。
    """
    if not isinstance(value, str) or len(value) != 5 or value[2] != "/":
        return False
    month, year = value[:2], value[3:]
    if not (month.isdigit() and year.isdigit()):
        return False
    return not 1 <= int(month) <= 12


def detect_near_miss_field_criteria(
    fields: Mapping[str, Any] | None,
    review_reasons: Sequence[str] | None,
    *,
    doc_type: str,
) -> list[str]:
    """C3：字段解析出来了、但差一点没过校验，像 OCR 误识而不是假证件。

    注意 **判定必须在这里做完，不能把原始值交给 LLM 判断** ——
    平台的 ``AIAssistClient._sanitize_payload`` 会对卡号脱敏（只留前 6 后 4 位），
    AI 侧拿到的号码本来就不完整。所以这里传出去的是**结论**，不是号码。

    身份证侧没有格式校验（只有 ``missing_*``），所以 C3 在身份证上无判据可用 ——
    这是数据性质决定的，不是遗漏。
    """
    if doc_type != "bank_card":
        return []

    codes = {str(code) for code in (review_reasons or [])}
    resolved = fields or {}

    # 卡号长度差一点（15 位或 20 位）：OCR 漏识或多识一位，不是伪造
    if "invalid_card_number" in codes:
        raw = resolved.get("card_number")
        digits = "".join(ch for ch in str(raw) if ch.isdigit()) if raw is not None else ""
        if digits and len(digits) in (15, 20):
            return [NEAR_MISS_FIELD]

    # 有效期月份越界但形状正确
    if "invalid_valid_date" in codes and _is_near_miss_expiry(resolved.get("valid_date")):
        return [NEAR_MISS_FIELD]

    return []


def detect_boundary_case(
    fields: Mapping[str, Any] | None,
    quality: Mapping[str, Any] | None,
    review_result: str,
    review_reasons: Sequence[str] | None,
    *,
    doc_type: str = "bank_card",
) -> list[str]:
    """返回命中的边界判据标签；空列表表示不是边界样本。

    ``review_result`` 不是 ``review`` 时**恒返回空** —— 这既是成本控制，
    也是权限边界：``reject`` 不该有被 LLM 复核的机会。
    """
    if review_result != "review":
        return []
    if not review_reasons:
        # review 却没有任何原因码，说明这条记录的判定来路不明，
        # 该交人工查规则本身，而不是问 LLM「要不要放行」
        return []

    criteria: list[str] = []
    criteria.extend(detect_near_threshold_criteria(quality))
    criteria.extend(detect_false_positive_criteria(review_reasons))
    criteria.extend(
        detect_near_miss_field_criteria(fields, review_reasons, doc_type=doc_type)
    )
    return criteria
