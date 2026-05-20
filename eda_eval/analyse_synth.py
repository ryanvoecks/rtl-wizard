#!/usr/bin/env python3
"""Logical-path analysis on the post-synthesis netlist (no P&R, no SPEF).

Mirrors `analyse.py`'s timing-path extraction and logical grouping, but
loads ORFS's `1_synth.odb` / `1_synth.sdc` instead of the routed
`6_final.*`. Slack here reflects Liberty cell delays plus OpenSTA's
default zero-RC interconnect estimate, so values are optimistic vs
post-route -- useful for early ranking of register-to-register groups
before placement, not for absolute timing targets.

Reports land in `<phase_dir>/reports/<platform>/<design>/base/`,
suffixed `_synth` to sit alongside the post-route outputs:
    critical_paths_synth.rpt      -- top M worst-slack individual paths
    critical_paths_raw_synth.tsv  -- the full sampled timing pool
    logical_paths_synth.rpt       -- top N register-to-register groups

Usage:
    uv run eda_eval/analyse_synth.py eda_results/<batch>/<benchmark>/<name>/<variant>/
    uv run eda_eval/analyse_synth.py <phase_dir> --top-paths 20 --top-logical 10
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from analyse import (
    EXTRACT_TCL,
    dump_hierarchy,
    liberty_files,
    load_hierarchy,
    load_run_config,
    read_pool_tsv,
    write_critical_paths,
    write_logical_paths,
)
from extract_metrics import find_unique, parse_period_ps

from common.config import RUN_CONFIG_FILENAME


def locate_post_synth(phase_dir: Path, design: str, platform: str) -> tuple[Path, Path]:
    """Return (odb, sdc) for the post-synth stage of this phase dir."""
    base = f"results/{platform}/{design}/*"
    odb = find_unique(phase_dir, f"{base}/1_synth.odb", "1_synth.odb")
    sdc = find_unique(phase_dir, f"{base}/1_synth.sdc", "1_synth.sdc")
    return odb, sdc


def run_openroad_extract(
    odb: Path,
    sdc: Path,
    libs: list[Path],
    pool: int,
    tsv_out: Path,
) -> None:
    """Pipe an STA-only driver script into openroad. No SPEF read: slack
    reflects Liberty cell delays plus the tool's default zero-RC
    interconnect estimate."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines.append(f"read_db {odb}")
    lines.append(f"read_sdc {sdc}")
    lines.append(f"source {EXTRACT_TCL}")
    script = "\n".join(lines)

    env = {
        **os.environ,
        "ANALYSE_OUT_TSV": str(tsv_out),
        "ANALYSE_POOL": str(pool),
    }
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input=script,
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"openroad extraction failed (rc={proc.returncode}). See output above."
        )
    if not tsv_out.is_file():
        raise RuntimeError(f"openroad did not write {tsv_out}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "phase_dir",
        type=Path,
        help="Variant phase dir (contains inputs/, results/, logs/, reports/).",
    )
    ap.add_argument(
        "--top-paths",
        type=int,
        default=50,
        metavar="M",
        help="How many worst-slack individual paths to report (default 50).",
    )
    ap.add_argument(
        "--top-logical",
        type=int,
        default=50,
        metavar="N",
        help="How many logical register-to-register groups to report (default 50).",
    )
    ap.add_argument(
        "--pool",
        type=int,
        default=1000,
        metavar="P",
        help="Path pool size for find_timing_paths (default 1000). "
        "Larger pools give better logical-group statistics.",
    )
    args = ap.parse_args(argv)

    phase_dir = args.phase_dir.resolve()
    if not phase_dir.is_dir():
        ap.error(f"not a directory: {phase_dir}")

    run_cfg = load_run_config(phase_dir)
    target = run_cfg["synth_target"]
    top_module = target["design"]["top_module"]
    platform = target["cfg"]["platform"]
    rtl_files = [Path(p) for p in target["design"]["rtl_files"]]
    missing = [p for p in rtl_files if not p.is_file()]
    if missing:
        ap.error(f"missing RTL files referenced from {RUN_CONFIG_FILENAME}: {missing}")

    odb, sdc = locate_post_synth(phase_dir, top_module, platform)
    libs = liberty_files(platform)

    reports_dir = phase_dir / "reports" / platform / top_module / "base"
    reports_dir.mkdir(parents=True, exist_ok=True)
    raw_tsv = reports_dir / "critical_paths_raw_synth.tsv"

    print(f"==> Phase dir:   {phase_dir}")
    print(f"==> Platform:    {platform}")
    print(f"==> Design:      {top_module}")
    print(f"==> Synth DB:    {odb}")
    print(f"==> SDC:         {sdc}")
    print(f"==> Reports:     {reports_dir}")
    print(
        f"==> Pool / paths / logical:"
        f" {args.pool} / {args.top_paths} / {args.top_logical}"
    )

    staging = raw_tsv.with_suffix(".tsv.partial")
    run_openroad_extract(odb, sdc, libs, args.pool, staging)
    staging.replace(raw_tsv)

    records = read_pool_tsv(raw_tsv)
    if not records:
        sys.stderr.write(
            "warning: OpenSTA returned zero paths -- is the design purely "
            "combinational with no constrained outputs?\n"
        )
        return 1

    hier_json = reports_dir / "hierarchy.json"
    dump_hierarchy(rtl_files, top_module, hier_json)
    hierarchy = load_hierarchy(hier_json)
    if top_module not in hierarchy:
        ap.error(
            f"top module {top_module!r} not found in yosys-dumped hierarchy: "
            f"{sorted(hierarchy)}"
        )

    crit_path = reports_dir / "critical_paths_synth.rpt"
    n_crit = write_critical_paths(records, crit_path, args.top_paths)

    logical_path = reports_dir / "logical_paths_synth.rpt"
    n_log = write_logical_paths(
        records,
        logical_path,
        args.top_logical,
        top_module,
        hierarchy,
        pool_size=len(records),
        clock_period_ns=parse_period_ps(sdc) / 1000,
    )

    print(f"==> Wrote {n_crit} worst paths to {crit_path}")
    print(f"==> Wrote {n_log} logical groups to {logical_path}")
    print(f"==> Raw timing pool ({len(records)} paths): {raw_tsv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
