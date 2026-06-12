#!/usr/bin/env python3
"""Per-design RBO table over the 140-iter dataset, async-inclusive ground truth.

Columns per design (and total/mean row):
  baseline_rbo            -- raw synth top-100 (skip_async=0) ranking
  async_corrected_rbo     -- functional synth top-1000, sort by raw slack, top-100
  mom_rbo                 -- functional pool, sort by MoM-corrected slack, top-100
  rbo_rbo                 -- functional pool, sort by RBO-corrected slack, top-100

All four are scored against the same ground truth: raw route top-100
(skip_async=0) from predictor/data/extraction/iter_baseline/. betas are
trained design-LOO so per-row scores are out-of-sample.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from predictor.calibrate_iters import (  # noqa: E402
    TOP_K,
    design_class_median,
    load_all,
    median_of_medians,
    per_iter_class_median,
)
from predictor.calibrate_iters_rbo import (  # noqa: E402
    RAW_ROUTE_DIR,
    fit_rbo,
    pack_iters,
)
from predictor.correction_calibration import (  # noqa: E402
    CAT_BOTH,
    CAT_INPUT,
    CAT_INTERNAL,
    CAT_OUTPUT,
    rbo_ext,
)

BASELINE_DIR = REPO / "predictor" / "data" / "extraction" / "iter_baseline"
OUT_PATH = (
    REPO / "predictor" / "data" / "calibration_iters_rbo" / "per_design_table.csv"
)


def _read_rpt_pairs(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        out.append((parts[2], parts[3]))
    return out


def _functional_ranking(
    packed: dict, betas: dict[str, float], top_k: int
) -> list[tuple[str, str]]:
    """Rank the functional pool by raw_synth_slack + beta[class], take top_k."""
    beta_arr = np.array(
        [betas[CAT_INPUT], betas[CAT_OUTPUT], betas[CAT_INTERNAL], betas[CAT_BOTH]],
        dtype=np.float64,
    )
    s_corr = packed["slack_synth"] + beta_arr[packed["class_idx"]]
    order = np.argsort(s_corr, kind="stable")
    return [packed["keys"][i] for i in order[:top_k].tolist()]


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--p", type=float, default=0.9)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    print(f"loading 140-iter dataset, scoring LOO RBO@K={args.top_k} p={args.p}")
    df = load_all()
    # raw async-inclusive route top-100 is the ground truth for both the
    # RBO optimizer's training objective and the final scoring.
    iters = pack_iters(df, raw_route_dir=RAW_ROUTE_DIR)
    per_iter = per_iter_class_median(df)

    # Per-iter raw extractions (loaded once).
    raw_synth: dict[str, list[tuple[str, str]]] = {}
    raw_route: dict[str, list[tuple[str, str]]] = {}
    missing = []
    for iid in iters:
        s = BASELINE_DIR / f"{iid}__1_synth_raw.rpt"
        r = BASELINE_DIR / f"{iid}__6_final_raw.rpt"
        if not s.is_file() or not r.is_file():
            missing.append(iid)
            continue
        raw_synth[iid] = _read_rpt_pairs(s)[: args.top_k]
        raw_route[iid] = _read_rpt_pairs(r)[: args.top_k]
    if missing:
        print(f"  WARNING: {len(missing)} iters missing raw rpts -- skipping them")
        for m in missing[:5]:
            print(f"    {m}")
    valid_iters = sorted(raw_synth.keys())
    print(f"  iters with both rpts: {len(valid_iters)}")

    rows = []
    designs = sorted(cast(pd.Series, df["design"]).unique())
    for d in designs:
        # Train MoM and RBO beta on the other 11 designs' iters only.
        train_per_iter = cast(pd.DataFrame, per_iter[per_iter["design"] != d])
        mom_loo = median_of_medians(design_class_median(train_per_iter))
        train_ids = sorted(cast(pd.Series, df[df["design"] != d]["iter_id"]).unique())
        x0 = np.array([mom_loo[CAT_INPUT], mom_loo[CAT_OUTPUT], mom_loo[CAT_INTERNAL]])
        rbo_loo, _, nfev = fit_rbo(iters, train_ids, args.top_k, args.p, x0=x0)

        zero_betas = {c: 0.0 for c in (CAT_INPUT, CAT_OUTPUT, CAT_INTERNAL, CAT_BOTH)}
        held_ids = [iid for iid in valid_iters if iters[iid]["design"] == d]
        per_iter_rbos = {"baseline": [], "async": [], "mom": [], "rbo": []}
        for iid in held_ids:
            truth = raw_route[iid]
            per_iter_rbos["baseline"].append(rbo_ext(raw_synth[iid], truth, args.p))
            per_iter_rbos["async"].append(
                rbo_ext(
                    _functional_ranking(iters[iid], zero_betas, args.top_k),
                    truth,
                    args.p,
                )
            )
            per_iter_rbos["mom"].append(
                rbo_ext(
                    _functional_ranking(iters[iid], mom_loo, args.top_k),
                    truth,
                    args.p,
                )
            )
            per_iter_rbos["rbo"].append(
                rbo_ext(
                    _functional_ranking(iters[iid], rbo_loo, args.top_k),
                    truth,
                    args.p,
                )
            )

        baseline = float(np.mean(per_iter_rbos["baseline"]))
        async_corr = float(np.mean(per_iter_rbos["async"]))
        mom_score = float(np.mean(per_iter_rbos["mom"]))
        rbo_score = float(np.mean(per_iter_rbos["rbo"]))
        rows.append(
            {
                "design": d,
                "n_iters": len(held_ids),
                "baseline_rbo": baseline,
                "async_corrected_rbo": async_corr,
                "mom_rbo": mom_score,
                "rbo_rbo": rbo_score,
                "mom_gain": mom_score - async_corr,
                "rbo_gain": rbo_score - async_corr,
                "mom_beta_input": mom_loo[CAT_INPUT],
                "mom_beta_output": mom_loo[CAT_OUTPUT],
                "mom_beta_internal": mom_loo[CAT_INTERNAL],
                "rbo_beta_input": rbo_loo[CAT_INPUT],
                "rbo_beta_output": rbo_loo[CAT_OUTPUT],
                "rbo_beta_internal": rbo_loo[CAT_INTERNAL],
                "rbo_nfev": nfev,
            }
        )
        print(
            f"  held={d:<16}  n={len(held_ids):>2}  "
            f"baseline={baseline:.3f}  async={async_corr:.3f}  "
            f"mom={mom_score:.3f}  rbo={rbo_score:.3f}"
        )

    table = pd.DataFrame(rows)
    total: dict[str, object] = {
        "design": "MEAN (iter-weighted)",
        "n_iters": int(cast(float, table["n_iters"].sum())),
    }
    for col in (
        "baseline_rbo",
        "async_corrected_rbo",
        "mom_rbo",
        "rbo_rbo",
        "mom_gain",
        "rbo_gain",
    ):
        total[col] = float(
            np.average(table[col].to_numpy(), weights=table["n_iters"].to_numpy())
        )
    plain: dict[str, object] = {
        "design": "MEAN (per-design)",
        "n_iters": int(cast(float, table["n_iters"].mean())),
    }
    for col in (
        "baseline_rbo",
        "async_corrected_rbo",
        "mom_rbo",
        "rbo_rbo",
        "mom_gain",
        "rbo_gain",
    ):
        plain[col] = float(cast(float, table[col].mean()))
    table = pd.concat([table, pd.DataFrame([plain, total])], ignore_index=True)
    table.to_csv(args.out, index=False)

    show_cols = [
        "design",
        "n_iters",
        "baseline_rbo",
        "async_corrected_rbo",
        "mom_rbo",
        "rbo_rbo",
        "mom_gain",
        "rbo_gain",
    ]
    fmt = table[show_cols].copy()
    for c in fmt.columns:
        if c in ("design", "n_iters"):
            continue
        fmt[c] = cast(pd.Series, fmt[c]).map(lambda v: f"{v:+.3f}")
    print()
    print(fmt.to_string(index=False))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
