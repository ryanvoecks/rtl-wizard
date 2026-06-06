#!/usr/bin/env python3
"""Plot the total distribution of edit categories.

Walks every `edit<n>_category.txt` under `artifacts/llm/<run>/<design>/
epoch_<m>/` and emits a single-series bar chart of category totals
(summed across all rounds) to `analysis/plots/edit_categories.{png,pdf}`.

Usage:
    uv run --with matplotlib python analysis/plot_edit_categories.py
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

REPO = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPO / "artifacts" / "llm"
NUM_ROUNDS = 4

# Display order, most-likely-most-frequent first so the chart reads
# left-to-right by salience. Residual buckets (Other, None) trail.
CATEGORY_ORDER = (
    "Pipeline insertion",
    "Combinational restructuring",
    "Parallelisation",
    "Logic simplification",
    "Fanout reduction",
    "Mixed",
    "Other",
    "None",
)
RESIDUAL_CATEGORIES = {"Other", "None"}

# Bold steel-blue for the substantive categories; same hue with reduced
# saturation for residual buckets, so the eye reads "these are catch-all".
PRIMARY_COLOR = "#3f7ac0"
RESIDUAL_COLOR = "#9bb5d4"
TEXT_COLOR = "#222222"
BAR_EDGE_COLOR = "#000000"
BAR_EDGE_WIDTH = 0.6


def _set_rc() -> None:
    """Paper-style rcParams. STIX ships with matplotlib so no system
    font dependency, and we get proper math/typography fallbacks."""
    rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["STIX Two Text", "STIXGeneral", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "axes.labelsize": 11,
            "axes.titlesize": 12,
            "axes.edgecolor": TEXT_COLOR,
            "axes.labelcolor": TEXT_COLOR,
            "axes.linewidth": 0.8,
            "xtick.color": TEXT_COLOR,
            "ytick.color": TEXT_COLOR,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.major.size": 3.5,
            "ytick.major.size": 3.5,
            "xtick.major.width": 0.8,
            "ytick.major.width": 0.8,
            "legend.fontsize": 9,
            "figure.dpi": 200,
            "savefig.dpi": 600,
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,  # editable text in PDF
            "ps.fonttype": 42,
        }
    )


def _collect() -> tuple[Counter, int]:
    """Sum category counts across every edit_n_category.txt found."""
    totals: Counter = Counter()
    n_epochs = 0
    for run_dir in sorted(ARTIFACT_ROOT.iterdir()):
        if not run_dir.is_dir():
            continue
        for design_dir in sorted(run_dir.iterdir()):
            if not design_dir.is_dir():
                continue
            for epoch_dir in sorted(design_dir.iterdir()):
                if not epoch_dir.is_dir():
                    continue
                if not (epoch_dir / "target_config.json").is_file():
                    continue
                n_epochs += 1
                for n in range(1, NUM_ROUNDS + 1):
                    p = epoch_dir / f"edit{n}_category.txt"
                    if not p.is_file():
                        continue
                    label = p.read_text().strip()
                    # Drop edits the categoriser flagged as "Invalid..."
                    # (model couldn't classify the diff). They are not
                    # part of the denominator we report below.
                    if label.startswith("Invalid"):
                        continue
                    totals[label] += 1
    return totals, n_epochs


def _plot(totals: Counter, n_epochs: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cats = [c for c in CATEGORY_ORDER if c in totals]
    for k in totals:
        if k not in cats:
            cats.append(k)
    counts = [totals[c] for c in cats]
    n_edits = sum(counts)
    colors = [
        RESIDUAL_COLOR if c in RESIDUAL_CATEGORIES else PRIMARY_COLOR for c in cats
    ]

    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    bars = ax.bar(
        cats,
        counts,
        color=colors,
        width=0.7,
        edgecolor=BAR_EDGE_COLOR,
        linewidth=BAR_EDGE_WIDTH,
        zorder=3,
    )

    # Bar-top counts; smaller, in dark grey -- present but unobtrusive.
    y_max = max(counts)
    for bar, count in zip(bars, counts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + y_max * 0.015,
            str(count),
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=TEXT_COLOR,
        )

    ax.set_xlabel("Edit category")
    ax.set_ylabel("Number of edits")
    ax.set_ylim(0, y_max * 1.12)

    # Restrained gridline, behind the bars.
    ax.grid(
        axis="y", linestyle="-", linewidth=0.4, color="#d0d0d0", alpha=0.9, zorder=1
    )
    ax.set_axisbelow(True)

    # Drop the top/right spines and lighten the remaining frame.
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(TEXT_COLOR)

    plt.setp(ax.get_xticklabels(), rotation=25, ha="right", rotation_mode="anchor")

    # Sample-size annotation, top-right, in the same style as a figure caption.
    ax.text(
        0.99,
        0.97,
        f"n = {n_edits} edits",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=11,
        color=TEXT_COLOR,
    )

    fig.tight_layout()
    fig.savefig(out_path)
    print(f"Wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default=str(REPO / "analysis" / "plots" / "edit_categories.png"),
    )
    args = ap.parse_args()
    _set_rc()
    totals, n_epochs = _collect()
    print(f"Scanned {n_epochs} epochs")
    print(f"Totals: {dict(totals)}")
    _plot(totals, n_epochs, Path(args.out))


if __name__ == "__main__":
    main()
