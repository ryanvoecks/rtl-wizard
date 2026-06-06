#!/usr/bin/env python3
"""Summarise rewriter-sweep achievable-fmax variance per design.

Walks `<batch>/<benchmark>/<name>/<variant>_rewrite_seed_*/` phase dirs,
pulls signed worst slack at both post-synth and post-route via
`eda_eval.extract_metrics.extract`, converts each WS to achievable fmax
via `fmax = 1000 / (T_target - WS_signed)`, and prints a markdown table
of mean and CV for both stages.

Usage:
    uv run python analysis/rewriter_fmax_variance.py <batch>
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CSV_PATH = REPO / "analysis" / "data" / "variance.csv"

from eda_eval.extract_metrics import (  # noqa: E402
    extract,
    parse_period_ps,
    resolve_batch,
    resolve_design_name,
)


def collect(
    batch_dir: Path,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Return per-design lists of (synth_fmax_mhz, route_fmax_mhz) across
    every `*_rewrite_seed_*` variant found under `batch_dir`."""
    synth: dict[str, list[float]] = {}
    route: dict[str, list[float]] = {}
    for variant in sorted(batch_dir.glob("*/*/*_rewrite_seed_*")):
        sdc = variant / "inputs" / "constraint.sdc"
        if not sdc.is_file():
            continue
        try:
            design = resolve_design_name(variant)
            period_ns = parse_period_ps(sdc) / 1000.0
            metrics = extract(variant, design)
        except Exception as exc:
            sys.stderr.write(f"skip {variant.relative_to(batch_dir)}: {exc}\n")
            continue
        key = f"{variant.parts[-3]}/{variant.parts[-2]}"
        synth.setdefault(key, []).append(1000.0 / (period_ns - metrics["synth_ws_ns"]))
        route.setdefault(key, []).append(1000.0 / (period_ns - metrics["route_ws_ns"]))
    return synth, route


def stats(vals: list[float]) -> tuple[float, float]:
    """Arithmetic mean and sample-CV (Bessel-corrected std / mean) of `vals`."""
    mean = statistics.fmean(vals)
    cv = statistics.stdev(vals) / mean if mean and len(vals) > 1 else float("nan")
    return mean, cv


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "batch",
        help="Batch identifier -- name under <repo>/eda_results/ or a direct path.",
    )
    args = ap.parse_args()
    batch_dir = resolve_batch(args.batch)

    synth, route = collect(batch_dir)
    designs = sorted(set(synth) | set(route))
    if not designs:
        sys.exit(f"no *_rewrite_seed_* variants found under {batch_dir}")

    print(f"# Rewriter-sweep fmax variance: {batch_dir.name}\n")
    print("| design | synth fmax (MHz) | synth CV | PnR fmax (MHz) | PnR CV | n |")
    print("|---|---:|---:|---:|---:|---:|")
    rows: list[dict[str, object]] = []
    for d in designs:
        s_vals = synth.get(d, [])
        r_vals = route.get(d, [])
        s_mean, s_cv = stats(s_vals) if s_vals else (float("nan"), float("nan"))
        r_mean, r_cv = stats(r_vals) if r_vals else (float("nan"), float("nan"))
        n = max(len(s_vals), len(r_vals))
        print(f"| {d} | {s_mean:.2f} | {s_cv:.4f} | {r_mean:.2f} | {r_cv:.4f} | {n} |")
        rows.append(
            {
                "design": d,
                "synth_fmax_mhz_mean": s_mean,
                "synth_fmax_mhz_cv": s_cv,
                "pnr_fmax_mhz_mean": r_mean,
                "pnr_fmax_mhz_cv": r_cv,
                "n": n,
            }
        )

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {CSV_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
