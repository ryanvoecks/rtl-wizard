#!/usr/bin/env python3
"""Re-calibrate the predictor on the synth top-1000 functional dataset.

Reuses the protocol from correction_calibration.py (median-of-medians beta
with beta_internal free and beta_both derived) but trains on the broader pool produced
by predictor/extract_synth_top1000_func.py: 1000 synth top-functional
logical blocks per design (skip_async applied at extraction time) with their
PnR slack looked up via path_slack_lookup.tcl.

For each design, two pools are used:
  - TRAINING pool (1000 paths from the new TSV):
      per-design per-class median delta, median-of-medians for beta.
  - EVALUATION:
      a. internal -- corrected-synth top-K (from the 1000) vs route top-K
         (same 1000 sorted by route slack). Same shape as the previous
         variant (b) calibration on the union pool, but with a much larger
         pool so a "synth top-100" prediction need not be drawn from a tiny
         100-path candidate set.
      b. external -- corrected-synth top-K (from the 1000) vs the TRUE
         route top-100 (from analysis/data/logical_blocks/<design>__6_final.rpt).
         At inference the actual PnR ranking is what we'd be judged against,
         not the route ranking restricted to our synth-1000 sample. This is
         the more honest metric and is the headline.

Inputs:
  - predictor/data/extraction/synth_top1000_func/<design>.tsv
      cols: start, end, slack_synth_ns, slack_route_ns
  - predictor/data/extraction/logical_blocks_top1000_func/<design>__1_synth.rpt
      cols: rank, slack, start, end, start_full, end_full  (used for full names)
  - analysis/data/logical_blocks/<design>__6_final.rpt
      route top-100 reference for external RBO

Outputs (under predictor/data/calibration_top1000/):
  - per_design_class_medians.csv
  - global_beta.csv
  - loo_median_of_medians.csv         (internal RBO at p=0.9)
  - per_design_rbo_summary_external.csv   (external RBO at p={0.6,0.9,0.96})
  - per_design_rbo_summary_internal.csv   (internal RBO at p={0.6,0.9,0.96})
  - headline.txt

Usage:
    uv run --with pandas --with numpy --with statsmodels --with scipy \\
        python predictor/calibrate_top1000.py
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

# Reuse helpers from the main calibration module so the protocol stays in one
# place; only the data sources and the external-RBO comparison are new.
from predictor.correction_calibration import (  # noqa: E402
    CAT_BOTH,
    CAT_INPUT,
    CAT_INTERNAL,
    CAT_OUTPUT,
    PORT_CLASSES,
    _endpoint_pin,
    _load_ports,
    _port_class_for,
    rbo_ext,
)

NEW_TSV_DIR = REPO / "predictor" / "data" / "extraction" / "synth_top1000_func"
NEW_RPT_DIR = REPO / "predictor" / "data" / "extraction" / "logical_blocks_top1000_func"
OLD_RPT_DIR = REPO / "analysis" / "data" / "logical_blocks"
OUT_DIR = REPO / "predictor" / "data" / "calibration_top1000"

TOP_K = 100
P_VALUES = (0.6, 0.9, 0.96)


def _read_tsv(path: Path) -> pd.DataFrame:
    rows = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            rows.append(
                {
                    "start": parts[0],
                    "end": parts[1],
                    "slack_synth_ns": float(parts[2]),
                    "slack_route_ns": float(parts[3]),
                }
            )
        except ValueError:
            continue
    return pd.DataFrame(rows)


def _read_rpt_fullnames(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """Map (start_collapsed, end_collapsed) -> (start_full, end_full)."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        out[(parts[2], parts[3])] = (parts[4], parts[5])
    return out


def _read_route_topk_pairs(rpt: Path, k: int) -> list[tuple[str, str]]:
    """Top-K collapsed (start, end) pairs from a logical_blocks rpt."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in rpt.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        key = (parts[2], parts[3])
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
        if len(pairs) >= k:
            break
    return pairs


def load_all(designs: list[str]) -> pd.DataFrame:
    """Long-form annotated DataFrame across all designs.

    Annotations:
      - port_class via load_ports + start_full/end_full
      - delta_ns = slack_route_ns - slack_synth_ns
    """
    frames = []
    for design in designs:
        tsv = NEW_TSV_DIR / f"{design}.tsv"
        rpt = NEW_RPT_DIR / f"{design}__1_synth.rpt"
        if not tsv.is_file() or not rpt.is_file():
            print(f"skip {design}: missing input file(s)", file=sys.stderr)
            continue
        df = _read_tsv(tsv)
        if df.empty:
            continue
        ports = _load_ports(design)
        fullmap = _read_rpt_fullnames(rpt)
        recs = []
        for _, row in df.iterrows():
            start = str(row["start"])
            end = str(row["end"])
            key = (start, end)
            full = fullmap.get(key, key)
            sf, ef = full
            slack_synth = float(row["slack_synth_ns"])  # type: ignore[arg-type]
            slack_route = float(row["slack_route_ns"])  # type: ignore[arg-type]
            recs.append(
                {
                    "design": design,
                    "start": start,
                    "end": end,
                    "start_full": sf,
                    "end_full": ef,
                    "end_pin": _endpoint_pin(ef),
                    "port_class": _port_class_for(sf, ef, ports),
                    "slack_synth_ns": slack_synth,
                    "slack_route_ns": slack_route,
                    "delta_ns": slack_route - slack_synth,
                }
            )
        frames.append(pd.DataFrame(recs))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def per_design_class_medians(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for design in sorted(df["design"].unique()):
        for cls in PORT_CLASSES:
            sub = cast(
                pd.DataFrame, df[(df["design"] == design) & (df["port_class"] == cls)]
            )
            arr = sub["delta_ns"].to_numpy()
            med = float(np.median(arr)) if arr.size else float("nan")
            rows.append(
                {
                    "design": design,
                    "port_class": cls,
                    "n": int(arr.size),
                    "median_delta_ns": med,
                }
            )
    return pd.DataFrame(rows)


def median_of_medians(per_design: pd.DataFrame) -> dict[str, float]:
    """Returns the global per-class beta with beta_both derived from additivity."""

    def _mom(cls: str) -> float:
        sub = cast(
            pd.DataFrame,
            per_design[(per_design["port_class"] == cls) & (per_design["n"] > 0)],
        )
        return (
            float(np.median(sub["median_delta_ns"].to_numpy()))
            if not sub.empty
            else 0.0
        )

    betas = {
        CAT_INPUT: _mom(CAT_INPUT),
        CAT_OUTPUT: _mom(CAT_OUTPUT),
        CAT_INTERNAL: _mom(CAT_INTERNAL),
    }
    betas[CAT_BOTH] = betas[CAT_INPUT] + betas[CAT_OUTPUT] - betas[CAT_INTERNAL]
    return betas


def _design_internal_rbo(
    df_d: pd.DataFrame, betas: dict[str, float], top_k: int, p: float
) -> float:
    """RBO of corrected synth top-K vs route top-K, both over the 1000 pool."""
    df_d = df_d.copy()
    df_d["beta_ns"] = df_d["port_class"].map(betas).fillna(0.0)  # type: ignore[arg-type]
    df_d["slack_synth_corr_ns"] = df_d["slack_synth_ns"] + df_d["beta_ns"]
    synth_sorted = df_d.sort_values("slack_synth_corr_ns")
    route_sorted = df_d.sort_values("slack_route_ns")
    s_keys = list(zip(synth_sorted["start"], synth_sorted["end"]))[:top_k]
    r_keys = list(zip(route_sorted["start"], route_sorted["end"]))[:top_k]
    return rbo_ext(s_keys, r_keys, p)


def _design_external_rbo(
    df_d: pd.DataFrame,
    betas: dict[str, float],
    top_k: int,
    p: float,
    route_topk_pairs: list[tuple[str, str]],
) -> float:
    """RBO of corrected synth top-K (from the 1000) vs the TRUE route top-K
    (from the existing analysis/data/logical_blocks rpt). Lists are
    non-conjoint; RBO_EXT handles that."""
    df_d = df_d.copy()
    df_d["beta_ns"] = df_d["port_class"].map(betas).fillna(0.0)  # type: ignore[arg-type]
    df_d["slack_synth_corr_ns"] = df_d["slack_synth_ns"] + df_d["beta_ns"]
    synth_sorted = df_d.sort_values("slack_synth_corr_ns")
    s_keys = list(zip(synth_sorted["start"], synth_sorted["end"]))[:top_k]
    return rbo_ext(s_keys, route_topk_pairs[:top_k], p)


def loo(
    df_all: pd.DataFrame,
    per_design: pd.DataFrame,
    route_topk: dict[str, list[tuple[str, str]]],
    top_k: int,
    p: float,
) -> pd.DataFrame:
    rows = []
    designs = sorted(df_all["design"].unique())
    zero = {c: 0.0 for c in PORT_CLASSES}
    for held in designs:
        train = cast(pd.DataFrame, per_design[per_design["design"] != held])
        train = cast(pd.DataFrame, train[train["n"] > 0])

        def _mom(cls: str, train: pd.DataFrame = train) -> float:
            sub = cast(pd.DataFrame, train[train["port_class"] == cls])
            return (
                float(np.median(sub["median_delta_ns"].to_numpy()))
                if not sub.empty
                else 0.0
            )

        beta_in = _mom(CAT_INPUT)
        beta_out = _mom(CAT_OUTPUT)
        beta_int = _mom(CAT_INTERNAL)
        betas = {
            CAT_INPUT: beta_in,
            CAT_OUTPUT: beta_out,
            CAT_INTERNAL: beta_int,
            CAT_BOTH: beta_in + beta_out - beta_int,
        }

        df_h = cast(pd.DataFrame, df_all[df_all["design"] == held])
        rt = route_topk.get(held, [])
        rbo_raw_int = _design_internal_rbo(df_h, zero, top_k, p)
        rbo_corr_int = _design_internal_rbo(df_h, betas, top_k, p)
        rbo_raw_ext = _design_external_rbo(df_h, zero, top_k, p, rt)
        rbo_corr_ext = _design_external_rbo(df_h, betas, top_k, p, rt)
        rows.append(
            {
                "held_out": held,
                "beta_input_ns": beta_in,
                "beta_output_ns": beta_out,
                "beta_internal_ns": beta_int,
                "beta_both_derived_ns": beta_in + beta_out - beta_int,
                "rbo_internal_raw": rbo_raw_int,
                "rbo_internal_corr": rbo_corr_int,
                "rbo_external_raw": rbo_raw_ext,
                "rbo_external_corr": rbo_corr_ext,
                "gain_internal": rbo_corr_int - rbo_raw_int,
                "gain_external": rbo_corr_ext - rbo_raw_ext,
            }
        )
    return pd.DataFrame(rows)


def per_design_summary(
    df_all: pd.DataFrame,
    betas: dict[str, float],
    route_topk: dict[str, list[tuple[str, str]]],
    top_k: int,
) -> pd.DataFrame:
    """Per-design 3-stage x 3-p table; "post_async" here equals "raw" because
    the synth pool is functional-only by construction. We still list it for
    parity with the original summary."""
    rows = []
    zero = {c: 0.0 for c in PORT_CLASSES}
    for design in sorted(df_all["design"].unique()):
        df_d = cast(pd.DataFrame, df_all[df_all["design"] == design])
        rt = route_topk.get(design, [])
        row: dict[str, object] = {"design": design, "n_pool": len(df_d)}
        for p in P_VALUES:
            tag = f"p{int(round(p * 100)):03d}"
            row[f"rbo_int_raw_{tag}"] = _design_internal_rbo(df_d, zero, top_k, p)
            row[f"rbo_int_corr_{tag}"] = _design_internal_rbo(df_d, betas, top_k, p)
            row[f"rbo_ext_raw_{tag}"] = _design_external_rbo(df_d, zero, top_k, p, rt)
            row[f"rbo_ext_corr_{tag}"] = _design_external_rbo(df_d, betas, top_k, p, rt)
        rows.append(row)
    df = pd.DataFrame(rows)
    mean_row: dict[str, object] = {
        "design": "MEAN",
        "n_pool": int(cast(float, df["n_pool"].mean())),
    }
    for c in df.columns:
        if c not in ("design", "n_pool"):
            mean_row[c] = float(cast(float, df[c].mean()))
    df = pd.concat([df, pd.DataFrame([mean_row])], ignore_index=True)
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--p", type=float, default=0.9)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    designs = sorted(p.stem for p in NEW_TSV_DIR.glob("*.tsv"))
    print(f"designs with new dataset: {len(designs)}  -> {designs}")
    df_all = load_all(designs)
    print(f"total rows in pooled dataset: {len(df_all)}")
    if df_all.empty:
        print(
            "no data; run predictor/extract_synth_top1000_func.py first",
            file=sys.stderr,
        )
        sys.exit(1)

    pool_sizes = cast(pd.Series, df_all.groupby("design").size()).sort_values()
    print("pool sizes per design:")
    for d, n in pool_sizes.items():
        print(f"  {d:<16}  {n}")

    print("\nper-design x port_class counts (functional only by construction):")
    pivot = cast(
        pd.DataFrame,
        df_all.groupby(["design", "port_class"]).size().unstack(fill_value=0),
    ).reindex(columns=PORT_CLASSES, fill_value=0)
    print(pivot.to_string())

    per_design = per_design_class_medians(df_all)
    per_design.to_csv(args.out_dir / "per_design_class_medians.csv", index=False)

    print("\nper-design median delta (ns):")
    show = per_design.pivot(
        index="design", columns="port_class", values="median_delta_ns"
    )
    show = show.reindex(columns=PORT_CLASSES)
    print(show.to_string())

    betas_all = median_of_medians(per_design)
    pd.DataFrame(
        [{"port_class": c, "global_median_ns": v} for c, v in betas_all.items()]
    ).to_csv(args.out_dir / "global_beta.csv", index=False)
    print("\nglobal beta (median-of-medians, beta_both derived):")
    for c, v in betas_all.items():
        print(f"  beta[{c}] = {v:+.3f} ns")

    # Load route top-100 reference per design for external RBO
    route_topk = {
        d: _read_route_topk_pairs(OLD_RPT_DIR / f"{d}__6_final.rpt", args.top_k)
        for d in designs
    }

    # LOO at p=0.9 (the headline)
    loo_df = loo(df_all, per_design, route_topk, args.top_k, args.p)
    loo_df.to_csv(args.out_dir / "loo_median_of_medians.csv", index=False)
    print(f"\nLOO at p={args.p}:")
    print(loo_df.to_string(index=False, float_format=lambda v: f"{v:+.3f}"))
    print(
        f"\n  mean LOO RBO internal raw : {loo_df['rbo_internal_raw'].mean():.3f}\n"
        f"  mean LOO RBO internal corr: {loo_df['rbo_internal_corr'].mean():.3f}\n"
        f"  mean LOO RBO external raw : {loo_df['rbo_external_raw'].mean():.3f}\n"
        f"  mean LOO RBO external corr: {loo_df['rbo_external_corr'].mean():.3f}\n"
        f"  gain external             : {loo_df['gain_external'].mean():+.3f}"
    )

    # Per-design summary at multiple p values (uses all-12 beta, not LOO -- LOO
    # adjustment is tiny per the spread check below)
    summary = per_design_summary(df_all, betas_all, route_topk, args.top_k)
    summary.to_csv(args.out_dir / "per_design_rbo_summary.csv", index=False)
    print("\nper-design RBO summary (all-12 beta, external = vs TRUE route top-100):")
    sfmt = summary.copy()
    for c in sfmt.columns:
        if c not in ("design", "n_pool"):
            sfmt[c] = sfmt[c].map(lambda v: f"{v:.3f}")
    print(sfmt.to_string(index=False))

    # Headline text
    lines = []
    lines.append(
        "Final beta (median-of-medians on synth top-1000 functional, all 12 designs):"
    )
    for c in PORT_CLASSES:
        lines.append(f"  beta[{c}] = {betas_all[c]:+.3f} ns")
    lines.append("")
    lines.append(f"LOO mean RBO at p={args.p}:")
    lines.append(f"  internal raw : {loo_df['rbo_internal_raw'].mean():.3f}")
    lines.append(f"  internal corr: {loo_df['rbo_internal_corr'].mean():.3f}")
    lines.append(f"  external raw : {loo_df['rbo_external_raw'].mean():.3f}")
    lines.append(f"  external corr: {loo_df['rbo_external_corr'].mean():.3f}")
    lines.append("")
    lines.append(
        "External = corrected-synth top-100 (drawn from the 1000-path "
        "functional pool) vs the design's actual route top-100 logical_blocks."
    )
    text = "\n".join(lines)
    print("\n" + "=" * 78)
    print(text)
    (args.out_dir / "headline.txt").write_text(text + "\n")


if __name__ == "__main__":
    main()
