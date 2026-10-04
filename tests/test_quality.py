"""Tests for image quality checks."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app import quality_check
from app.quality_check import check_image_quality, detect_blur, detect_brightness, detect_glare


ARTIFACT_DIR = Path("reports") / "test-artifacts" / "quality"
BANK_CARD_DATA_DIR = Path("data") / "processed" / "bank_card"
SAMPLE_INDEXES = (1, 2, 3)
QUALITY_TYPES = ("normal", "blur", "glare", "occlusion", "rotate", "dark", "bright")


def bank_card_sample_path(quality_type: str, index: int) -> Path:
    return BANK_CARD_DATA_DIR / quality_type / f"bank_card_{index:04d}.png"


def sample_ids(quality_type: str) -> list[str]:
    return [f"{quality_type}-{index:04d}" for index in SAMPLE_INDEXES]


def artifact_path(name: str) -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACT_DIR / name


def save_checkerboard(path: Path, size: tuple[int, int] = (240, 160)) -> None:
    image = Image.new("RGB", size, (128, 128, 128))
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], 10):
        for x in range(0, size[0], 10):
            color = (40, 40, 40) if (x // 10 + y // 10) % 2 == 0 else (220, 220, 220)
            draw.rectangle((x, y, x + 9, y + 9), fill=color)
    image.save(path)


def test_detect_blur() -> None:
    sharp_path = artifact_path("sharp.png")
    blur_path = artifact_path("blur.png")
    save_checkerboard(sharp_path)
    Image.open(sharp_path).filter(ImageFilter.GaussianBlur(radius=5)).save(blur_path)

    assert not detect_blur(str(sharp_path))
    assert detect_blur(str(blur_path))


def test_detect_brightness() -> None:
    dark_path = artifact_path("dark.png")
    bright_path = artifact_path("bright.png")
    normal_path = artifact_path("normal.png")
    Image.new("RGB", (120, 80), (30, 30, 30)).save(dark_path)
    Image.new("RGB", (120, 80), (240, 240, 240)).save(bright_path)
    Image.new("RGB", (120, 80), (128, 128, 128)).save(normal_path)

    assert detect_brightness(str(dark_path)) == "dark"
    assert detect_brightness(str(bright_path)) == "bright"
    assert detect_brightness(str(normal_path)) == "normal"


def test_detect_glare() -> None:
    image_path = artifact_path("glare.png")
    image = Image.new("RGB", (240, 160), (100, 120, 140))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 110, 90), fill=(255, 255, 255))
    image.save(image_path)

    assert detect_glare(str(image_path))


def test_check_image_quality_pass() -> None:
    image_path = artifact_path("quality_normal.png")
    save_checkerboard(image_path)

    result = check_image_quality(str(image_path))

    assert result["is_blur"] is False
    assert result["brightness"] == "normal"
    assert result["has_glare"] is False
    assert result["quality_result"] == "pass"
    assert result["quality_reasons"] == []
    assert result["severe_reasons"] == []


def test_check_image_quality_exposes_raw_metrics() -> None:
    """原始指标要随结论一起返回 —— 规则层靠它判严重度，AI 服务靠它做真重算。"""
    image_path = artifact_path("quality_normal_metrics.png")
    save_checkerboard(image_path)

    result = check_image_quality(str(image_path))

    assert set(result["quality_metrics"]) == {
        "blur_laplacian_variance",
        "brightness_mean",
        "glare_component_ratio",
        "rotation_angle_degrees",
        "occlusion_area_ratio",
    }


@pytest.mark.parametrize(
    ("is_blur", "brightness", "has_glare", "expected_reasons"),
    [
        (False, "normal", False, []),
        (True, "normal", False, ["image_blur"]),
        (False, "dark", False, ["image_dark"]),
        (False, "bright", False, ["image_bright"]),
        (False, "normal", True, ["glare_detected"]),
    ],
)
def test_check_image_quality_returns_reason_codes(
    monkeypatch,
    is_blur: bool,
    brightness: str,
    has_glare: bool,
    expected_reasons: list[str],
) -> None:
    # check_image_quality 现在一次读图算出原始指标，所以桩要打在测量函数上
    metrics = {
        "blur_laplacian_variance": 10.0 if is_blur else 900.0,
        "brightness_mean": {"dark": 20.0, "bright": 240.0}.get(brightness, 120.0),
        "glare_component_ratio": 0.5 if has_glare else 0.0,
    }
    monkeypatch.setattr("app.quality_check.measure_image_quality", lambda image_path: metrics)

    result = check_image_quality("unused.png")

    assert result["quality_reasons"] == expected_reasons
    assert result["quality_result"] == ("review" if expected_reasons else "pass")


@pytest.mark.parametrize(
    ("quality_type", "image_path"),
    [
        (quality_type, bank_card_sample_path(quality_type, index))
        for quality_type in QUALITY_TYPES
        for index in SAMPLE_INDEXES
    ],
    ids=[
        f"{quality_type}-{index:04d}"
        for quality_type in QUALITY_TYPES
        for index in SAMPLE_INDEXES
    ],
)
def test_processed_bank_card_samples_exist(quality_type: str, image_path: Path) -> None:
    assert image_path.is_file(), f"Missing {quality_type} sample: {image_path}"


@pytest.mark.parametrize(
    "image_path",
    [bank_card_sample_path("normal", index) for index in SAMPLE_INDEXES],
    ids=sample_ids("normal"),
)
def test_processed_normal_bank_cards_have_normal_brightness_and_are_not_blurry(image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert result["brightness"] == "normal"
    assert result["is_blur"] is False


@pytest.mark.parametrize(
    "image_path",
    [bank_card_sample_path("normal", index) for index in SAMPLE_INDEXES],
    ids=sample_ids("normal"),
)
def test_processed_normal_bank_cards_pass_quality_gate(image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert result["quality_result"] == "pass"
    assert result["quality_reasons"] == []


@pytest.mark.parametrize(
    "image_path",
    [bank_card_sample_path("blur", index) for index in SAMPLE_INDEXES],
    ids=sample_ids("blur"),
)
def test_processed_blur_bank_cards_are_blurry(image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert result["is_blur"] is True
    assert "image_blur" in result["quality_reasons"]


@pytest.mark.parametrize(
    "image_path",
    [bank_card_sample_path("dark", index) for index in SAMPLE_INDEXES],
    ids=sample_ids("dark"),
)
def test_processed_dark_bank_cards_are_dark(image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert result["brightness"] == "dark"
    assert "image_dark" in result["quality_reasons"]


@pytest.mark.parametrize(
    "image_path",
    [bank_card_sample_path("bright", index) for index in SAMPLE_INDEXES],
    ids=sample_ids("bright"),
)
def test_processed_bright_bank_cards_are_bright(image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert result["brightness"] == "bright"
    assert "image_bright" in result["quality_reasons"]


@pytest.mark.parametrize(
    "image_path",
    [bank_card_sample_path("glare", index) for index in SAMPLE_INDEXES],
    ids=sample_ids("glare"),
)
def test_processed_glare_bank_cards_have_glare(image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert result["has_glare"] is True
    assert "glare_detected" in result["quality_reasons"]


@pytest.mark.parametrize(
    ("quality_type", "image_path"),
    [
        (quality_type, bank_card_sample_path(quality_type, index))
        for quality_type in ("occlusion", "rotate")
        for index in SAMPLE_INDEXES
    ],
    ids=[
        f"{quality_type}-{index:04d}"
        for quality_type in ("occlusion", "rotate")
        for index in SAMPLE_INDEXES
    ],
)
def test_processed_occlusion_and_rotate_bank_cards_are_processable(quality_type: str, image_path: Path) -> None:
    result = check_image_quality(str(image_path))

    assert quality_type in {"occlusion", "rotate"}
    assert set(result) == {
        "is_blur",
        "brightness",
        "has_glare",
        "quality_result",
        "quality_reasons",
        "quality_metrics",
        "severe_reasons",
    }
    assert isinstance(result["is_blur"], bool)
    assert result["brightness"] in {"dark", "normal", "bright"}
    assert isinstance(result["has_glare"], bool)
    assert result["quality_result"] in {"pass", "review"}
    assert isinstance(result["severe_reasons"], list)


# ── 严重退化判定 ──────────────────────────────────────────────────────────────

def test_severe_reasons_empty_for_clean_scan() -> None:
    metrics = {
        "blur_laplacian_variance": 900.0,
        "brightness_mean": 120.0,
        "glare_component_ratio": 0.0,
    }

    assert quality_check._severe_reasons(metrics) == []


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"blur_laplacian_variance": 29.9, "brightness_mean": 120.0}, "severe_image_blur"),
        ({"blur_laplacian_variance": 30.0, "brightness_mean": 120.0}, None),
        ({"brightness_mean": 34.9}, "severe_image_dark"),
        ({"brightness_mean": 35.0}, None),
        ({"brightness_mean": 215.1}, "severe_image_bright"),
        ({"brightness_mean": 215.0}, None),
    ],
)
def test_severe_threshold_boundaries(metrics: dict, expected: str | None) -> None:
    """阈值是排他的：恰好等于阈值不算严重，越过才算。"""
    reasons = quality_check._severe_reasons(metrics)

    if expected is None:
        assert reasons == []
    else:
        assert reasons == [expected]


def test_glare_severity_is_currently_disabled() -> None:
    """反光的严重度阈值停用中 —— 再大的亮斑也不判 reject。

    原因见 ``SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD``：标定间隙只有 9%，
    按它判 reject 是过拟合。停用期间反光一律走人工复核。
    """
    assert quality_check.SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD is None

    reasons = quality_check._severe_reasons(
        {"brightness_mean": 120.0, "glare_component_ratio": 0.99}
    )

    assert reasons == []


def test_moderate_degradation_is_not_severe() -> None:
    """偏暗但未到严重档（35–65）应当只判 review，而不是 reject。"""
    metrics = {"blur_laplacian_variance": 400.0, "brightness_mean": 50.0}

    assert quality_check._severe_reasons(metrics) == []
