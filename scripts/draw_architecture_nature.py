"""Draw a publication-style architecture figure for the OCR review platform."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import patches
from matplotlib.path import Path as MplPath


plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Arial", "DejaVu Sans", "Liberation Sans"]
plt.rcParams["svg.fonttype"] = "none"
plt.rcParams["pdf.fonttype"] = 42
plt.rcParams["font.size"] = 7.5
plt.rcParams["axes.linewidth"] = 0.8


ROOT_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT_DIR / "reports"
OUT_BASE = OUT_DIR / "bank_ocr_architecture_nature"

PALETTE = {
    "ink": "#272727",
    "muted": "#606060",
    "line": "#4D4D4D",
    "blue": "#0F4D92",
    "blue_soft": "#DDEAF7",
    "green": "#2E7D48",
    "green_soft": "#DDF3DE",
    "gold": "#C98A1C",
    "gold_soft": "#F8E8C8",
    "violet": "#7256A6",
    "violet_soft": "#ECE7F6",
    "rose": "#B64342",
    "rose_soft": "#F6CFCB",
    "gray_soft": "#F3F4F6",
    "gray_mid": "#D8D8D8",
}


def add_panel_label(ax: plt.Axes, label: str, x: float, y: float) -> None:
    ax.text(x, y, label, fontsize=12, fontweight="bold", color=PALETTE["ink"], ha="left", va="top")


def add_box(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    lines: list[str],
    fill: str,
    edge: str,
    title_color: str | None = None,
) -> patches.FancyBboxPatch:
    box = patches.FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.018,rounding_size=0.025",
        linewidth=1.0,
        edgecolor=edge,
        facecolor=fill,
    )
    ax.add_patch(box)
    if lines:
        ax.text(
            x + 0.04,
            y + h - 0.045,
            title,
            fontsize=7.8,
            fontweight="bold",
            color=title_color or PALETTE["ink"],
            ha="left",
            va="top",
        )
        ax.text(
            x + 0.04,
            y + h - 0.125,
            "\n".join(lines),
            fontsize=5.6,
            color=PALETTE["muted"],
            ha="left",
            va="top",
            linespacing=1.15,
        )
    else:
        ax.text(
            x + w / 2,
            y + h / 2,
            title,
            fontsize=8.6,
            fontweight="bold",
            color=title_color or PALETTE["ink"],
            ha="center",
            va="center",
        )
    return box


def add_arrow(
    ax: plt.Axes,
    start: tuple[float, float],
    end: tuple[float, float],
    label: str | None = None,
    color: str = "#4D4D4D",
    curve: float = 0.0,
    dashed: bool = False,
) -> None:
    if curve:
        sx, sy = start
        ex, ey = end
        mx = (sx + ex) / 2
        vertices = [start, (mx, sy + curve), (mx, ey + curve), end]
        codes = [MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4]
        path = MplPath(vertices, codes)
        patch = patches.FancyArrowPatch(
            path=path,
            arrowstyle="-|>",
            mutation_scale=9,
            linewidth=0.95,
            color=color,
            linestyle=(0, (3, 2)) if dashed else "solid",
        )
    else:
        patch = patches.FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=9,
            linewidth=0.95,
            color=color,
            linestyle=(0, (3, 2)) if dashed else "solid",
        )
    ax.add_patch(patch)
    if label:
        lx = (start[0] + end[0]) / 2
        ly = (start[1] + end[1]) / 2
        ax.text(lx, ly + 0.025, label, fontsize=5.9, color=color, ha="center", va="bottom")


def add_band(ax: plt.Axes, x: float, y: float, w: float, h: float, label: str, color: str) -> None:
    rect = patches.FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.01,rounding_size=0.018",
        linewidth=0.8,
        edgecolor=PALETTE["gray_mid"],
        facecolor=color,
        alpha=0.58,
        zorder=0,
    )
    ax.add_patch(rect)
    ax.text(x + 0.035, y + 0.025, label, fontsize=7.4, fontweight="bold", color=PALETTE["muted"], ha="left", va="bottom")


def draw_runtime_panel(ax: plt.Axes) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    add_panel_label(ax, "a", 0.0, 1.0)
    ax.text(
        0.055,
        0.98,
        "Runtime review pipeline",
        fontsize=10.5,
        fontweight="bold",
        color=PALETTE["ink"],
        ha="left",
        va="top",
    )
    ax.text(
        0.055,
        0.925,
        "Uploaded document images are converted into quality metrics, OCR text, structured fields and a review decision.",
        fontsize=6.7,
        color=PALETTE["muted"],
        ha="left",
        va="top",
    )

    add_band(ax, 0.035, 0.70, 0.93, 0.17, "API entry", PALETTE["blue_soft"])
    add_band(ax, 0.035, 0.39, 0.93, 0.23, "Image and OCR services", PALETTE["green_soft"])
    add_band(ax, 0.035, 0.11, 0.93, 0.20, "Parsing and rule decision", PALETTE["gold_soft"])

    add_box(
        ax,
        0.07,
        0.735,
        0.19,
        0.105,
        "Client",
        [],
        PALETTE["gray_soft"],
        PALETTE["line"],
    )
    add_box(
        ax,
        0.31,
        0.735,
        0.24,
        0.105,
        "app/main.py",
        [],
        PALETTE["blue_soft"],
        PALETTE["blue"],
    )
    add_box(
        ax,
        0.60,
        0.735,
        0.28,
        0.105,
        "Upload guard",
        [],
        PALETTE["blue_soft"],
        PALETTE["blue"],
    )


    add_box(
        ax,
        0.08,
        0.465,
        0.25,
        0.125,
        "quality_check.py",
        [],
        PALETTE["green_soft"],
        PALETTE["green"],
    )
    add_box(
        ax,
        0.39,
        0.465,
        0.25,
        0.125,
        "ocr_service.py",
        [],
        PALETTE["green_soft"],
        PALETTE["green"],
    )
    add_box(
        ax,
        0.70,
        0.465,
        0.19,
        0.125,
        "OCR text",
        [],
        "#FFFFFF",
        PALETTE["green"],
    )


    add_box(
        ax,
        0.08,
        0.17,
        0.23,
        0.12,
        "field_parser.py",
        [],
        PALETTE["gold_soft"],
        PALETTE["gold"],
    )
    add_box(
        ax,
        0.37,
        0.17,
        0.23,
        0.12,
        "id_card_parser.py",
        [],
        PALETTE["gold_soft"],
        PALETTE["gold"],
    )
    add_box(
        ax,
        0.66,
        0.17,
        0.22,
        0.12,
        "rule_check.py",
        [],
        PALETTE["gold_soft"],
        PALETTE["gold"],
    )


    add_arrow(ax, (0.26, 0.787), (0.31, 0.787))
    add_arrow(ax, (0.55, 0.787), (0.60, 0.787))
    add_arrow(ax, (0.71, 0.735), (0.22, 0.59), curve=0.05)
    add_arrow(ax, (0.75, 0.735), (0.51, 0.59), curve=0.02)
    add_arrow(ax, (0.64, 0.528), (0.70, 0.528))
    add_arrow(ax, (0.79, 0.465), (0.20, 0.29), curve=-0.08)
    add_arrow(ax, (0.79, 0.465), (0.49, 0.29), curve=-0.03)
    add_arrow(ax, (0.33, 0.525), (0.66, 0.24), curve=-0.12)
    add_arrow(ax, (0.31, 0.23), (0.66, 0.23))
    add_arrow(ax, (0.60, 0.23), (0.66, 0.23))
    add_arrow(ax, (0.88, 0.23), (0.95, 0.23))
    ax.text(0.945, 0.235, "JSON\nreview_result\nfields", fontsize=5.7, color=PALETTE["blue"], va="center", ha="left")


def draw_support_panel(ax: plt.Axes) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    add_panel_label(ax, "b", 0.0, 1.0)
    ax.text(0.07, 0.98, "Data, diagnostics and tests", fontsize=10.5, fontweight="bold", color=PALETTE["ink"], ha="left", va="top")
    ax.text(
        0.07,
        0.92,
        "Synthetic fixtures and test reports define the review surface without storing real customer documents.",
        fontsize=6.7,
        color=PALETTE["muted"],
        ha="left",
        va="top",
    )

    add_box(
        ax,
        0.08,
        0.70,
        0.34,
        0.14,
        "scripts/",
        [],
        PALETTE["violet_soft"],
        PALETTE["violet"],
    )
    add_box(
        ax,
        0.56,
        0.70,
        0.34,
        0.14,
        "data/",
        [],
        PALETTE["rose_soft"],
        PALETTE["rose"],
    )
    add_box(
        ax,
        0.08,
        0.45,
        0.34,
        0.14,
        "tests/",
        [],
        PALETTE["violet_soft"],
        PALETTE["violet"],
    )
    add_box(
        ax,
        0.56,
        0.45,
        0.34,
        0.14,
        "reports/",
        [],
        PALETTE["rose_soft"],
        PALETTE["rose"],
    )
    add_arrow(ax, (0.42, 0.77), (0.56, 0.77), "fixtures", color=PALETTE["violet"])
    add_arrow(ax, (0.73, 0.70), (0.73, 0.59), "samples", color=PALETTE["rose"])
    add_arrow(ax, (0.42, 0.52), (0.56, 0.52), "outputs", color=PALETTE["violet"])

    ax.text(0.08, 0.27, "Current quality-data mismatch", fontsize=8.0, fontweight="bold", color=PALETTE["ink"])
    ax.text(
        0.08,
        0.215,
        "normal images exceed the glare pixel-ratio threshold; bright images remain below the current >210 bright threshold.",
        fontsize=5.8,
        color=PALETTE["muted"],
        ha="left",
        va="top",
    )


def draw_result_panel(ax: plt.Axes) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    add_panel_label(ax, "c", 0.0, 1.0)
    ax.text(0.07, 0.95, "Decision semantics", fontsize=10.5, fontweight="bold", color=PALETTE["ink"], ha="left", va="top")

    decisions = [
        ("pass", "complete fields\nquality passes", PALETTE["green_soft"], PALETTE["green"]),
        ("review", "manual check\nneeded", PALETTE["gold_soft"], PALETTE["gold"]),
        ("reject", "invalid card\nnumber", PALETTE["rose_soft"], PALETTE["rose"]),
    ]
    x = 0.08
    for title, body, fill, edge in decisions:
        add_box(ax, x, 0.32, 0.25, 0.40, title, body.splitlines(), fill, edge, title_color=edge)
        x += 0.30


def main() -> None:
    fig = plt.figure(figsize=(9.6, 6.3))
    grid = fig.add_gridspec(
        2,
        2,
        width_ratios=[1.72, 1.0],
        height_ratios=[1.0, 0.55],
        left=0.035,
        right=0.985,
        bottom=0.06,
        top=0.93,
        hspace=0.20,
        wspace=0.12,
    )
    ax_a = fig.add_subplot(grid[:, 0])
    ax_b = fig.add_subplot(grid[0, 1])
    ax_c = fig.add_subplot(grid[1, 1])

    fig.text(0.035, 0.975, "Bank OCR review platform", fontsize=12.2, fontweight="bold", color=PALETTE["ink"], ha="left", va="top")
    fig.text(
        0.035,
        0.948,
        "A lightweight FastAPI pipeline couples image quality checks, OCR normalization, field parsing and rule-based review.",
        fontsize=7.2,
        color=PALETTE["muted"],
        ha="left",
        va="top",
    )

    draw_runtime_panel(ax_a)
    draw_support_panel(ax_b)
    draw_result_panel(ax_c)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{OUT_BASE}.svg", bbox_inches="tight")
    fig.savefig(f"{OUT_BASE}.pdf", bbox_inches="tight")
    fig.savefig(f"{OUT_BASE}.png", dpi=300, bbox_inches="tight")
    fig.savefig(f"{OUT_BASE}.tiff", dpi=600, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {OUT_BASE}.svg")
    print(f"Saved {OUT_BASE}.pdf")
    print(f"Saved {OUT_BASE}.png")
    print(f"Saved {OUT_BASE}.tiff")


if __name__ == "__main__":
    main()
