"""影像质检阈值的机器可读副本。

为什么需要这个文件
------------------
``app/quality_check.py`` 里的阈值是写在函数体里的字面量
（``variance < 80.0``、``mean_value < 65``……）。这在平台侧没问题 ——
判定就在同一个函数里完成。但 Agent 需要一个**可被程序读取**的阈值表：

* ``recompute_quality`` 工具要能拿它反推原因码，而不是把数字再抄一遍；
* 阈值一旦被抄两遍，就一定会漂移，而「阈值以语料为准」是本服务的公开承诺。

所以这里把阈值抽成结构化的常量，并配一条**一致性测试**：
从 ``app/quality_check.py`` 的源码里把真值读出来，与这份常量逐项比对。
平台改了阈值而这里没跟，测试直接红 —— 这就是防漂移的机制，不是靠自觉。

注意：本模块刻意**不 import** ``app.quality_check``。那份实现依赖 OpenCV，
而 AI 服务要保持轻依赖、可独立部署；跨进程去 import 平台的图像库得不偿失。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

REASON_IMAGE_BLUR = "image_blur"
REASON_IMAGE_DARK = "image_dark"
REASON_IMAGE_BRIGHT = "image_bright"
REASON_GLARE_DETECTED = "glare_detected"

QUALITY_REASON_CODES: tuple[str, ...] = (
    REASON_IMAGE_BLUR,
    REASON_IMAGE_DARK,
    REASON_IMAGE_BRIGHT,
    REASON_GLARE_DETECTED,
)

#: 原始指标字段名。平台若把这些数字随 payload 发过来，``recompute_quality``
#: 就能真正重算；发不过来时只能做一致性核对（见 ``audit_quality``）。
METRIC_BLUR_VARIANCE = "blur_laplacian_variance"
METRIC_BRIGHTNESS_MEAN = "brightness_mean"
METRIC_GLARE_COMPONENT_RATIO = "glare_component_ratio"


@dataclass(frozen=True)
class ImageQualityThresholds:
    """与 ``app/quality_check.py`` 保持一致的阈值。"""

    blur_variance: float = 80.0
    brightness_dark: float = 65.0
    brightness_bright: float = 210.0
    glare_value: int = 245
    glare_saturation: int = 45
    glare_component_ratio: float = 0.005

    def as_dict(self) -> Dict[str, Any]:
        return {
            "blur_variance": self.blur_variance,
            "brightness_dark": self.brightness_dark,
            "brightness_bright": self.brightness_bright,
            "glare_value": self.glare_value,
            "glare_saturation": self.glare_saturation,
            "glare_component_ratio": self.glare_component_ratio,
        }

    def describe(self) -> List[str]:
        """人话版阈值说明，直接进工具输出给审核员看。"""
        return [
            f"清晰度：拉普拉斯方差 < {self.blur_variance:g} 判为模糊",
            f"亮度：灰度均值 < {self.brightness_dark:g} 判为过暗，"
            f"> {self.brightness_bright:g} 判为过亮",
            f"反光：像素值 > {self.glare_value} 且饱和度 < {self.glare_saturation}，"
            f"且最大连通域占比 > {self.glare_component_ratio * 100:g}%",
        ]


DEFAULT_THRESHOLDS = ImageQualityThresholds()


def _number(metrics: Mapping[str, Any], key: str) -> Optional[float]:
    value = metrics.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def derive_quality_reasons(
    metrics: Mapping[str, Any],
    thresholds: ImageQualityThresholds = DEFAULT_THRESHOLDS,
) -> List[str]:
    """按阈值从原始指标反推原因码。

    只有能取到对应指标的判定才会出现 —— 拿不到就不猜，
    宁可少给一个原因码，也不给一个没有依据的结论。
    """
    reasons: List[str] = []

    variance = _number(metrics, METRIC_BLUR_VARIANCE)
    if variance is not None and variance < thresholds.blur_variance:
        reasons.append(REASON_IMAGE_BLUR)

    mean_value = _number(metrics, METRIC_BRIGHTNESS_MEAN)
    if mean_value is not None:
        if mean_value < thresholds.brightness_dark:
            reasons.append(REASON_IMAGE_DARK)
        elif mean_value > thresholds.brightness_bright:
            reasons.append(REASON_IMAGE_BRIGHT)

    glare_ratio = _number(metrics, METRIC_GLARE_COMPONENT_RATIO)
    if glare_ratio is not None and glare_ratio > thresholds.glare_component_ratio:
        reasons.append(REASON_GLARE_DETECTED)

    return reasons


def derive_quality_result(reasons: List[str]) -> str:
    """与平台判定一致：任一质检原因码命中即为 review，否则 pass。"""
    return "review" if reasons else "pass"


def audit_quality(
    quality_result: Optional[str],
    quality_reasons: Optional[List[str]],
    metrics: Optional[Mapping[str, Any]] = None,
    thresholds: ImageQualityThresholds = DEFAULT_THRESHOLDS,
) -> Dict[str, Any]:
    """核对一条记录的质量判定是否自洽，能重算就重算。

    返回结构里 ``mode`` 说明这次核对的力度：

    * ``metrics`` —— 拿到了原始指标，做了**真重算**，``recomputed=True``；
    * ``flags``   —— 只有结论与原因码，只能核对**自洽性**（结论与原因码是否矛盾、
      是否出现未收录的原因码），``recomputed=False``。

    为什么不把 ``flags`` 模式也算作「重算」：没有原始数字就无法验证阈值是否
    真的被跨过，只能验证「有原因码就该是 review」这类一致性。把它说成重算
    是自欺欺人 —— 本项目对此一律明示。
    """
    stored_reasons = [str(item) for item in (quality_reasons or [])]
    result: Dict[str, Any] = {
        "quality_result": quality_result,
        "stored_reasons": stored_reasons,
        "thresholds": thresholds.as_dict(),
        "threshold_notes": thresholds.describe(),
    }

    if metrics:
        derived = derive_quality_reasons(metrics, thresholds)
        result.update(
            {
                "mode": "metrics",
                "recomputed": True,
                "metrics": dict(metrics),
                "derived_reasons": derived,
                "derived_quality_result": derive_quality_result(derived),
                "matches_stored": sorted(derived) == sorted(stored_reasons),
                "missing_from_derived": sorted(set(stored_reasons) - set(derived)),
                "extra_in_derived": sorted(set(derived) - set(stored_reasons)),
            }
        )
        return result

    expected = derive_quality_result(stored_reasons)
    unknown = [code for code in stored_reasons if code not in QUALITY_REASON_CODES]
    result.update(
        {
            "mode": "flags",
            "recomputed": False,
            "derived_reasons": stored_reasons,
            "derived_quality_result": expected,
            "consistent": bool(quality_result == expected),
            "unknown_reason_codes": unknown,
        }
    )
    return result


__all__ = (
    "DEFAULT_THRESHOLDS",
    "METRIC_BLUR_VARIANCE",
    "METRIC_BRIGHTNESS_MEAN",
    "METRIC_GLARE_COMPONENT_RATIO",
    "QUALITY_REASON_CODES",
    "REASON_GLARE_DETECTED",
    "REASON_IMAGE_BLUR",
    "REASON_IMAGE_BRIGHT",
    "REASON_IMAGE_DARK",
    "ImageQualityThresholds",
    "audit_quality",
    "derive_quality_reasons",
    "derive_quality_result",
)
