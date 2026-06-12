#!/usr/bin/env python3
"""Per-design retention ratio r = d_pnr / d_synth.

The supervisor pointed out that the absolute synth-vs-pnr gap conflates
magnitude with reliability: viterbi (60 pp gap) still retains 55% of
its synth gain at PnR, while uberddr3 (15 pp gap) washes out to ~3%.
The retention ratio r captures reliability directly.

We pool per epoch by taking the best-synth iter's fmax (from
llm_design_uplift) divided by the seed-variance mean baseline fmax,
then average epoch-level ratios via geomean. r = geomean(pnr_ratios) /
geomean(synth_ratios).

Designs split cleanly bimodal at r ~ 0.5 vs r ~ 0.1; the band between
is empty, so we label:

  reliable    r >= 0.5
  unreliable  r <= 0.15
  artifact    wb_dma -- seed-variance baseline is degenerate (CV=0,
              base_fmax = 116 MHz vs target 933 MHz). Quarantined.

Outputs:
  - analysis/data/llm_retention.csv (one row per design)
  - stdout table sorted by r
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DESIGN_UPLIFT = REPO / "analysis" / "data" / "llm_design_uplift.csv"
OUT_CSV = REPO / "analysis" / "data" / "llm_retention.csv"

RELIABLE_THRESH = 0.50
UNRELIABLE_THRESH = 0.15
ARTIFACT_DESIGNS = {"wb_dma"}  # baseline is degenerate, see docstring


def _label(r: float, design: str) -> str:
    if design in ARTIFACT_DESIGNS:
        return "artifact"
    if r >= RELIABLE_THRESH:
        return "reliable"
    if r <= UNRELIABLE_THRESH:
        return "unreliable"
    return "borderline"


def load_design_uplift(path: Path) -> list[dict]:
    if not path.is_file():
        sys.exit(f"missing {path}; run analysis/llm_design_uplift.py first")
    return list(csv.DictReader(path.open()))


def compute_retention(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        design = r["design"]
        gm_synth_pct = float(r["gm_synth_uplift_pct"])
        gm_pnr_pct = float(r["gm_pnr_uplift_pct"])
        synth_ratio = 1 + gm_synth_pct / 100
        pnr_ratio = 1 + gm_pnr_pct / 100
        if not math.isfinite(synth_ratio) or synth_ratio <= 0:
            ret = float("nan")
        else:
            # Use the ratios' deviations from 1 so r matches the
            # supervisor's "d_pnr / d_synth" framing.
            d_s = synth_ratio - 1
            d_p = pnr_ratio - 1
            ret = d_p / d_s if d_s != 0 else float("nan")
        out.append(
            {
                "design": design,
                "n": int(r["n"]),
                "gm_synth_pct": gm_synth_pct,
                "gm_pnr_pct": gm_pnr_pct,
                "retention": ret,
                "label": _label(ret, design),
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--in", dest="inp", type=Path, default=DESIGN_UPLIFT)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    args = ap.parse_args()

    rows = compute_retention(load_design_uplift(args.inp))
    rows.sort(
        key=lambda r: -(r["retention"] if not math.isnan(r["retention"]) else -99)
    )

    header = (
        f"{'design':<16}  {'n':>2}  {'synth%':>7}  {'pnr%':>7}  {'r':>6}  {'label':<10}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        ret_s = f"{r['retention']:+.2f}" if not math.isnan(r["retention"]) else "n/a"
        print(
            f"{r['design']:<16}  {r['n']:>2}  "
            f"{r['gm_synth_pct']:>+6.2f}%  {r['gm_pnr_pct']:>+6.2f}%  "
            f"{ret_s:>6}  {r['label']:<10}"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {args.out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
