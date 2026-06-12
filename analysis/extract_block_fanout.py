#!/usr/bin/env python3
"""Extract average net-fanout of the top-N worst-slack logical blocks.

For each baseline (post-synth) target, runs `find_block_fanouts` from
`eda_eval/tcl/worst_logical_paths.tcl`: same iterative block-discovery
loop as `find_worst_logical_blocks`, but each row also carries the
output-net fanout statistics for the union of cells matched by the
start + end block selectors.

Caches one TSV per design at:
    analysis/data/block_fanout/<design>__1_synth.tsv

Columns (per row, one row per block rank):
    rank  worst_slack_ns  start  end  n_cells  n_output_nets  mean_fanout  max_fanout

Each openroad invocation takes ~30-90 s; single-threaded and cached
exactly like `extract_topk_paths.py`.

Usage:
    uv run python analysis/extract_block_fanout.py [--top-n 25] [--force]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, ORFS_FLOW, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.analyse import _tcl_braces, liberty_files, locate_results  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

OUT_DIR = REPO / "analysis" / "data" / "block_fanout"
WLP_TCL = REPO / "eda_eval" / "tcl" / "worst_logical_paths.tcl"
STAGE = "1_synth"
PLATFORM = "nangate45"


def run_openroad_block_fanout(
    odb: Path,
    sdc: Path,
    set_rc: Path,
    libs: list[Path],
    top_n: int,
    top_module: str,
    out_path: Path,
) -> None:
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {WLP_TCL}"]
    # Post-synth stage carries no parasitics, so spef is empty.
    lines.append(
        "find_block_fanouts "
        f"{_tcl_braces(str(out_path))} {top_n} {_tcl_braces(STAGE)} "
        f"{{}} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)}"
    )
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input="\n".join(lines),
        env={**os.environ},
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad block-fanout extraction failed")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-n", type=int, default=25)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--designs",
        nargs="*",
        default=None,
        help="restrict to these design names (default: every baseline target)",
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    set_rc = ORFS_FLOW / "platforms" / PLATFORM / "setRC.tcl"
    libs = liberty_files(PLATFORM)
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
        if not list(cp.glob(f"logs/{PLATFORM}/*/base/6_report.json")):
            print(f"skip {design.name}: baseline cache missing", file=sys.stderr)
            continue
        out_tsv = OUT_DIR / f"{design.name}__{STAGE}.tsv"
        if out_tsv.is_file() and not args.force:
            print(f"have {out_tsv.relative_to(REPO)}")
            continue
        try:
            odb, sdc, _spef = locate_results(cp, design.top_module, PLATFORM, STAGE)
        except Exception as exc:
            print(f"skip {design.name}: {exc}", file=sys.stderr)
            continue
        t0 = time.time()
        try:
            run_openroad_block_fanout(
                odb, sdc, set_rc, libs, args.top_n, design.top_module, out_tsv
            )
        except Exception as exc:
            print(f"FAIL {design.name}: {exc}", file=sys.stderr)
            continue
        print(f"wrote {out_tsv.relative_to(REPO)} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
