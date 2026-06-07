#!/usr/bin/env python3
"""Per-design LLM-eval uplift summary vs seed-variance mean baseline.

For each (design, epoch) trajectory, pick the iter with the best
post-synth worst slack across the iters of the sample, then read both
the synth and pnr worst slack from THAT iter. The same iter is used for
both stages. Convert each to achievable fmax via
`fmax = 1000 / (T_target - WS_signed)`, and compare against the 8-seed
rewriter-sweep mean fmax from `analysis/data/variance.csv`. A trajectory
is counted only when the best-synth iter has both stages reported.

Columns:
  - n: number of valid trajectories (3 per design unless one is dropped)
  - gm_synth_uplift_pct: geomean over trajectories of
      (synth_fmax / mean_seed_synth_fmax) - 1, taken at the best-synth iter
  - gm_pnr_uplift_pct: geomean of (pnr_fmax / mean_seed_pnr_fmax) - 1,
      pnr_fmax taken from the SAME iter that maximised synth
  - mean_synth_minus_pnr_pct: mean per-trajectory (synth_uplift - pnr_uplift)
  - both_gain: fraction of trajectories where synth_uplift > +noise AND
      pnr_uplift > +noise (gain in both stages)
  - synth_gain_pnr_within_noise: synth_uplift > +noise AND
      |pnr_uplift| <= noise (synth improves, pnr lost in the noise)
  - synth_gain_pnr_loss: synth_uplift > +noise AND
      pnr_uplift < -noise (synth improves but pnr regresses)

Noise margin per design and per stage = NOISE_K * CV from `variance.csv`.
NOISE_K = 2.509 is the half-width of a 99% normal CI on a single sample.

Writes a CSV to `analysis/data/llm_design_uplift.csv` and prints a
pretty table to stdout.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY_JSON = REPO / "tmp" / "summary.json"
VARIANCE_CSV = REPO / "analysis" / "data" / "variance.csv"
OUT_CSV = REPO / "analysis" / "data" / "llm_design_uplift.csv"

NOISE_K = 2.509


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
            out[short] = {
                "synth_mean": float(row["synth_fmax_mhz_mean"]),
                "synth_cv": float(row["synth_fmax_mhz_cv"]),
                "pnr_mean": float(row["pnr_fmax_mhz_mean"]),
                "pnr_cv": float(row["pnr_fmax_mhz_cv"]),
            }
    return out


def collect(summary: dict, variance: dict[str, dict[str, float]]) -> dict[str, dict]:
    baselines = summary["baselines"]
    samples = summary["samples"]
    per_design: dict[str, dict] = {}

    for key, iters in samples.items():
        _, _, name, _ = key.split("|")
        b = baselines.get(name)
        v = variance.get(name)
        if not b or not b.get("present") or v is None:
            continue
        period = b["period_ns"]

        iters_i = {int(k): row for k, row in iters.items()}
        if not iters_i:
            continue
        # Pick the iter with the best synth WS, then read pnr WS from the
        # SAME iter. Skip iters that don't have both stages reported, so
        # the per-stage numbers are always comparable.
        candidates = [
            (k, row)
            for k, row in iters_i.items()
            if row.get("synth_wns_ns") is not None and row.get("pnr_ws_ns") is not None
        ]
        if not candidates:
            continue
        _, best = max(candidates, key=lambda kv: kv[1]["synth_wns_ns"])
        best_s = best["synth_wns_ns"]
        best_p = best["pnr_ws_ns"]

        f_synth_fmax = fmax_mhz(period, best_s)
        f_pnr_fmax = fmax_mhz(period, best_p)
        if f_synth_fmax is None or f_pnr_fmax is None:
            continue

        s_up_pct = (f_synth_fmax / v["synth_mean"] - 1) * 100
        p_up_pct = (f_pnr_fmax / v["pnr_mean"] - 1) * 100
        s_n_pct = NOISE_K * v["synth_cv"] * 100
        p_n_pct = NOISE_K * v["pnr_cv"] * 100

        d = per_design.setdefault(
            name,
            {
                "synth_ratios": [],
                "pnr_ratios": [],
                "delta_pcts": [],
                "n_traj": 0,
                "n_both_gain": 0,
                "n_synth_gain_pnr_noise": 0,
                "n_synth_gain_pnr_loss": 0,
            },
        )
        d["synth_ratios"].append(f_synth_fmax / v["synth_mean"])
        d["pnr_ratios"].append(f_pnr_fmax / v["pnr_mean"])
        d["delta_pcts"].append(s_up_pct - p_up_pct)
        d["n_traj"] += 1

        synth_gain = s_up_pct > s_n_pct
        if synth_gain and p_up_pct > p_n_pct:
            d["n_both_gain"] += 1
        if synth_gain and abs(p_up_pct) <= p_n_pct:
            d["n_synth_gain_pnr_noise"] += 1
        if synth_gain and p_up_pct < -p_n_pct:
            d["n_synth_gain_pnr_loss"] += 1

    return per_design


def render(per_design: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for name in sorted(per_design):
        d = per_design[name]
        n = d["n_traj"]
        if n == 0:
            continue
        rows.append(
            {
                "design": name,
                "n": n,
                "gm_synth_uplift_pct": (geomean(d["synth_ratios"]) - 1) * 100,
                "gm_pnr_uplift_pct": (geomean(d["pnr_ratios"]) - 1) * 100,
                "mean_synth_minus_pnr_pct": sum(d["delta_pcts"]) / n,
                "n_both_gain": d["n_both_gain"],
                "n_synth_gain_pnr_within_noise": d["n_synth_gain_pnr_noise"],
                "n_synth_gain_pnr_loss": d["n_synth_gain_pnr_loss"],
                "frac_both_gain": d["n_both_gain"] / n,
                "frac_synth_gain_pnr_within_noise": d["n_synth_gain_pnr_noise"] / n,
                "frac_synth_gain_pnr_loss": d["n_synth_gain_pnr_loss"] / n,
            }
        )
    return rows


def print_table(rows: list[dict]) -> None:
    header = (
        f"{'design':<16}  {'n':>3}  "
        f"{'gm_synth%':>10}  {'gm_pnr%':>9}  {'mean_dp%':>9}  "
        f"{'both_gain':>9}  {'syn+/pnr~':>9}  {'syn+/pnr-':>9}"
    )
    print(header)
    print("-" * len(header))
    tot_n = tot_both = tot_noise = tot_loss = 0
    all_synth: list[float] = []
    all_pnr: list[float] = []
    all_delta: list[float] = []
    for r in rows:
        n = r["n"]
        gs = r["gm_synth_uplift_pct"]
        gp = r["gm_pnr_uplift_pct"]
        md = r["mean_synth_minus_pnr_pct"]
        b, w, ll = (
            r["n_both_gain"],
            r["n_synth_gain_pnr_within_noise"],
            r["n_synth_gain_pnr_loss"],
        )
        print(
            f"{r['design']:<16}  {n:>3}  "
            f"{gs:>+9.2f}%  {gp:>+8.2f}%  {md:>+8.2f}%  "
            f"{b / n:>9.0%}  {w / n:>9.0%}  {ll / n:>9.0%}"
        )
        tot_n += n
        tot_both += b
        tot_noise += w
        tot_loss += ll
        # Recover ratios from the printed pct uplifts for the overall geomean.
        all_synth.append(1 + gs / 100)
        all_pnr.append(1 + gp / 100)
        all_delta.append(md)
    print("-" * len(header))
    if tot_n:
        # Note: overall geomean is over per-design geomeans (one row each),
        # which is the natural roll-up when designs have equal weight.
        gs = (geomean(all_synth) - 1) * 100
        gp = (geomean(all_pnr) - 1) * 100
        md = sum(all_delta) / len(all_delta)
        print(
            f"{'overall':<16}  {tot_n:>3}  "
            f"{gs:>+9.2f}%  {gp:>+8.2f}%  {md:>+8.2f}%  "
            f"{tot_both / tot_n:>9.0%}  "
            f"{tot_noise / tot_n:>9.0%}  "
            f"{tot_loss / tot_n:>9.0%}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--summary",
        type=Path,
        default=SUMMARY_JSON,
        help=f"summary.json input (default: {SUMMARY_JSON.relative_to(REPO)})",
    )
    ap.add_argument(
        "--variance",
        type=Path,
        default=VARIANCE_CSV,
        help=f"variance CSV input (default: {VARIANCE_CSV.relative_to(REPO)})",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=OUT_CSV,
        help=f"CSV output path (default: {OUT_CSV.relative_to(REPO)})",
    )
    args = ap.parse_args()

    if not args.summary.is_file():
        sys.exit(f"missing summary input: {args.summary}")
    if not args.variance.is_file():
        sys.exit(f"missing variance input: {args.variance}")

    summary = json.loads(args.summary.read_text())
    variance = load_variance(args.variance)
    per_design = collect(summary, variance)
    rows = render(per_design)
    if not rows:
        sys.exit("no per-design rows produced; check summary/variance inputs")

    print_table(rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {args.out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
