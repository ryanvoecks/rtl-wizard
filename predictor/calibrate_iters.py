#!/usr/bin/env python3
"""Train the predictor on the 145-iter dataset, design-LOO.

For each iter we have a top-1000 functional pool with synth+route slacks.
Iters are grouped by their parent design (aes, e203, ...); LOO holds out
all iters of one design at a time so β is never trained on data from the
held-out design.

Aggregation:
  per (iter, port_class) median Δ
    -> per (design, port_class) median (median of its iters' medians)
       -> per port_class global median across in-sample designs
β_input / β_output / β_internal are estimated independently; β_both is
derived β_input + β_output - β_internal.

RBO is computed per iter on its own 1000-pool: corrected-synth top-100 vs
route top-100 (both drawn from that iter's pool). Per-design RBO is the
mean over the design's iters.

Outputs to predictor/data/calibration_iters/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from predictor.correction_calibration import (  # noqa: E402
    CAT_BOTH, CAT_INPUT, CAT_INTERNAL, CAT_OUTPUT, PORT_CLASSES,
    _endpoint_pin, _load_ports, _port_class_for, rbo_ext,
)

ITER_DIR = REPO / "predictor" / "data" / "extraction" / "iterations"
OUT_DIR = REPO / "predictor" / "data" / "calibration_iters"
ART_ROOT = REPO / "artifacts" / "llm"
TOP_K = 100
P_VALUES = (0.6, 0.9, 0.96)


def _outdated_iter_ids(iter_ids: list[str]) -> set[str]:
    """Drop the older copy when (design, epoch, iter) appears in multiple runs."""
    by_key: dict[tuple[str, str, str], list[str]] = {}
    for iid in iter_ids:
        _, _, design, epoch, iter_name = _split_iter_id(iid)
        by_key.setdefault((design, epoch, iter_name), []).append(iid)
    drops: set[str] = set()
    for ids in by_key.values():
        if len(ids) > 1:
            ids.sort()  # iter_id starts with timestamp -> lex order is chronological
            drops.update(ids[:-1])
    return drops


def _invalid_iter_ids(iter_ids: list[str]) -> set[str]:
    """Iters whose llm_category artifact starts with 'Invalid'."""
    batch_for_design: dict[str, str] = {}
    if ART_ROOT.is_dir():
        for batch_dir in ART_ROOT.iterdir():
            if not batch_dir.is_dir():
                continue
            for design_dir in batch_dir.iterdir():
                if design_dir.is_dir():
                    batch_for_design[design_dir.name] = batch_dir.name
    drops: set[str] = set()
    for iid in iter_ids:
        _, _, design, epoch, iter_name = _split_iter_id(iid)
        batch = batch_for_design.get(design)
        if not batch:
            continue
        epoch_short = epoch.replace("reference_epoch_", "epoch_")
        iter_n = iter_name.removeprefix("iter_")
        cat = ART_ROOT / batch / design / epoch_short / f"edit{iter_n}_category.txt"
        if not cat.is_file():
            continue
        first = next((ln.strip() for ln in cat.read_text().splitlines() if ln.strip()), "")
        if first == "Invalid":
            drops.add(iid)
    return drops


def _split_iter_id(iter_id: str) -> tuple[str, str, str, str, str]:
    parts = iter_id.split("__")
    run, repo, design, epoch, iter_name = parts[0], parts[1], parts[2], parts[3], parts[4]
    return run, repo, design, epoch, iter_name


def _read_tsv(path: Path) -> pd.DataFrame:
    rows = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            rows.append({
                "start": parts[0], "end": parts[1],
                "slack_synth_ns": float(parts[2]),
                "slack_route_ns": float(parts[3]),
            })
        except ValueError:
            continue
    return pd.DataFrame(rows)


def _read_rpt_fullnames(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        out[(parts[2], parts[3])] = (parts[4], parts[5])
    return out


def load_all() -> pd.DataFrame:
    """Long-form DataFrame across all valid iters."""
    tsvs = sorted(ITER_DIR.glob("*.tsv"))
    all_ids = [t.stem for t in tsvs]
    outdated = _outdated_iter_ids(all_ids)
    invalid = _invalid_iter_ids(all_ids)
    dropped = outdated | invalid
    print(f"dropping {len(outdated)} outdated + {len(invalid)} invalid = {len(dropped)} iters")
    for iid in sorted(outdated):
        print(f"  outdated: {iid}")
    for iid in sorted(invalid):
        print(f"  invalid:  {iid}")

    ports_cache: dict[str, dict[str, str]] = {}
    frames = []
    for tsv in tsvs:
        iter_id = tsv.stem
        if iter_id in dropped:
            continue
        _, _, design, _, _ = _split_iter_id(iter_id)
        rpt = ITER_DIR / f"{iter_id}__1_synth.rpt"
        if not rpt.is_file():
            print(f"skip {iter_id}: missing rpt", file=sys.stderr)
            continue
        df = _read_tsv(tsv)
        if df.empty:
            continue
        full = _read_rpt_fullnames(rpt)
        if design not in ports_cache:
            ports_cache[design] = _load_ports(design)
        ports = ports_cache[design]
        recs = []
        for _, row in df.iterrows():
            key = (row["start"], row["end"])
            sf, ef = full.get(key, key)
            recs.append({
                "iter_id": iter_id,
                "design": design,
                "start": row["start"],
                "end": row["end"],
                "start_full": sf,
                "end_full": ef,
                "end_pin": _endpoint_pin(ef),
                "port_class": _port_class_for(sf, ef, ports),
                "slack_synth_ns": float(row["slack_synth_ns"]),
                "slack_route_ns": float(row["slack_route_ns"]),
                "delta_ns": float(row["slack_route_ns"]) - float(row["slack_synth_ns"]),
            })
        frames.append(pd.DataFrame(recs))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def per_iter_class_median(df: pd.DataFrame) -> pd.DataFrame:
    """Per (iter, class) median Δ."""
    rows = []
    for (iter_id, design, cls), sub in df.groupby(["iter_id", "design", "port_class"]):
        rows.append({
            "iter_id": iter_id, "design": design, "port_class": cls,
            "n": len(sub),
            "median_delta_ns": float(np.median(sub["delta_ns"].to_numpy())),
        })
    return pd.DataFrame(rows)


def design_class_median(per_iter: pd.DataFrame) -> pd.DataFrame:
    """Median over an in-sample design's iter-medians, per class."""
    rows = []
    for (design, cls), sub in per_iter.groupby(["design", "port_class"]):
        rows.append({
            "design": design, "port_class": cls,
            "n_iters": len(sub),
            "median_delta_ns": float(np.median(sub["median_delta_ns"].to_numpy())),
        })
    return pd.DataFrame(rows)


def median_of_medians(per_design: pd.DataFrame) -> dict[str, float]:
    def _mom(cls: str) -> float:
        s = per_design[(per_design["port_class"] == cls) & (per_design["n_iters"] > 0)]
        return float(np.median(s["median_delta_ns"].to_numpy())) if not s.empty else 0.0
    b = {CAT_INPUT: _mom(CAT_INPUT), CAT_OUTPUT: _mom(CAT_OUTPUT),
         CAT_INTERNAL: _mom(CAT_INTERNAL)}
    b[CAT_BOTH] = b[CAT_INPUT] + b[CAT_OUTPUT] - b[CAT_INTERNAL]
    return b


def _iter_rbo(df_iter: pd.DataFrame, betas: dict[str, float],
              top_k: int, p: float) -> float:
    df = df_iter.copy()
    df["beta_ns"] = df["port_class"].map(betas).fillna(0.0)
    df["slack_synth_corr_ns"] = df["slack_synth_ns"] + df["beta_ns"]
    s_keys = list(zip(df.sort_values("slack_synth_corr_ns")["start"],
                      df.sort_values("slack_synth_corr_ns")["end"]))[:top_k]
    r_keys = list(zip(df.sort_values("slack_route_ns")["start"],
                      df.sort_values("slack_route_ns")["end"]))[:top_k]
    return rbo_ext(s_keys, r_keys, p)


def loo(df_all: pd.DataFrame, per_iter: pd.DataFrame, top_k: int) -> pd.DataFrame:
    designs = sorted(df_all["design"].unique())
    zero = {c: 0.0 for c in PORT_CLASSES}
    rows = []
    for held in designs:
        train = per_iter[per_iter["design"] != held]
        per_d = design_class_median(train)
        betas = median_of_medians(per_d)

        held_iters = sorted(df_all[df_all["design"] == held]["iter_id"].unique())
        per_iter_rbo_raw = {p: [] for p in P_VALUES}
        per_iter_rbo_corr = {p: [] for p in P_VALUES}
        for iid in held_iters:
            df_i = df_all[df_all["iter_id"] == iid]
            for p in P_VALUES:
                per_iter_rbo_raw[p].append(_iter_rbo(df_i, zero, top_k, p))
                per_iter_rbo_corr[p].append(_iter_rbo(df_i, betas, top_k, p))

        row = {
            "held_out": held, "n_iters": len(held_iters),
            "beta_input_ns": betas[CAT_INPUT],
            "beta_output_ns": betas[CAT_OUTPUT],
            "beta_internal_ns": betas[CAT_INTERNAL],
            "beta_both_derived_ns": betas[CAT_BOTH],
        }
        for p in P_VALUES:
            tag = f"p{int(round(p * 100)):03d}"
            row[f"rbo_raw_{tag}"] = float(np.mean(per_iter_rbo_raw[p]))
            row[f"rbo_corr_{tag}"] = float(np.mean(per_iter_rbo_corr[p]))
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print("loading 145 iters...")
    df = load_all()
    if df.empty:
        print("no data", file=sys.stderr); sys.exit(1)
    print(f"  iters loaded: {df['iter_id'].nunique()}")
    print(f"  designs:      {sorted(df['design'].unique())}")
    print(f"  total rows:   {len(df)}")
    print("\niters per design:")
    for d, n in df.groupby("design")["iter_id"].nunique().sort_index().items():
        print(f"  {d:<16}  {n}")

    per_iter = per_iter_class_median(df)
    per_iter.to_csv(args.out_dir / "per_iter_class_medians.csv", index=False)

    # Global (all 12) β for reference
    per_d_all = design_class_median(per_iter)
    per_d_all.to_csv(args.out_dir / "per_design_class_medians.csv", index=False)
    betas_global = median_of_medians(per_d_all)
    pd.DataFrame([{"port_class": c, "global_median_ns": v} for c, v in betas_global.items()]
                 ).to_csv(args.out_dir / "global_beta.csv", index=False)
    print("\nglobal β (all 12, median-of-medians, β_both derived):")
    for c, v in betas_global.items():
        print(f"  β[{c}] = {v:+.3f} ns")

    print("\nrunning design-LOO...")
    loo_df = loo(df, per_iter, args.top_k)
    loo_df.to_csv(args.out_dir / "loo_design.csv", index=False)
    print("\nper-design LOO RBO (β trained on the other 11 designs' iters):")
    show_cols = ["held_out", "n_iters",
                 "beta_input_ns", "beta_output_ns", "beta_internal_ns",
                 "rbo_raw_p060", "rbo_corr_p060",
                 "rbo_raw_p090", "rbo_corr_p090",
                 "rbo_raw_p096", "rbo_corr_p096"]
    fmt = loo_df[show_cols].copy()
    for c in fmt.columns:
        if c not in ("held_out", "n_iters"):
            fmt[c] = fmt[c].map(lambda v: f"{v:+.3f}")
    print(fmt.to_string(index=False))

    print("\nLOO mean RBO across 12 held-out designs:")
    for p in P_VALUES:
        tag = f"p{int(round(p * 100)):03d}"
        raw = loo_df[f"rbo_raw_{tag}"].mean()
        corr = loo_df[f"rbo_corr_{tag}"].mean()
        print(f"  p={p}:  raw={raw:.3f}  corr={corr:.3f}  gain={corr - raw:+.3f}")

    print("\nLOO β spread:")
    for c in ("beta_input_ns", "beta_output_ns", "beta_internal_ns"):
        arr = loo_df[c].to_numpy()
        rel = arr.std() / abs(arr.mean()) if abs(arr.mean()) > 1e-6 else float("inf")
        print(f"  {c:<20}  min={arr.min():+.3f}  max={arr.max():+.3f}  std={arr.std():.3f}  std/|mean|={rel:.2f}")

    headline = []
    headline.append("Final β (median-of-medians on 145 iters, design-LOO):")
    for c in PORT_CLASSES:
        headline.append(f"  β[{c}] = {betas_global[c]:+.3f} ns")
    headline.append("")
    headline.append("LOO mean RBO (design-level LOO over 12):")
    for p in P_VALUES:
        tag = f"p{int(round(p * 100)):03d}"
        raw = loo_df[f"rbo_raw_{tag}"].mean()
        corr = loo_df[f"rbo_corr_{tag}"].mean()
        headline.append(f"  p={p}:  raw={raw:.3f}  corr={corr:.3f}  gain={corr - raw:+.3f}")
    text = "\n".join(headline)
    (args.out_dir / "headline.txt").write_text(text + "\n")


if __name__ == "__main__":
    main()
