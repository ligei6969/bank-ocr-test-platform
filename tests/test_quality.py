"""Tests for image quality checks."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.quality_check import check_image_quality, detect_blur, detect_brightness, detect_glare


ARTIFACT_DIR = Path("reports") / "test-artifacts" / "quality"


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

    assert result == {
        "is_blur": False,
        "brightness": "normal",
        "has_glare": False,
        "quality_result": "pass",
    }
