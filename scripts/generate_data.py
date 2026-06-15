"""Generate synthetic bank OCR test images and labels.

This script uses only Python's standard library. It creates simple BMP images
with OCR-friendly ASCII text so the first data step can run without Pillow or
OpenCV.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data" / "synthetic"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"

WHITE = (248, 250, 252)
BLACK = (15, 23, 42)
GRAY = (100, 116, 139)
BLUE = (37, 99, 235)
GREEN = (22, 163, 74)
RED = (220, 38, 38)


FONT = {
    " ": ["000", "000", "000", "000", "000", "000", "000"],
    "-": ["000", "000", "000", "111", "000", "000", "000"],
    ".": ["0", "0", "0", "0", "0", "0", "1"],
    "/": ["001", "001", "010", "010", "100", "100", "000"],
    ":": ["0", "1", "0", "0", "0", "1", "0"],
    "_": ["000", "000", "000", "000", "000", "000", "111"],
    "0": ["111", "101", "101", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "010", "010", "111"],
    "2": ["111", "001", "001", "111", "100", "100", "111"],
    "3": ["111", "001", "001", "111", "001", "001", "111"],
    "4": ["101", "101", "101", "111", "001", "001", "001"],
    "5": ["111", "100", "100", "111", "001", "001", "111"],
    "6": ["111", "100", "100", "111", "101", "101", "111"],
    "7": ["111", "001", "001", "010", "010", "100", "100"],
    "8": ["111", "101", "101", "111", "101", "101", "111"],
    "9": ["111", "101", "101", "111", "001", "001", "111"],
    "A": ["111", "101", "101", "111", "101", "101", "101"],
    "B": ["110", "101", "101", "110", "101", "101", "110"],
    "C": ["111", "100", "100", "100", "100", "100", "111"],
    "D": ["110", "101", "101", "101", "101", "101", "110"],
    "E": ["111", "100", "100", "111", "100", "100", "111"],
    "F": ["111", "100", "100", "111", "100", "100", "100"],
    "G": ["111", "100", "100", "101", "101", "101", "111"],
    "H": ["101", "101", "101", "111", "101", "101", "101"],
    "I": ["111", "010", "010", "010", "010", "010", "111"],
    "J": ["111", "001", "001", "001", "001", "101", "111"],
    "K": ["101", "101", "110", "100", "110", "101", "101"],
    "L": ["100", "100", "100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101", "101", "101"],
    "N": ["101", "111", "111", "111", "111", "111", "101"],
    "O": ["111", "101", "101", "101", "101", "101", "111"],
    "P": ["111", "101", "101", "111", "100", "100", "100"],
    "Q": ["111", "101", "101", "101", "111", "001", "001"],
    "R": ["110", "101", "101", "110", "101", "101", "101"],
    "S": ["111", "100", "100", "111", "001", "001", "111"],
    "T": ["111", "010", "010", "010", "010", "010", "010"],
    "U": ["101", "101", "101", "101", "101", "101", "111"],
    "V": ["101", "101", "101", "101", "101", "101", "010"],
    "W": ["101", "101", "101", "101", "111", "111", "101"],
    "X": ["101", "101", "101", "010", "101", "101", "101"],
    "Y": ["101", "101", "101", "010", "010", "010", "010"],
    "Z": ["111", "001", "001", "010", "100", "100", "111"],
}


def new_image(width: int, height: int, color: tuple[int, int, int] = WHITE) -> list[list[tuple[int, int, int]]]:
    return [[color for _ in range(width)] for _ in range(height)]


def set_pixel(image: list[list[tuple[int, int, int]]], x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= y < len(image) and 0 <= x < len(image[0]):
        image[y][x] = color


def draw_rect(
    image: list[list[tuple[int, int, int]]],
    x: int,
    y: int,
    width: int,
    height: int,
    color: tuple[int, int, int],
    fill: bool = False,
) -> None:
    for yy in range(y, y + height):
        for xx in range(x, x + width):
            if fill or yy in (y, y + height - 1) or xx in (x, x + width - 1):
                set_pixel(image, xx, yy, color)


def draw_text(
    image: list[list[tuple[int, int, int]]],
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int] = BLACK,
    scale: int = 3,
) -> None:
    cursor = x
    for char in text.upper():
        pattern = FONT.get(char, FONT[" "])
        for row_idx, row in enumerate(pattern):
            for col_idx, value in enumerate(row):
                if value == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            set_pixel(image, cursor + col_idx * scale + sx, y + row_idx * scale + sy, color)
        cursor += (len(pattern[0]) + 1) * scale


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def save_png(image: list[list[tuple[int, int, int]]], path: Path) -> None:
    height = len(image)
    width = len(image[0])
    raw_rows = bytearray()
    for row in image:
        raw_rows.append(0)
        for red, green, blue in row:
            raw_rows.extend((red, green, blue))

    png_data = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            png_chunk(b"IDAT", zlib.compress(bytes(raw_rows), level=9)),
            png_chunk(b"IEND", b""),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        file.write(png_data)


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def draw_id_card(sample_id: str, fields: dict[str, str]) -> Path:
    path = DATA_DIR / "id_card" / "front" / "normal" / f"{sample_id}.png"
    image = new_image(760, 460)
    draw_rect(image, 20, 20, 720, 420, BLUE)
    draw_text(image, 55, 55, "CHINA ID CARD", BLUE, 5)
    draw_text(image, 55, 135, f"NAME: {fields['name']}", BLACK, 4)
    draw_text(image, 55, 190, f"SEX: {fields['sex']}", BLACK, 4)
    draw_text(image, 55, 245, f"BIRTH: {fields['birth_date']}", BLACK, 4)
    draw_text(image, 55, 300, f"ID: {fields['id_number']}", BLACK, 4)
    draw_text(image, 55, 355, f"VALID: {fields['valid_date']}", BLACK, 4)
    draw_rect(image, 555, 120, 120, 150, GRAY)
    draw_text(image, 575, 180, "PHOTO", WHITE, 3)
    save_png(image, path)
    return path


def draw_bank_card(sample_id: str, fields: dict[str, str]) -> Path:
    path = DATA_DIR / "bank_card" / "normal" / f"{sample_id}.png"
    image = new_image(760, 460, (239, 246, 255))
    draw_rect(image, 20, 20, 720, 420, GREEN)
    draw_text(image, 55, 60, "BANK CARD", GREEN, 6)
    draw_rect(image, 55, 145, 95, 70, (234, 179, 8), fill=True)
    draw_text(image, 55, 250, fields["card_number"], BLACK, 5)
    draw_text(image, 55, 340, f"NAME: {fields['name']}", BLACK, 4)
    draw_text(image, 55, 390, f"EXP: {fields['expire_date']}", BLACK, 4)
    save_png(image, path)
    return path


def draw_application_form(sample_id: str, fields: dict[str, str]) -> Path:
    path = DATA_DIR / "application_form" / "normal" / f"{sample_id}.png"
    image = new_image(860, 620)
    draw_rect(image, 25, 25, 810, 570, BLACK)
    draw_text(image, 70, 60, "ACCOUNT APPLICATION FORM", RED, 5)
    rows = [
        ("NAME", fields["name"]),
        ("ID", fields["id_number"]),
        ("PHONE", fields["phone"]),
        ("CARD", fields["card_number"]),
        ("AMOUNT", fields["loan_amount"]),
        ("DATE", fields["apply_date"]),
    ]
    y = 150
    for key, value in rows:
        draw_text(image, 70, y, f"{key}: {value}", BLACK, 4)
        draw_rect(image, 60, y - 15, 740, 46, GRAY)
        y += 70
    save_png(image, path)
    return path


def build_labels() -> list[dict[str, object]]:
    common = {
        "name": "ZHANG SAN",
        "id_number": "110101200001011234",
        "phone": "13800000000",
        "card_number": "6222020202020202",
    }

    samples = [
        {
            "sample_id": "id_0001",
            "doc_type": "id_card",
            "quality_type": "normal",
            "fields": {
                "name": common["name"],
                "sex": "M",
                "birth_date": "2000-01-01",
                "id_number": common["id_number"],
                "valid_date": "2035-01-01",
            },
        },
        {
            "sample_id": "card_0001",
            "doc_type": "bank_card",
            "quality_type": "normal",
            "fields": {
                "name": common["name"],
                "card_number": common["card_number"],
                "expire_date": "12/30",
            },
        },
        {
            "sample_id": "form_0001",
            "doc_type": "application_form",
            "quality_type": "normal",
            "fields": {
                "name": common["name"],
                "id_number": common["id_number"],
                "phone": common["phone"],
                "card_number": common["card_number"],
                "loan_amount": "50000",
                "apply_date": "2026-06-15",
            },
        },
    ]

    labels = []
    for sample in samples:
        doc_type = str(sample["doc_type"])
        fields = sample["fields"]
        sample_id = str(sample["sample_id"])
        if doc_type == "id_card":
            image_path = draw_id_card(sample_id, fields)  # type: ignore[arg-type]
        elif doc_type == "bank_card":
            image_path = draw_bank_card(sample_id, fields)  # type: ignore[arg-type]
        else:
            image_path = draw_application_form(sample_id, fields)  # type: ignore[arg-type]

        labels.append(
            {
                "image_path": relative(image_path),
                "doc_type": doc_type,
                "side": "front" if doc_type == "id_card" else None,
                "quality_type": sample["quality_type"],
                "fields": fields,
            }
        )

    return labels


def main() -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    labels = build_labels()
    LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {len(labels)} images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
