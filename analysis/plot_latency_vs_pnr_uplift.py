#!/usr/bin/env python3
"""Scatter of agent wall-clock vs PnR uplift, coloured by feedback mode.

For each (design, feedback) cell, take the geomean across epochs of:
  - total_time: agent wall-clock in seconds (from the .eval log;
    includes all thinking, tool calls, and -- for the iterative agent --
    the time spent generating synthesis/pnr feedback and running the
    testbench between iterations).
  - pnr_fmax_ratio = pnr_fmax / mean_seed_pnr_fmax. pnr_fmax is read
    from the iter named by `best_iter` in the matching
    analysis/data/llm_per_edit_<feedback>.csv -- so each feedback mode's
    pick comes from its own predictor (synth WNS for synth, predicted-
    PnR for predict, real post-route WS for pnr). The raw WNS/WS at
    that iter is then read directly from tmp/summary.json and divided
    by the 8-seed rewriter-sweep mean in analysis/data/variance.csv.

Both axes log-scaled; the fmax ratio is strictly positive (1.0 == no
change vs the seed-variance baseline).

Inputs:
  - analysis/data/llm_per_edit_{synth,predict,pnr}.csv (best_iter picks)
  - tmp/summary.json (per-iter metrics)
  - analysis/data/variance.csv (rewriter sweep means per design)
  - artifacts/llm/<run>/*.eval (Inspect logs for the wall-clock field)

Outputs:
  - analysis/plots/latency_vs_pnr_uplift.png
  - analysis/data/latency_vs_pnr_uplift.csv

Usage:
    uv run --with matplotlib python analysis/plot_latency_vs_pnr_uplift.py
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from inspect_ai.log import read_eval_log
from matplotlib import rcParams
from matplotlib.lines import Line2D

REPO = Path(__file__).resolve().parents[1]
SUMMARY_JSON = REPO / "tmp" / "summary.json"
VARIANCE_CSV = REPO / "analysis" / "data" / "variance.csv"
ARTIFACTS = REPO / "artifacts" / "llm"
PER_EDIT_CSVS = {
    "synth": REPO / "analysis" / "data" / "llm_per_edit_synth.csv",
    "predict": REPO / "analysis" / "data" / "llm_per_edit_predict.csv",
    "pnr": REPO / "analysis" / "data" / "llm_per_edit_pnr.csv",
}
OUT_PNG = REPO / "analysis" / "plots" / "latency_vs_pnr_uplift.png"
OUT_CSV = REPO / "analysis" / "data" / "latency_vs_pnr_uplift.csv"

TEXT_COLOR = "#222222"

TRAINING_DESIGNS = (
    "aes",
    "sha512",
    "double_fpu",
    "bitonic_sorter",
    "e203",
    "reed_solomon",
    "systolic_tpu",
    "viterbi",
    "verilog_axi",
    "uberddr3",
    "wb_dma",
    "jpeg_encoder",
)
TEST_DESIGNS = ("cordic", "fir", "wb_conmax", "modexp")
ALL_DESIGNS = TRAINING_DESIGNS + TEST_DESIGNS

FEEDBACKS = ("synth", "predict", "pnr")

# Feedback gets a colour. Blue -> orange -> red reads as a cost ramp,
# which matches the wall-clock trend.
FEEDBACK_COLORS: dict[str, str] = {
    "synth": "#1f77b4",
    "predict": "#2ca02c",
    "pnr": "#d62728",
}
FEEDBACK_LABELS: dict[str, str] = {
    "synth": "synth feedback",
    "predict": "predict feedback",
    "pnr": "post-route feedback",
}


def fmax_mhz(period_ns: float, ws_ns: float) -> float | None:
    cp = period_ns - ws_ns
    if cp <= 0:
        return None
    return 1000.0 / cp


def geomean(xs: list[float]) -> float:
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def load_variance(path: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            short = row["design"].split("/", 1)[1]
            out[short] = {"pnr_mean": float(row["pnr_fmax_mhz_mean"])}
    return out


def load_latencies(root: Path) -> dict[tuple[str, str, int], float]:
    """(feedback, design, epoch) -> wall-clock seconds.

    The viterbi rerun batch wins over the original PnR batch when both
    are present, matching the rest of the analysis pipeline.
    """
    out: dict[tuple[str, str, int], float] = {}
    for evf in sorted(glob.glob(str(root / "*" / "*.eval"))):
        run_dir = Path(evf).parent.name
        feedback = run_dir.split("_", 1)[0]
        is_rerun = "viterbi_rerun" in run_dir
        log = read_eval_log(evf)
        if log.samples is None:
            continue
        for s in log.samples:
            if s.total_time is None:
                continue
            key = (feedback, str(s.id), s.epoch)
            if key in out and not is_rerun:
                continue
            out[key] = s.total_time
    return out


def load_per_edit_picks(
    csvs: dict[str, Path],
) -> dict[str, list[dict]]:
    """feedback -> [{design, epoch, run, best_iter}, ...] for non-failed rows."""
    out: dict[str, list[dict]] = {}
    for fb, path in csvs.items():
        rows: list[dict] = []
        with path.open() as f:
            for r in csv.DictReader(f):
                if r["best_synth_pct"].startswith("fail") or r[
                    "best_pnr_pct"
                ].startswith("fail"):
                    continue
                rows.append(
                    {
                        "design": r["design"],
                        "epoch": int(r["epoch"]),
                        "run": r["run"],
                        "best_iter": int(r["best_iter"]),
                    }
                )
        out[fb] = rows
    return out


def pnr_fmax_at_iter(
    summary: dict, run: str, design: str, epoch: int, k: int
) -> float | None:
    b = summary["baselines"].get(design)
    if not b or not b.get("present"):
        return None
    period = b["period_ns"]
    needle = f"|{design}|epoch_{epoch}"
    for key, iters in summary["samples"].items():
        if not key.startswith(run + "|") or not key.endswith(needle):
            continue
        row = iters.get(str(k)) or iters.get(k)
        if row is None or row.get("pnr_ws_ns") is None:
            return None
        return fmax_mhz(period, row["pnr_ws_ns"])
    return None


def collect(
    summary: dict,
    variance: dict[str, dict[str, float]],
    latencies: dict[tuple[str, str, int], float],
    picks: dict[str, list[dict]],
) -> list[dict]:
    rows: list[dict] = []
    for design in ALL_DESIGNS:
        v = variance.get(design)
        if v is None:
            continue
        for fb in FEEDBACKS:
            pairs: list[tuple[float, float]] = []
            for p in picks[fb]:
                if p["design"] != design:
                    continue
                fp = pnr_fmax_at_iter(
                    summary, p["run"], design, p["epoch"], p["best_iter"]
                )
                if fp is None:
                    continue
                t = latencies.get((fb, design, p["epoch"]))
                if t is None:
                    continue
                pairs.append((t, fp / v["pnr_mean"]))
            if not pairs:
                continue
            rows.append(
                {
                    "design": design,
                    "feedback": fb,
                    "n_epochs": len(pairs),
                    "gm_total_time_s": geomean([p[0] for p in pairs]),
                    "gm_pnr_fmax_ratio": geomean([p[1] for p in pairs]),
                }
            )
    return rows


def _set_rc() -> None:
    rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["STIX Two Text", "STIXGeneral", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "axes.labelsize": 19,
            "axes.titlesize": 19,
            "axes.edgecolor": TEXT_COLOR,
            "axes.labelcolor": TEXT_COLOR,
            "axes.linewidth": 1.0,
            "xtick.color": TEXT_COLOR,
            "ytick.color": TEXT_COLOR,
            "xtick.labelsize": 15,
            "ytick.labelsize": 15,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.major.size": 5,
            "ytick.major.size": 5,
            "xtick.major.width": 1.0,
            "ytick.major.width": 1.0,
            "legend.fontsize": 14,
            "figure.dpi": 200,
            "savefig.dpi": 600,
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def _plot(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11.0, 7.5))

    ax.axhline(1.0, color="#888888", linewidth=0.6, linestyle="--", zorder=1)

    seen: set[str] = set()
    for r in rows:
        fb = r["feedback"]
        ax.scatter(
            r["gm_total_time_s"],
            r["gm_pnr_fmax_ratio"],
            color=FEEDBACK_COLORS[fb],
            marker="o",
            s=80,
            alpha=0.85,
            linewidths=0.6,
            edgecolors="black",
            zorder=3,
        )
        seen.add(fb)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Wall-clock per run (s)")
    ax.set_ylabel("Post-route fmax uplift")
    ax.grid(
        which="both",
        linestyle="-",
        linewidth=0.4,
        color="#d0d0d0",
        alpha=0.9,
        zorder=0,
    )
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    legend_handles = [
        Line2D(
            [],
            [],
            linestyle="",
            marker="o",
            markersize=9,
            markerfacecolor=FEEDBACK_COLORS[fb],
            markeredgecolor="black",
            markeredgewidth=0.6,
            label=FEEDBACK_LABELS[fb],
        )
        for fb in FEEDBACKS
        if fb in seen
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper left",
        frameon=False,
        handletextpad=0.6,
        borderpad=0.4,
    )

    fig.savefig(out_path)
    print(f"Wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--summary", type=Path, default=SUMMARY_JSON)
    ap.add_argument("--variance", type=Path, default=VARIANCE_CSV)
    ap.add_argument("--artifacts", type=Path, default=ARTIFACTS)
    ap.add_argument("--out", type=Path, default=OUT_PNG)
    ap.add_argument("--csv", type=Path, default=OUT_CSV)
    args = ap.parse_args()
    _set_rc()

    summary = json.loads(args.summary.read_text())
    variance = load_variance(args.variance)
    latencies = load_latencies(args.artifacts)
    picks = load_per_edit_picks(PER_EDIT_CSVS)
    rows = collect(summary, variance, latencies, picks)
    if not rows:
        raise SystemExit("no points to plot; check inputs")

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {args.csv} ({len(rows)} points)")

    _plot(rows, args.out)


if __name__ == "__main__":
    main()
