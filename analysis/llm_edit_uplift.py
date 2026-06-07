#!/usr/bin/env python3
"""Per-design LLM-eval uplift summary aggregated over individual edits.

Same shape as `llm_design_uplift.py`, but the unit of analysis is one
edit rather than one trajectory's best-synth iter. With 3 epochs of 4
edits each there are 12 edits per design; designs whose summary has an
iter with missing metrics drop both edges incident to that gap, so e203
and reed_solomon report n=10.

Each valid edit k is scored iter-over-iter: synth_uplift = (iter_k
synth_fmax / iter_{k-1} synth_fmax) - 1, and similarly for pnr. The
denominator for edit 1 is the canonical baseline run (iter_0). An edit
is valid iff both iter_{k-1} and iter_k have both stages reported.

Noise margin scales by sqrt(2) because both endpoints of an edit are
seed-variable: the variance of a ratio of two independent samples from
the same distribution is ~2 * Var(single sample), so the threshold on
"is this change above noise" widens by sqrt(2) relative to a comparison
against the noise-free seed mean (which is the per-design summary's
convention).

Columns:
  - n: number of valid edits (12 per design unless one or more iters
      are missing metrics)
  - gm_synth_uplift_pct: geomean over edits of
      (synth_fmax_iter_k / synth_fmax_iter_{k-1}) - 1
  - gm_pnr_uplift_pct: same for post-route, both iters from the chain
  - mean_synth_minus_pnr_pct: mean per-edit (synth_uplift - pnr_uplift)
  - both_gain: fraction of edits where synth_uplift > +noise AND
      pnr_uplift > +noise
  - synth_gain_pnr_within_noise: synth_uplift > +noise AND
      |pnr_uplift| <= noise
  - synth_gain_pnr_loss: synth_uplift > +noise AND
      pnr_uplift < -noise

Noise margin per design and per stage = NOISE_K * CV from `variance.csv`.
NOISE_K = 2.509 is the half-width of a 99% normal CI on a single sample.

Writes a CSV to `analysis/data/llm_edit_uplift.csv` and prints a pretty
table to stdout.
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
OUT_CSV = REPO / "analysis" / "data" / "llm_edit_uplift.csv"

NOISE_K = 2.509
# Iter-over-iter ratios compound two seed-variable single samples. The
# original threshold in llm_design_uplift scored "single sample vs mean of
# 8 seeds" -- variance sigma^2 * (1 + 1/8). For iter-over-iter we have
# variance 2 * sigma^2, so the threshold scales by sqrt(2 / (1 + 1/8))
# = sqrt(16/9) = 4/3 relative to the design-level noise width.
NOISE_SCALE = math.sqrt(2) / math.sqrt(1 + 1 / 8)


def fmax_mhz(period_ns: float, ws_ns: float) -> float | None:
    cp = period_ns - ws_ns
    if cp <= 0:
        return None
    return 1000.0 / cp


def geomean(xs: list[float]) -> float:
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def rank(xs: list[float]) -> list[float]:
    """Average-of-tied ranks."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(rank(xs), rank(ys))


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
        base_s = b.get("synth_wns_ns")
        base_p = b.get("pnr_ws_ns")
        if base_s is None or base_p is None:
            continue

        # Build a states array indexed by iter (0 = baseline). An edit k
        # is valid iff both states[k-1] and states[k] are fully reported,
        # mirroring the iter-over-iter rule from the per-edit corr table.
        iters_i = {int(k): row for k, row in iters.items()}
        states: list[tuple[float | None, float | None]] = [(base_s, base_p)]
        for k in sorted(iters_i):
            row = iters_i[k]
            states.append((row.get("synth_wns_ns"), row.get("pnr_ws_ns")))

        d = per_design.setdefault(
            name,
            {
                "synth_ratios": [],
                "pnr_ratios": [],
                "delta_pcts": [],
                "n_edits": 0,
                "n_both_gain": 0,
                "n_synth_gain_pnr_noise": 0,
                "n_synth_gain_pnr_loss": 0,
            },
        )

        for i in range(1, len(states)):
            ps, pp = states[i - 1]
            cs, cp = states[i]
            if None in (ps, pp, cs, cp):
                continue
            prev_s = fmax_mhz(period, ps)
            prev_p = fmax_mhz(period, pp)
            cur_s = fmax_mhz(period, cs)
            cur_p = fmax_mhz(period, cp)
            if None in (prev_s, prev_p, cur_s, cur_p):
                continue

            synth_ratio = cur_s / prev_s
            pnr_ratio = cur_p / prev_p
            s_up_pct = (synth_ratio - 1) * 100
            p_up_pct = (pnr_ratio - 1) * 100
            s_n_pct = NOISE_K * NOISE_SCALE * v["synth_cv"] * 100
            p_n_pct = NOISE_K * NOISE_SCALE * v["pnr_cv"] * 100

            d["synth_ratios"].append(synth_ratio)
            d["pnr_ratios"].append(pnr_ratio)
            d["delta_pcts"].append(s_up_pct - p_up_pct)
            d["n_edits"] += 1

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
        n = d["n_edits"]
        if n == 0:
            continue
        rho = spearman(d["synth_ratios"], d["pnr_ratios"])
        rows.append(
            {
                "design": name,
                "n": n,
                "gm_synth_uplift_pct": (geomean(d["synth_ratios"]) - 1) * 100,
                "gm_pnr_uplift_pct": (geomean(d["pnr_ratios"]) - 1) * 100,
                "mean_synth_minus_pnr_pct": sum(d["delta_pcts"]) / n,
                "spearman_synth_pnr": rho if rho is not None else float("nan"),
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
        f"{'spearman':>8}  "
        f"{'both_gain':>9}  {'syn+/pnr~':>9}  {'syn+/pnr-':>9}"
    )
    print(header)
    print("-" * len(header))
    tot_n = tot_both = tot_noise = tot_loss = 0
    all_synth: list[float] = []
    all_pnr: list[float] = []
    all_delta: list[float] = []
    all_rhos: list[float] = []
    for r in rows:
        n = r["n"]
        gs = r["gm_synth_uplift_pct"]
        gp = r["gm_pnr_uplift_pct"]
        md = r["mean_synth_minus_pnr_pct"]
        rho = r["spearman_synth_pnr"]
        b, w, ll = (
            r["n_both_gain"],
            r["n_synth_gain_pnr_within_noise"],
            r["n_synth_gain_pnr_loss"],
        )
        rho_s = f"{rho:+.3f}" if not math.isnan(rho) else "n/a"
        print(
            f"{r['design']:<16}  {n:>3}  "
            f"{gs:>+9.2f}%  {gp:>+8.2f}%  {md:>+8.2f}%  "
            f"{rho_s:>8}  "
            f"{b / n:>9.0%}  {w / n:>9.0%}  {ll / n:>9.0%}"
        )
        tot_n += n
        tot_both += b
        tot_noise += w
        tot_loss += ll
        all_synth.append(1 + gs / 100)
        all_pnr.append(1 + gp / 100)
        all_delta.append(md)
        if not math.isnan(rho):
            all_rhos.append(rho)
    print("-" * len(header))
    if tot_n:
        gs = (geomean(all_synth) - 1) * 100
        gp = (geomean(all_pnr) - 1) * 100
        md = sum(all_delta) / len(all_delta)
        rho_avg = sum(all_rhos) / len(all_rhos) if all_rhos else float("nan")
        rho_s = f"{rho_avg:+.3f}" if not math.isnan(rho_avg) else "n/a"
        print(
            f"{'overall':<16}  {tot_n:>3}  "
            f"{gs:>+9.2f}%  {gp:>+8.2f}%  {md:>+8.2f}%  "
            f"{rho_s:>8}  "
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
