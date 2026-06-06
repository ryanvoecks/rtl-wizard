#!/usr/bin/env python3
"""Plot the total distribution of edit categories.

Walks every `edit<n>_category.txt` under `artifacts/llm/<run>/<design>/
epoch_<m>/` and emits a single-series bar chart of category totals
(summed across all 4 rounds) to `analysis/edit_categories.png`.

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

REPO = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPO / "artifacts" / "llm"
NUM_ROUNDS = 4

# Display order, most-likely-most-frequent first so the chart reads
# left-to-right by salience.
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
                    totals[p.read_text().strip()] += 1
    return totals, n_epochs


def _plot(totals: Counter, n_epochs: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cats = [c for c in CATEGORY_ORDER if c in totals]
    # Stragglers (any label that escaped CATEGORY_ORDER) get appended.
    for k in totals:
        if k not in cats:
            cats.append(k)
    counts = [totals[c] for c in cats]
    n_edits = sum(counts)

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(cats, counts, color="#1f77b4")
    for bar, count in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.5,
                str(count), ha="center", va="bottom", fontsize=9)

    ax.set_ylabel("Number of edits")
    ax.set_title(
        f"Edit categories ({n_epochs} epochs x {NUM_ROUNDS} rounds = {n_edits} edits)"
    )
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", linestyle=":", alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"Wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default=str(REPO / "analysis" / "plots" / "edit_categories.png"),
    )
    args = ap.parse_args()
    totals, n_epochs = _collect()
    print(f"Scanned {n_epochs} epochs")
    print(f"Totals: {dict(totals)}")
    _plot(totals, n_epochs, Path(args.out))


if __name__ == "__main__":
    main()
