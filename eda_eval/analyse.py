#!/usr/bin/env python3
"""Dump the top-N worst logical paths (n-bit reg/IO bus to bus) for a phase dir.

Thin wrapper around tcl/worst_logical_paths.tcl: it loads a stage database into
openroad and lets the TCL pick the right interconnect parasitics for that stage
(the .odb persists none) and do the dedup, so the report has one entry per
(source-bus, dest-bus) pair, ranked worst-slack first.
"""

from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from common.config import EDA_EVAL, ORFS_FLOW, RunConfig
from eda_eval.extract_metrics import find_unique

# Default config
TOP_N = 25

# ORFS flow target -> the db stage it produces, in dependency order.
STAGE_DBS = {
    "synth": "1_synth",
    "floorplan": "2_floorplan",
    "place": "3_place",
    "cts": "4_cts",
    "route": "5_route",
    "do-finish": "6_final",
}

# tcl paths
WLP_TCL = EDA_EVAL / "tcl" / "worst_logical_paths.tcl"


# Helpers


def locate_results(
    phase_dir: Path,
    design: str,
    platform: str,
    stage: str,
) -> tuple[Path, Path, Path | None]:
    """Return (odb, sdc, spef_or_none) for `stage` (e.g. `1_synth`, `6_final`)."""
    base = f"results/{platform}/{design}/*"
    odb = find_unique(phase_dir, f"{base}/{stage}.odb", f"{stage}.odb")
    sdc = find_unique(phase_dir, f"{base}/{stage}.sdc", f"{stage}.sdc")
    spef = next(iter(phase_dir.glob(f"{base}/{stage}.spef")), None)
    return odb, sdc, spef


def liberty_files(platform: str) -> list[Path]:
    """All .lib files for `platform` under the ORFS platforms dir."""
    libs = sorted((ORFS_FLOW / "platforms" / platform / "lib").glob("*.lib"))
    if not libs:
        raise FileNotFoundError(f"no .lib files for platform {platform!r}")
    return libs


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
    set_rc: Path,
    libs: list[Path],
    top_n: int,
    stage: str,
    top_module: str,
    out_path: Path,
) -> None:
    """Drive openroad to dump the worst logical-path report to out_path.

    `spef` (the stage .spef, if any) and `set_rc` (the platform wire-RC config)
    are handed to the TCL, which selects the parasitics model for the stage.
    """
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {WLP_TCL}"]
    # The TCL exposes find_worst_logical_paths as a proc; pass its inputs as
    # arguments rather than smuggling them through the environment.
    spef_arg = _tcl_braces(str(spef)) if spef is not None else "{}"
    lines.append(
        "find_worst_logical_paths "
        f"{_tcl_braces(str(out_path))} {top_n} {_tcl_braces(stage)} "
        f"{spef_arg} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)}"
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


def _read_report(path: Path) -> pd.DataFrame:
    """Parse the TCL-written report (tab-separated, `#`-prefixed headers) into a
    DataFrame with columns rank / worst_slack_ns / start / end."""
    return pd.read_csv(
        path,
        sep="\t",
        comment="#",
        header=None,
        names=["rank", "worst_slack_ns", "start", "end"],
    )


def analyse(
    run: RunConfig, *, top_n: int = TOP_N, stage: str | None = None
) -> pd.DataFrame:
    """Run STA on the phase dir, write the worst-logical-paths report, and
    return it as a DataFrame. `stage` (a db name in STAGE_DBS) overrides the
    stage; when None it is the latest stage produced by RunConfig.flow_targets."""
    design = run.synth_target.design
    top_module = design.top_module
    platform = run.synth_target.cfg.platform
    phase_dir = run.output_dir

    if stage is None:
        produced = [db for tgt, db in STAGE_DBS.items() if tgt in run.flow_targets]
        if not produced:
            raise ValueError(f"run produced no analysable stage: {run.flow_targets}")
        stage = produced[-1]

    odb, sdc, spef = locate_results(phase_dir, top_module, platform, stage)
    set_rc = ORFS_FLOW / "platforms" / platform / "setRC.tcl"
    libs = liberty_files(platform)
    reports_dir = phase_dir / "reports" / platform / top_module / "base"
    reports_dir.mkdir(parents=True, exist_ok=True)

    out_path = reports_dir / f"worst_logical_paths.{stage}.rpt"
    # Generate thread-safe partial files for concurrent writes
    staging = out_path.with_name(f"{out_path.name}.{secrets.token_hex(8)}.partial")
    run_openroad(odb, sdc, spef, set_rc, libs, top_n, stage, top_module, staging)
    staging.replace(out_path)
    return _read_report(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase_dir", type=Path)
    parser.add_argument("--top-n", type=int, default=TOP_N, metavar="N")
    parser.add_argument(
        "--stage",
        choices=list(STAGE_DBS.values()),
        default=None,
        help="database stage to analyse; defaults to latest",
    )
    args = parser.parse_args()

    phase_dir = args.phase_dir.resolve()
    run = replace(RunConfig.load(phase_dir / RunConfig.FILENAME), output_dir=phase_dir)
    df = analyse(run, top_n=args.top_n, stage=args.stage)
    # Long hierarchical names would otherwise be truncated to 50 chars.
    with pd.option_context(
        "display.max_rows", None, "display.max_colwidth", None, "display.width", None
    ):
        print(df.to_string(index=False))


if __name__ == "__main__":
    main()
