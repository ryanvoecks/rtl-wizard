#!/usr/bin/env python3
"""Line plot of per-edit uplift for each design's best sample.

For each of the 12 designs, pick the sample (epoch) whose synth fmax
peaks highest across its 4 iters, and plot that sample's percentage
uplift vs the canonical baseline at each edit round. Synth is drawn
solid, post-route dashed, in a colour per design.

Inputs:
  - tmp/summary.json: per-iter metrics dump produced by tmp/summarize.py
Outputs:
  - analysis/plots/best_sample_uplift.png

Usage:
    uv run --with matplotlib python analysis/plot_best_sample_uplift.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
from matplotlib.lines import Line2D

REPO = Path(__file__).resolve().parents[1]
SUMMARY_JSON = REPO / "tmp" / "summary.json"
OUT_PNG = REPO / "analysis" / "plots" / "best_sample_uplift.png"

NUM_ROUNDS = 4
TEXT_COLOR = "#222222"
Y_CLIP = 150

# Mapping from summary.json's short design name to (display name, type).
DESIGN_META: dict[str, tuple[str, str]] = {
    "aes": ("AES block cipher", "Datapath"),
    "verilog_axi": ("AXI crossbar", "Interconnect"),
    "bitonic_sorter": ("Bitonic sorter", "Datapath"),
    "uberddr3": ("DDR3 controller", "Control"),
    "double_fpu": ("Double FPU", "Datapath"),
    "e203": ("e203 CPU", "Mixed"),
    "jpeg_encoder": ("JPEG encoder", "Mixed"),
    "reed_solomon": ("RS encoder/decoder", "Datapath"),
    "sha512": ("SHA-512 hash core", "Datapath"),
    "systolic_tpu": ("TPU systolic array", "Datapath"),
    "viterbi": ("Viterbi decoder", "Mixed"),
    "wb_dma": ("WB DMA bridge", "Control"),
}
# Panel order (top-left, top-right, bottom-left, bottom-right).
TYPE_ORDER = ("Datapath", "Mixed", "Control", "Interconnect")

# A distinct colour per design, paired so panel colours never repeat.
# Hand-picked from tab20 / Set1 to keep neighbours visually separable.
DESIGN_COLORS: dict[str, str] = {
    "aes": "#1f77b4",  # blue
    "bitonic_sorter": "#ff7f0e",  # orange
    "double_fpu": "#2ca02c",  # green
    "reed_solomon": "#d62728",  # red
    "sha512": "#9467bd",  # purple
    "systolic_tpu": "#8c564b",  # brown
    "e203": "#17becf",  # teal
    "jpeg_encoder": "#bcbd22",  # olive
    "viterbi": "#e377c2",  # pink
    "uberddr3": "#7f7f7f",  # grey
    "wb_dma": "#ff9896",  # salmon
    "verilog_axi": "#393b79",  # navy
}


def fmax_mhz(period_ns: float, ws_ns: float | None) -> float | None:
    if ws_ns is None:
        return None
    cp = period_ns - ws_ns
    if cp <= 0:
        return None
    return 1000.0 / cp


def _set_rc() -> None:
    rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["STIX Two Text", "STIXGeneral", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "axes.labelsize": 19,
            "axes.titlesize": 20,
            "axes.edgecolor": TEXT_COLOR,
            "axes.labelcolor": TEXT_COLOR,
            "axes.linewidth": 1.0,
            "xtick.color": TEXT_COLOR,
            "ytick.color": TEXT_COLOR,
            "xtick.labelsize": 16,
            "ytick.labelsize": 16,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.major.size": 5,
            "ytick.major.size": 5,
            "xtick.major.width": 1.0,
            "ytick.major.width": 1.0,
            "legend.fontsize": 14.5,
            "figure.dpi": 200,
            "savefig.dpi": 600,
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def _pick_best_sample(samples: dict, baselines: dict) -> dict[str, dict]:
    """For each design, return the (epoch's) per-iter metric series whose
    peak synth fmax is highest."""
    by_design: dict[str, list[tuple[float, dict, dict]]] = {}
    for key, iters in samples.items():
        _, _, name, _ = key.split("|")
        b = baselines.get(name)
        if not b or not b.get("present"):
            continue
        period = b["period_ns"]
        if b.get("synth_wns_ns") is None or b.get("pnr_ws_ns") is None:
            continue

        iters_i = {int(k): row for k, row in iters.items()}
        synth_fs = [fmax_mhz(period, r.get("synth_wns_ns")) for r in iters_i.values()]
        peaks = [f for f in synth_fs if f is not None]
        if not peaks:
            continue
        peak = max(peaks)
        by_design.setdefault(name, []).append((peak, b, iters_i))

    out: dict[str, dict] = {}
    for name, candidates in by_design.items():
        peak, b, iters_i = max(candidates, key=lambda c: c[0])
        out[name] = {"baseline": b, "iters": iters_i, "peak_synth_fmax": peak}
    return out


def _series(
    b: dict, iters_i: dict[int, dict], stage: str
) -> tuple[list[int], list[float]]:
    """Build (x, y_pct) lists for one stage in {'synth', 'pnr'}.

    x: edit round (0 = baseline through NUM_ROUNDS), y: pct uplift vs
    baseline. Iters with missing metrics are dropped from the series,
    leaving a gap in the line.
    """
    period = b["period_ns"]
    base_ws = b["synth_wns_ns"] if stage == "synth" else b["pnr_ws_ns"]
    base_fmax = fmax_mhz(period, base_ws)
    assert base_fmax is not None
    key = "synth_wns_ns" if stage == "synth" else "pnr_ws_ns"

    xs = [0]
    ys = [0.0]
    for k in range(1, NUM_ROUNDS + 1):
        row = iters_i.get(k)
        if row is None:
            continue
        f = fmax_mhz(period, row.get(key))
        if f is None:
            continue
        xs.append(k)
        ys.append((f / base_fmax - 1) * 100)
    return xs, ys


def _plot(per_design: dict[str, dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Bucket designs by type, preserving DESIGN_META iteration order so
    # the legend rows within a panel read in a stable sequence.
    by_type: dict[str, list[str]] = {t: [] for t in TYPE_ORDER}
    for name, (_, dtype) in DESIGN_META.items():
        if name in per_design:
            by_type[dtype].append(name)

    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9.4), sharex=True, sharey=True)
    axes_flat = axes.flatten()

    for ax, dtype in zip(axes_flat, TYPE_ORDER):
        names = by_type[dtype]
        legend_handles = []
        for name in names:
            d = per_design[name]
            b = d["baseline"]
            iters_i = d["iters"]
            display, _ = DESIGN_META[name]
            c = DESIGN_COLORS[name]

            x_s, y_s = _series(b, iters_i, "synth")
            x_p, y_p = _series(b, iters_i, "pnr")
            ax.plot(
                x_s,
                y_s,
                color=c,
                linestyle="-",
                linewidth=1.8,
                marker="o",
                markersize=4.0,
                zorder=3,
            )
            ax.plot(
                x_p,
                y_p,
                color=c,
                linestyle="--",
                linewidth=1.6,
                marker="o",
                markersize=3.4,
                zorder=3,
            )
            legend_handles.append(
                Line2D([], [], color=c, linestyle="-", linewidth=2.0, label=display)
            )

        ax.axhline(0, color="#888888", linewidth=0.6, zorder=1)
        ax.set_title(dtype, color=TEXT_COLOR, pad=6)
        ax.grid(
            axis="y", linestyle="-", linewidth=0.4, color="#d0d0d0", alpha=0.9, zorder=1
        )
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        if legend_handles:
            ax.legend(
                handles=legend_handles,
                loc="upper left",
                frameon=False,
                handlelength=1.8,
                borderpad=0.3,
            )

    axes_flat[0].set_xticks(range(0, NUM_ROUNDS + 1))
    axes_flat[0].set_xlim(-0.2, NUM_ROUNDS + 0.2)
    axes_flat[0].set_ylim(top=Y_CLIP)

    fig.supxlabel("Edit round (0 = baseline)", fontsize=20, color=TEXT_COLOR)
    fig.supylabel("fmax uplift vs baseline (%)", fontsize=20, color=TEXT_COLOR)

    # Style/stage legend at the figure level (line-style key).
    style_handles = [
        Line2D([], [], color=TEXT_COLOR, linestyle="-", linewidth=2.0, label="synth"),
        Line2D(
            [], [], color=TEXT_COLOR, linestyle="--", linewidth=1.7, label="post-route"
        ),
    ]
    fig.legend(
        handles=style_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.01),
        ncols=2,
        frameon=False,
        fontsize=17,
    )

    fig.tight_layout(rect=(0.02, 0.02, 1, 0.96))
    fig.savefig(out_path)
    print(f"Wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--summary", type=Path, default=SUMMARY_JSON)
    ap.add_argument("--out", type=Path, default=OUT_PNG)
    args = ap.parse_args()
    _set_rc()
    data = json.loads(args.summary.read_text())
    per_design = _pick_best_sample(data["samples"], data["baselines"])
    _plot(per_design, args.out)


if __name__ == "__main__":
    main()
