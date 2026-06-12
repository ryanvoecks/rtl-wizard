#!/usr/bin/env python3
"""Extract worst-slack timing-path pools at post-synth and post-route.

For each baseline run in `common.targets.all_targets`, finds the cached
ORFS results, runs openroad twice (1_synth and 6_final), and writes the
TSV pool of paths produced by `eda_eval/tcl/extract_critical_paths.tcl`.
The pools land at:

    analysis/data/critical_paths/<design>__<stage>.tsv

Skips any (design, stage) pair whose TSV already exists. Each openroad
run takes ~30-90 s, so the full sweep is single-threaded and slow; run
once and cache.

Usage:
    uv run python analysis/extract_topk_paths.py [--pool 500] [--force]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.analyse import liberty_files, locate_results  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402
from eda_eval.scope import run_openroad_extract  # noqa: E402

OUT_DIR = REPO / "analysis" / "data" / "critical_paths"
STAGES = ("1_synth", "6_final")  # post-synth zero-RC + post-route w/ SPEF


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--pool",
        type=int,
        default=500,
        help="-group_path_count passed to find_timing_paths",
    )
    ap.add_argument(
        "--force", action="store_true", help="re-extract even if TSV already exists"
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for target in all_targets:
        design = target.design
        run = RunConfig(
            synth_target=target,
            output_dir=EDA_RUNS / "_dummy_baseline",
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=1,
        )
        cp = cache_path(run)
        if not list(cp.glob("logs/nangate45/*/base/6_report.json")):
            print(f"skip {design.name}: baseline cache missing", file=sys.stderr)
            continue
        libs = liberty_files("nangate45")
        for stage in STAGES:
            out_tsv = OUT_DIR / f"{design.name}__{stage}.tsv"
            if out_tsv.is_file() and not args.force:
                print(f"have {out_tsv.relative_to(REPO)}")
                continue
            try:
                odb, sdc, spef = locate_results(
                    cp, design.top_module, "nangate45", stage
                )
            except Exception as exc:
                print(f"skip {design.name} {stage}: {exc}", file=sys.stderr)
                continue
            # post-synth has no parasitics yet, so spef stays None there.
            spef_to_use = spef if stage == "6_final" else None
            t0 = time.time()
            try:
                run_openroad_extract(odb, sdc, spef_to_use, libs, args.pool, out_tsv)
            except Exception as exc:
                print(f"FAIL {design.name} {stage}: {exc}", file=sys.stderr)
                continue
            print(f"wrote {out_tsv.relative_to(REPO)} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
