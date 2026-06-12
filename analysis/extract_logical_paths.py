#!/usr/bin/env python3
"""Extract worst-logical-{paths,blocks} at post-synth + post-route per baseline.

Wraps `eda_eval.analyse.analyse` for each `common.targets.all_targets`,
running it twice (stage='1_synth' and stage='6_final') so the path-level
analyses (Q1: top-K stability, slack histograms; Q2: near-critical
density features) can read the same reports.

The `--kind` flag selects the collapse level:
  paths  -- collapse only bus indices [N] -> [I],[J],...  (default)
  blocks -- collapse every numeric run, so replicated submodules
            (`dct_block_0..7`) all map to one entry.

Each run writes:
    <baseline cache>/reports/nangate45/<top>/base/worst_logical_<kind>.<stage>.rpt
and the corresponding analysis copy at:
    analysis/data/logical_<kind>/<design>__<stage>.rpt

Already-present reports are skipped (idempotent). Designs can be filtered
with `--designs` to extract just a subset.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.analyse import KIND_PATHS, KINDS, analyse  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

OUT_LOG_DIR_TMPL = REPO / "analysis" / "data" / "logical_{kind}"
TOP_N = 500
STAGES = ("1_synth", "6_final")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--kind",
        choices=list(KINDS),
        default=KIND_PATHS,
        help="dedup level: 'paths' (bus indices only) or 'blocks' (every number)",
    )
    ap.add_argument(
        "--designs",
        nargs="*",
        default=None,
        help="restrict to these design names (default: every baseline target)",
    )
    args = ap.parse_args()
    kind: str = args.kind
    out_dir = Path(str(OUT_LOG_DIR_TMPL).format(kind=kind))
    out_dir.mkdir(parents=True, exist_ok=True)

    only = set(args.designs) if args.designs else None
    for target in all_targets:
        design = target.design
        if only is not None and design.name not in only:
            continue
        run = RunConfig(
            synth_target=target,
            output_dir=EDA_RUNS / "_dummy_baseline",
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=1,
        )
        cp = cache_path(run)
        if not list(cp.glob("logs/nangate45/*/base/6_report.json")):
            print(f"skip {design.name}: baseline missing", file=sys.stderr)
            continue
        # analyse.py reads RunConfig from output_dir; the baseline cache
        # already has run_config.json so just override output_dir to point
        # there.
        run = replace(RunConfig.load(cp / RunConfig.FILENAME), output_dir=cp)
        for stage in STAGES:
            rpt = (
                cp
                / "reports"
                / "nangate45"
                / design.top_module
                / "base"
                / f"worst_logical_{kind}.{stage}.rpt"
            )
            dest = out_dir / f"{design.name}__{stage}.rpt"
            if dest.is_file() and not args.force:
                print(f"have {dest.relative_to(REPO)}")
                continue
            t0 = time.time()
            try:
                # include_full=True -- downstream (extract_path_slack_union.py,
                # the predictor analysis scripts) needs start_full / end_full.
                analyse(
                    run, top_n=args.top_n, stage=stage, kind=kind, include_full=True
                )
            except Exception as exc:
                print(f"FAIL {design.name} {stage}: {exc}", file=sys.stderr)
                continue
            if not rpt.is_file():
                print(
                    f"FAIL {design.name} {stage}: report not produced", file=sys.stderr
                )
                continue
            dest.write_text(rpt.read_text())
            print(f"wrote {dest.relative_to(REPO)} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
