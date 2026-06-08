#!/usr/bin/env python3
"""Per-design RBO table across the three pipeline stages and two persistences.

For each design, computes top-100 RBO between corrected-synth and raw route
under three configurations:
  - raw            -- no filter, no correction
  - post_async     -- synth-side async/test endpoint quarantine (filter only)
  - post_io        -- async filter + median-of-medians I/O correction
                      (β_input=+0.226, β_output=-0.323, β_internal=-0.050,
                       β_both=-0.047 derived as β_in+β_out-β_int)
at p in {0.6, 0.9}. The route side is unfiltered everywhere -- it is the
ground truth -- so the quarantine and correction only ever change the
synth-side ranking.

Inputs:
  - analysis/data/path_slack_union/<design>.tsv
  - analysis/data/logical_blocks/<design>__{1_synth,6_final}.rpt
  - analysis/data/critical_paths/<design>__{1_synth,6_final}.tsv

Output:
  - analysis/data/correction/per_design_rbo_summary.csv

Usage:
    uv run --with pandas --with numpy --with statsmodels --with scipy \\
        python analysis/correction_per_design_summary.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from predictor.correction_calibration import (  # noqa: E402
    CAT_BOTH,
    CAT_INPUT,
    CAT_INTERNAL,
    CAT_OUTPUT,
    PORT_CLASSES,
    UNION_DIR,
    _design_rbo,
    load_all,
)

OUT_CSV = REPO / "predictor" / "data" / "calibration" / "per_design_rbo_summary.csv"
TOP_K = 100

# Median-of-medians β with β_internal free and β_both derived via additivity.
# Frozen from the all-12 calibration run; in-sample (one set of β for every
# design row -- a per-design LOO refit would barely change anything since
# β_input and β_output are stable to 3 sig figs across folds).
BETAS = {
    CAT_INPUT: +0.226,
    CAT_OUTPUT: -0.323,
    CAT_INTERNAL: -0.050,
    CAT_BOTH: +0.226 + (-0.323) - (-0.050),  # = -0.047
}
ZERO = {c: 0.0 for c in PORT_CLASSES}


def main() -> None:
    designs = sorted(p.stem for p in UNION_DIR.glob("*.tsv"))
    df_all = load_all(designs)

    rows: list[dict[str, object]] = []
    for design in designs:
        df_d = cast(pd.DataFrame, df_all[df_all["design"] == design])
        row: dict[str, object] = {"design": design}
        for p in (0.6, 0.9, 0.96):
            tag = f"p{int(round(p * 100)):03d}"
            row[f"rbo_raw_{tag}"] = _design_rbo(
                df_d, ZERO, TOP_K, p, filter_synth=False
            )
            row[f"rbo_post_async_{tag}"] = _design_rbo(
                df_d, ZERO, TOP_K, p, filter_synth=True
            )
            row[f"rbo_post_io_{tag}"] = _design_rbo(
                df_d, BETAS, TOP_K, p, filter_synth=True
            )
        rows.append(row)

    df = pd.DataFrame(rows)
    # Mean row at the bottom.
    mean_row: dict[str, object] = {"design": "MEAN"}
    for c in df.columns:
        if c == "design":
            continue
        mean_row[c] = float(cast(float, df[c].mean()))
    df = pd.concat([df, pd.DataFrame([mean_row])], ignore_index=True)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False, float_format="%.4f")

    fmt = df.copy()
    for c in fmt.columns:
        if c != "design":
            fmt[c] = fmt[c].map(lambda v: f"{v:.3f}")
    print(f"β used: {BETAS}\n")
    print(fmt.to_string(index=False))
    try:
        rel = OUT_CSV.relative_to(REPO)
    except ValueError:
        rel = OUT_CSV
    print(f"\nWrote {rel}")


if __name__ == "__main__":
    main()
