#!/usr/bin/env python3
"""Per-edit-category LLM uplift table.

For every valid edit, look up its category label from
`artifacts/llm/<run>/<design>/epoch_<n>/edit<k>_category.txt`, compute
iter-over-iter synth and post-route fmax ratios from `tmp/summary.json`,
and aggregate by category. The category file's first line may be
"Invalid" (we then read the original category from line 2 but skip the
edit); otherwise the first line is the category.

Columns:
  - n: number of valid edits in this category
  - gm_synth_uplift_pct: geomean over edits of
      (iter_k synth_fmax / iter_{k-1} synth_fmax) - 1
  - gm_pnr_uplift_pct: same for post-route
  - mean_synth_minus_pnr_pct: mean per-edit (synth_uplift - pnr_uplift)
  - n_round1..n_round4: tally of edits per round (sums to `n`)

Writes a CSV to `analysis/data/llm_category_uplift.csv` and prints a
pretty table to stdout.

Usage:
    uv run python analysis/llm_category_uplift.py
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
ARTIFACT_ROOT = REPO / "artifacts" / "llm"
OUT_CSV = REPO / "analysis" / "data" / "llm_category_uplift.csv"

NUM_ROUNDS = 4
# wb_dma's seed-variance baseline is pinned at 116 MHz (a synth-side
# degenerate case in the calibration); per-edit ratios off that baseline
# distort the category stats by an order of magnitude. Easier to drop it
# than to special-case the baseline.
EXCLUDE_DESIGNS = {"wb_dma"}

# Display order: substantive categories first by likely salience, then
# residuals trail. Anything we encounter outside this list is appended
# in alphabetical order at the end.
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


def fmax_mhz(period_ns: float, ws_ns: float | None) -> float | None:
    if ws_ns is None:
        return None
    cp = period_ns - ws_ns
    if cp <= 0:
        return None
    return 1000.0 / cp


def geomean(xs: list[float]) -> float:
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def _read_category(path: Path) -> str | None:
    """Return the canonical category for an edit, or None if invalid /
    missing. The first line is "Invalid" for skipped edits."""
    if not path.is_file():
        return None
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    if not lines:
        return None
    if lines[0] == "Invalid":
        return None
    return lines[0]


def collect(summary: dict) -> dict[str, dict]:
    baselines = summary["baselines"]
    samples = summary["samples"]
    per_cat: dict[str, dict] = {}

    for key, iters in samples.items():
        run, _, design, ep_str = key.split("|")
        if design in EXCLUDE_DESIGNS:
            continue
        b = baselines.get(design)
        if not b or not b.get("present"):
            continue
        period = b["period_ns"]
        base_s = b.get("synth_wns_ns")
        base_p = b.get("pnr_ws_ns")
        if base_s is None or base_p is None:
            continue

        iters_i = {int(k): row for k, row in iters.items()}
        # states indexed 0..NUM_ROUNDS; 0 = baseline.
        states: list[tuple[float | None, float | None]] = [(base_s, base_p)]
        for k in sorted(iters_i):
            row = iters_i[k]
            states.append((row.get("synth_wns_ns"), row.get("pnr_ws_ns")))

        epoch_dir = ARTIFACT_ROOT / run / design / ep_str
        for k in range(1, len(states)):
            ps, pp = states[k - 1]
            cs, cp = states[k]
            if ps is None or pp is None or cs is None or cp is None:
                continue
            prev_s = fmax_mhz(period, ps)
            prev_p = fmax_mhz(period, pp)
            cur_s = fmax_mhz(period, cs)
            cur_p = fmax_mhz(period, cp)
            if prev_s is None or prev_p is None or cur_s is None or cur_p is None:
                continue
            cat = _read_category(epoch_dir / f"edit{k}_category.txt")
            if cat is None:
                continue

            synth_ratio = cur_s / prev_s
            pnr_ratio = cur_p / prev_p
            s_up_pct = (synth_ratio - 1) * 100
            p_up_pct = (pnr_ratio - 1) * 100

            d = per_cat.setdefault(
                cat,
                {
                    "synth_ratios": [],
                    "pnr_ratios": [],
                    "delta_pcts": [],
                    "n": 0,
                    "n_by_round": {r: 0 for r in range(1, NUM_ROUNDS + 1)},
                },
            )
            d["synth_ratios"].append(synth_ratio)
            d["pnr_ratios"].append(pnr_ratio)
            d["delta_pcts"].append(s_up_pct - p_up_pct)
            d["n"] += 1
            d["n_by_round"][k] += 1

    return per_cat


def render(per_cat: dict[str, dict]) -> list[dict]:
    cats = [c for c in CATEGORY_ORDER if c in per_cat]
    for c in sorted(per_cat):
        if c not in cats:
            cats.append(c)

    rows: list[dict] = []
    for cat in cats:
        d = per_cat[cat]
        n = d["n"]
        row = {
            "category": cat,
            "n": n,
            "gm_synth_uplift_pct": (geomean(d["synth_ratios"]) - 1) * 100,
            "gm_pnr_uplift_pct": (geomean(d["pnr_ratios"]) - 1) * 100,
            "mean_synth_minus_pnr_pct": sum(d["delta_pcts"]) / n,
        }
        for r in range(1, NUM_ROUNDS + 1):
            row[f"n_round{r}"] = d["n_by_round"][r]
        rows.append(row)
    return rows


def print_table(rows: list[dict]) -> None:
    round_hdr = "  ".join(f"{'r' + str(r):>3}" for r in range(1, NUM_ROUNDS + 1))
    header = (
        f"{'category':<30}  {'n':>3}  "
        f"{'gm_synth%':>10}  {'gm_pnr%':>9}  {'mean_dp%':>9}  "
        f"{round_hdr}"
    )
    print(header)
    print("-" * len(header))
    all_synth: list[float] = []
    all_pnr: list[float] = []
    all_delta: list[float] = []
    tot_n = 0
    tot_by_round = {r: 0 for r in range(1, NUM_ROUNDS + 1)}
    for r in rows:
        n = r["n"]
        gs = r["gm_synth_uplift_pct"]
        gp = r["gm_pnr_uplift_pct"]
        md = r["mean_synth_minus_pnr_pct"]
        round_cells = "  ".join(
            f"{r[f'n_round{rr}']:>3}" for rr in range(1, NUM_ROUNDS + 1)
        )
        print(
            f"{r['category']:<30}  {n:>3}  "
            f"{gs:>+9.2f}%  {gp:>+8.2f}%  {md:>+8.2f}%  "
            f"{round_cells}"
        )
        all_synth.append(1 + gs / 100)
        all_pnr.append(1 + gp / 100)
        all_delta.append(md)
        tot_n += n
        for rr in range(1, NUM_ROUNDS + 1):
            tot_by_round[rr] += r[f"n_round{rr}"]
    print("-" * len(header))
    if tot_n:
        gs = (geomean(all_synth) - 1) * 100
        gp = (geomean(all_pnr) - 1) * 100
        md = sum(all_delta) / len(all_delta)
        round_cells = "  ".join(
            f"{tot_by_round[rr]:>3}" for rr in range(1, NUM_ROUNDS + 1)
        )
        print(
            f"{'overall':<30}  {tot_n:>3}  "
            f"{gs:>+9.2f}%  {gp:>+8.2f}%  {md:>+8.2f}%  "
            f"{round_cells}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--summary", type=Path, default=SUMMARY_JSON)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    args = ap.parse_args()

    if not args.summary.is_file():
        sys.exit(f"missing summary input: {args.summary}")

    summary = json.loads(args.summary.read_text())
    per_cat = collect(summary)
    rows = render(per_cat)
    if not rows:
        sys.exit("no rows produced; check summary/artifact inputs")

    print_table(rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {args.out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
