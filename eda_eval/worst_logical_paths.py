#!/usr/bin/env python3
"""Dump the top-N worst logical paths (n-bit reg/IO bus to bus) for a phase dir.

Thin wrapper around tcl/worst_logical_paths.tcl: it loads the routed (or
post-synth) database into openroad and lets the TCL do the dedup, so the report
has exactly one entry per (source-bus, dest-bus) pair, ranked worst-slack first.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from common.config import EDA_EVAL, PNR_FLOW_TARGETS, RunConfig
from eda_eval.analyse import liberty_files, locate_results

# Default config
TOP_N = 50

# tcl paths
WLP_TCL = EDA_EVAL / "tcl" / "worst_logical_paths.tcl"


def _tcl_braces(value: str) -> str:
    """Wrap a string as a literal Tcl word. Filesystem paths and identifiers
    here never contain braces, so brace-quoting is safe and unambiguous."""
    if "{" in value or "}" in value:
        raise ValueError(f"cannot safely brace-quote for Tcl: {value!r}")
    return "{" + value + "}"


def run_openroad(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    libs: list[Path],
    top_n: int,
    stage_label: str,
    top_module: str,
    out_path: Path,
) -> None:
    """Drive openroad to dump the worst logical-path report to out_path."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}"]
    if spef is not None:
        lines.append(f"read_spef {spef}")
    lines.append(f"source {WLP_TCL}")
    # The TCL exposes find_worst_logical_paths as a proc; pass its inputs as
    # arguments rather than smuggling them through the environment.
    lines.append(
        "find_worst_logical_paths "
        f"{_tcl_braces(str(out_path))} {top_n} "
        f"{_tcl_braces(stage_label)} {_tcl_braces(top_module)}"
    )
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input="\n".join(lines),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad logical-path extraction failed")


def worst_logical_paths(run: RunConfig, *, top_n: int = TOP_N) -> str:
    """Run STA on the phase dir and write the worst-logical-paths report.
    Determines stage (synth/route) based on RunConfig."""
    design = run.synth_target.design
    top_module = design.top_module
    platform = run.synth_target.cfg.platform
    phase_dir = run.output_dir

    if set(run.flow_targets) & set(PNR_FLOW_TARGETS):
        stage, stage_label = "6_final", "post-route"
    else:
        stage, stage_label = "1_synth", "post-synth"

    odb, sdc, spef = locate_results(phase_dir, top_module, platform, stage)
    libs = liberty_files(platform)
    reports_dir = phase_dir / "reports" / platform / top_module / "base"
    reports_dir.mkdir(parents=True, exist_ok=True)

    out_path = reports_dir / "worst_logical_paths.rpt"
    staging = out_path.with_suffix(".rpt.partial")
    run_openroad(odb, sdc, spef, libs, top_n, stage_label, top_module, staging)
    staging.replace(out_path)
    return out_path.read_text()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase_dir", type=Path)
    parser.add_argument("--top-n", type=int, default=TOP_N, metavar="N")
    args = parser.parse_args()

    phase_dir = args.phase_dir.resolve()
    run = replace(RunConfig.load(phase_dir / RunConfig.FILENAME), output_dir=phase_dir)
    print(worst_logical_paths(run, top_n=args.top_n))


if __name__ == "__main__":
    main()
