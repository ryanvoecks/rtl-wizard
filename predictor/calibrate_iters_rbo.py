#!/usr/bin/env python3
"""Train the predictor with RBO as the direct objective.

Same 145-iter, design-LOO setup as calibrate_iters.py, but instead of the
median-of-medians per-class delta estimator we run Nelder-Mead over
(beta_input, beta_output, beta_internal) to maximise mean per-iter RBO@K.
beta_both is derived (beta_input + beta_output - beta_internal) so the
3-parameter search matches the structure of the MoM model and stays
directly comparable.

Outputs to predictor/data/calibration_iters_rbo/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
from scipy.optimize import minimize

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from predictor.calibrate_iters import (  # noqa: E402
    P_VALUES,
    TOP_K,
    design_class_median,
    load_all,
    median_of_medians,
    per_iter_class_median,
)
from predictor.correction_calibration import (  # noqa: E402
    CAT_BOTH,
    CAT_INPUT,
    CAT_INTERNAL,
    CAT_OUTPUT,
    PORT_CLASSES,
    rbo_ext,
)

OUT_DIR = REPO / "predictor" / "data" / "calibration_iters_rbo"
RAW_ROUTE_DIR = REPO / "predictor" / "data" / "extraction" / "iter_baseline"

# port_class index for the packed per-iter arrays.
CLASS_IDX = {CAT_INPUT: 0, CAT_OUTPUT: 1, CAT_INTERNAL: 2, CAT_BOTH: 3}


def _read_raw_route_keys(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        out.append((parts[2], parts[3]))
    return out


def pack_iters(
    df_all: pd.DataFrame,
    raw_route_dir: Path | None = None,
) -> dict[str, dict[str, np.ndarray | list[tuple[str, str]]]]:
    """Per-iter numpy arrays so the inner RBO loop never touches pandas.

    If `raw_route_dir` is set, the route ranking for each iter is loaded from
    <iter_id>__6_final_raw.rpt (raw top-100, skip_async=0) instead of being
    derived from the synth-functional pool's route_slack column. This is
    the async-inclusive ground truth -- the proxy version drops async paths
    and anything outside the synth top-1000.
    """
    out: dict[str, dict] = {}
    for iid_h, sub in df_all.groupby("iter_id"):
        iid = str(iid_h)
        sub_df = cast(pd.DataFrame, sub)
        keys = list(zip(sub_df["start"].tolist(), sub_df["end"].tolist()))
        raw_route_keys: list[tuple[str, str]] | None = None
        if raw_route_dir is not None:
            rpt = raw_route_dir / f"{iid}__6_final_raw.rpt"
            if rpt.is_file():
                raw_route_keys = _read_raw_route_keys(rpt)
        out[iid] = {
            "design": str(sub_df["design"].iloc[0]),
            "slack_synth": sub_df["slack_synth_ns"].to_numpy(),
            "slack_route": sub_df["slack_route_ns"].to_numpy(),
            "class_idx": cast(pd.Series, sub_df["port_class"])
            .map(CLASS_IDX)
            .to_numpy(dtype=np.int8),
            "keys": keys,
            "raw_route_keys": raw_route_keys,
            # Cache the proxy route ranking once -- it does not depend on beta.
            "route_topk_cache": {},
        }
    return out


def _beta_array(b: dict[str, float]) -> np.ndarray:
    return np.array(
        [b[CAT_INPUT], b[CAT_OUTPUT], b[CAT_INTERNAL], b[CAT_BOTH]], dtype=np.float64
    )


def _expand(x: np.ndarray) -> np.ndarray:
    """3 free params -> 4 betas with b_both = b_in + b_out - b_int."""
    b_in, b_out, b_int = float(x[0]), float(x[1]), float(x[2])
    return np.array([b_in, b_out, b_int, b_in + b_out - b_int], dtype=np.float64)


def _iter_rbo_fast(packed: dict, beta_arr: np.ndarray, top_k: int, p: float) -> float:
    s_corr = packed["slack_synth"] + beta_arr[packed["class_idx"]]
    s_order = np.argsort(s_corr, kind="stable")
    s_keys = [packed["keys"][i] for i in s_order[:top_k].tolist()]
    raw = packed.get("raw_route_keys")
    if raw is not None:
        r_keys = raw[:top_k]
    else:
        cache = packed["route_topk_cache"]
        if top_k not in cache:
            r_order = np.argsort(packed["slack_route"], kind="stable")
            cache[top_k] = [packed["keys"][i] for i in r_order[:top_k].tolist()]
        r_keys = cache[top_k]
    return rbo_ext(s_keys, r_keys, p)


def _mean_rbo(
    iters: dict, iter_ids: list[str], beta_arr: np.ndarray, top_k: int, p: float
) -> float:
    """Per-design mean RBO, then mean across designs -- so each design carries
    equal weight regardless of its iter count (10 vs 12 across the dataset)."""
    by_design: dict[str, list[float]] = {}
    for iid in iter_ids:
        by_design.setdefault(iters[iid]["design"], []).append(
            _iter_rbo_fast(iters[iid], beta_arr, top_k, p)
        )
    return float(np.mean([float(np.mean(v)) for v in by_design.values()]))


def fit_rbo(
    iters: dict,
    iter_ids: list[str],
    top_k: int,
    p: float,
    *,
    x0: np.ndarray,
    xatol: float = 5e-4,
    fatol: float = 1e-5,
    maxiter: int = 400,
) -> tuple[dict[str, float], float, int]:
    """Maximise mean RBO via Nelder-Mead in 3D, return (betas, best_rbo, n_evals)."""

    def neg_obj(x: np.ndarray) -> float:
        return -_mean_rbo(iters, iter_ids, _expand(x), top_k, p)

    res = minimize(
        neg_obj,
        x0=x0,
        method="Nelder-Mead",
        options={"xatol": xatol, "fatol": fatol, "maxiter": maxiter, "adaptive": True},
    )
    b_in, b_out, b_int = float(res.x[0]), float(res.x[1]), float(res.x[2])
    betas = {
        CAT_INPUT: b_in,
        CAT_OUTPUT: b_out,
        CAT_INTERNAL: b_int,
        CAT_BOTH: b_in + b_out - b_int,
    }
    return betas, -float(res.fun), int(res.nfev)


def loo_rbo(
    df_all: pd.DataFrame,
    per_iter: pd.DataFrame,
    iters: dict,
    top_k: int,
    p_train: float,
) -> pd.DataFrame:
    designs = sorted(cast(pd.Series, df_all["design"]).unique())
    zero = np.zeros(4)
    rows = []
    for held in designs:
        train_ids = sorted(
            cast(pd.Series, df_all[df_all["design"] != held]["iter_id"]).unique()
        )
        held_ids = sorted(
            cast(pd.Series, df_all[df_all["design"] == held]["iter_id"]).unique()
        )

        # Warm start from the training-set MoM beta -- cheap and close to
        # the RBO optimum in practice, halves the optimizer's work.
        per_d_train = design_class_median(
            cast(pd.DataFrame, per_iter[per_iter["design"] != held])
        )
        mom_train = median_of_medians(per_d_train)
        x0 = np.array(
            [mom_train[CAT_INPUT], mom_train[CAT_OUTPUT], mom_train[CAT_INTERNAL]]
        )

        betas, train_rbo, nfev = fit_rbo(iters, train_ids, top_k, p_train, x0=x0)
        beta_arr = _beta_array(betas)

        row: dict[str, float | str | int] = {
            "held_out": held,
            "n_iters": len(held_ids),
            "n_train_iters": len(train_ids),
            "nfev": nfev,
            "train_rbo_target": train_rbo,
            "beta_input_ns": betas[CAT_INPUT],
            "beta_output_ns": betas[CAT_OUTPUT],
            "beta_internal_ns": betas[CAT_INTERNAL],
            "beta_both_derived_ns": betas[CAT_BOTH],
        }
        for p in P_VALUES:
            tag = f"p{int(round(p * 100)):03d}"
            row[f"rbo_raw_{tag}"] = _mean_rbo(iters, held_ids, zero, top_k, p)
            row[f"rbo_corr_{tag}"] = _mean_rbo(iters, held_ids, beta_arr, top_k, p)
        rows.append(row)
        rbo_tag = f"rbo_corr_p{int(round(p_train * 100)):03d}"
        b_in = betas[CAT_INPUT]
        b_out = betas[CAT_OUTPUT]
        b_int = betas[CAT_INTERNAL]
        print(
            f"  held={held:<16} nfev={nfev:>3}  "
            f"b=({b_in:+.3f},{b_out:+.3f},{b_int:+.3f})  "
            f"held_rbo@p={p_train}: {row[rbo_tag]:.3f}"
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument(
        "--p-train",
        type=float,
        default=0.9,
        help="RBO persistence used as the training objective (default 0.9)",
    )
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument(
        "--proxy-route",
        action="store_true",
        help="use the synth-functional pool's route_slack as the route ranking "
        "(legacy behaviour); default is raw async-inclusive route top-100",
    )
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print("loading 145 iters...")
    df = load_all()
    if df.empty:
        print("no data", file=sys.stderr)
        sys.exit(1)
    print(f"  iters loaded: {df['iter_id'].nunique()}")
    designs = sorted(cast(pd.Series, df["design"]).unique())
    print(f"  designs:      {designs}")
    print(f"  total rows:   {len(df)}")

    per_iter = per_iter_class_median(df)
    raw_dir = None if args.proxy_route else RAW_ROUTE_DIR
    route_desc = (
        "proxy (functional pool)" if raw_dir is None else "raw async-inclusive top-100"
    )
    print(f"  route ground truth: {route_desc}")
    iters = pack_iters(df, raw_route_dir=raw_dir)

    # Global in-sample fit (all 12 designs) for the headline beta.
    print("\nfitting global RBO-optimal beta on all 12 designs...")
    per_d_all = design_class_median(per_iter)
    mom_all = median_of_medians(per_d_all)
    x0 = np.array([mom_all[CAT_INPUT], mom_all[CAT_OUTPUT], mom_all[CAT_INTERNAL]])
    all_ids = sorted(cast(pd.Series, df["iter_id"]).unique())
    betas_global, train_rbo, nfev_global = fit_rbo(
        iters, all_ids, args.top_k, args.p_train, x0=x0
    )
    print(f"  nfev={nfev_global}  in-sample RBO@p={args.p_train} = {train_rbo:.3f}")
    print(f"  starting from MoM: ({x0[0]:+.3f}, {x0[1]:+.3f}, {x0[2]:+.3f})")
    print(
        f"  RBO-optimal      : "
        f"({betas_global[CAT_INPUT]:+.3f}, "
        f"{betas_global[CAT_OUTPUT]:+.3f}, "
        f"{betas_global[CAT_INTERNAL]:+.3f})"
    )

    pd.DataFrame(
        [{"port_class": c, "global_beta_ns": betas_global[c]} for c in PORT_CLASSES]
    ).to_csv(args.out_dir / "global_beta_rbo.csv", index=False)

    # Reference numbers for the comparison: in-sample RBO with MoM betas
    # so the headline can show "MoM vs RBO-trained" cleanly.
    beta_arr_mom = _beta_array(mom_all)
    zero = np.zeros(4)
    in_sample_baseline = {}
    for p in P_VALUES:
        in_sample_baseline[p] = {
            "raw": _mean_rbo(iters, all_ids, zero, args.top_k, p),
            "mom": _mean_rbo(iters, all_ids, beta_arr_mom, args.top_k, p),
            "rbo": _mean_rbo(iters, all_ids, _beta_array(betas_global), args.top_k, p),
        }

    print(f"\nrunning design-LOO (training objective p={args.p_train})...")
    loo_df = loo_rbo(df, per_iter, iters, args.top_k, args.p_train)
    loo_df.to_csv(args.out_dir / "loo_design.csv", index=False)

    print("\nLOO mean RBO across 12 held-out designs:")
    print(f"  {'p':<6} {'raw':>7} {'rbo':>7} {'gain':>7}")
    for p in P_VALUES:
        tag = f"p{int(round(p * 100)):03d}"
        raw = loo_df[f"rbo_raw_{tag}"].mean()
        corr = loo_df[f"rbo_corr_{tag}"].mean()
        print(f"  p={p:<4} {raw:>7.3f} {corr:>7.3f} {corr - raw:>+7.3f}")

    print("\nLOO beta spread (RBO-trained):")
    for c in ("beta_input_ns", "beta_output_ns", "beta_internal_ns"):
        arr = loo_df[c].to_numpy()
        rel = arr.std() / abs(arr.mean()) if abs(arr.mean()) > 1e-6 else float("inf")
        print(
            f"  {c:<20}  min={arr.min():+.3f}  max={arr.max():+.3f}  "
            f"std={arr.std():.3f}  std/|mean|={rel:.2f}"
        )

    headline = []
    headline.append(
        f"RBO-trained beta (mean per-iter RBO@K={args.top_k} p={args.p_train}, "
        f"all 12 in-sample):"
    )
    for c in PORT_CLASSES:
        headline.append(f"  beta[{c}] = {betas_global[c]:+.3f} ns")
    headline.append("")
    headline.append("In-sample mean RBO (all 12 designs, all iters):")
    headline.append(f"  {'p':<6} {'raw':>7} {'mom':>7} {'rbo':>7}")
    for p in P_VALUES:
        b = in_sample_baseline[p]
        headline.append(
            f"  p={p:<4} {b['raw']:>7.3f} {b['mom']:>7.3f} {b['rbo']:>7.3f}"
        )
    headline.append("")
    headline.append("LOO mean RBO across 12 held-out designs (RBO-trained):")
    for p in P_VALUES:
        tag = f"p{int(round(p * 100)):03d}"
        raw = loo_df[f"rbo_raw_{tag}"].mean()
        corr = loo_df[f"rbo_corr_{tag}"].mean()
        headline.append(
            f"  p={p}:  raw={raw:.3f}  corr={corr:.3f}  gain={corr - raw:+.3f}"
        )
    text = "\n".join(headline)
    (args.out_dir / "headline.txt").write_text(text + "\n")
    print()
    print(text)


if __name__ == "__main__":
    main()
