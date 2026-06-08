#!/usr/bin/env python3
"""Dump the top-N worst logical paths or blocks for a phase dir.

Thin wrapper around tcl/worst_logical_paths.tcl: it loads a stage database into
openroad and lets the TCL pick the right interconnect parasitics for that stage
(the .odb persists none) and do the dedup, so the report has one entry per
(source, dest) pair, ranked worst-slack first.

`--kind paths`  collapses only bus indices "[N]"; one row per n-bit-bus to
                n-bit-bus pair.
`--kind blocks` collapses every numeric run in the instance name as well, so
                replicated submodules (e.g. `dct_block_0..7`) map to one entry.
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
KIND_PATHS = "paths"
KIND_BLOCKS = "blocks"
KINDS = (KIND_PATHS, KIND_BLOCKS)

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
WCB_TCL = EDA_EVAL / "tcl" / "worst_corrected_blocks.tcl"

# Frozen predictor coefficients (median-of-medians on 140 valid LLM iters,
# design-LOO). See predictor/data/calibration_iters/global_beta.csv.
BETA_INPUT = +0.231
BETA_OUTPUT = -0.332
BETA_INTERNAL = -0.032
DEFAULT_CORRECTED_BATCH = 25


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
    kind: str = KIND_PATHS,
    include_full: bool = False,
    apply_correction: bool = False,
    batch_size: int = DEFAULT_CORRECTED_BATCH,
    include_debug: bool = False,
) -> None:
    """Drive openroad to dump the worst-logical-{paths,blocks} report.

    `include_full=True` emits the 6-column schema (adds start_full / end_full);
    default is the 4-column schema.
    `apply_correction=True` runs the predictor's corrector-aware extractor
    instead -- top-N corrected-slack blocks with early termination. `kind`
    and `include_full` are ignored in that mode. `include_debug=True` adds
    beta/iter/n_pulled metadata and the synth_slack/port_class columns;
    default is a minimal 4-column rank/corr_slack/start/end schema.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}, expected one of {KINDS}")
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}"]
    spef_arg = _tcl_braces(str(spef)) if spef is not None else "{}"

    if apply_correction:
        lines += [f"source {WCB_TCL}"]
        lines.append(
            f"find_top_corrected_blocks "
            f"{_tcl_braces(str(out_path))} {top_n} {_tcl_braces(stage)} "
            f"{spef_arg} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)} "
            f"{BETA_INPUT:.4f} {BETA_OUTPUT:.4f} {BETA_INTERNAL:.4f} "
            f"{batch_size} 1 {1 if include_debug else 0}"
        )
    else:
        lines += [f"source {WLP_TCL}"]
        tcl_proc = (
            "find_worst_logical_blocks"
            if kind == KIND_BLOCKS
            else "find_worst_logical_paths"
        )
        # Trailing positional args are skip_async, include_full. The synth-time
        # async quarantine (skip_async=1) is only used by predictor/, not here.
        lines.append(
            f"{tcl_proc} "
            f"{_tcl_braces(str(out_path))} {top_n} {_tcl_braces(stage)} "
            f"{spef_arg} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)} "
            f"0 {1 if include_full else 0}"
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


_REPORT_COLS_4 = ["rank", "worst_slack_ns", "start", "end"]
_REPORT_COLS_6 = _REPORT_COLS_4 + ["start_full", "end_full"]
_REPORT_COLS_CORRECTED_MIN = ["rank", "corr_slack_ns", "start", "end"]
_REPORT_COLS_CORRECTED_DEBUG = [
    "rank", "corr_slack_ns", "synth_slack_ns", "port_class", "start", "end",
]


def _read_report(
    path: Path, *, apply_correction: bool = False, include_debug: bool = False
) -> pd.DataFrame:
    """Parse the TCL-written rpt. The default schema (4 or 6 cols) is sniffed
    from the first data row; the corrected schemas are opted into explicitly."""
    if apply_correction:
        names = (
            _REPORT_COLS_CORRECTED_DEBUG if include_debug
            else _REPORT_COLS_CORRECTED_MIN
        )
        return pd.read_csv(path, sep="\t", comment="#", header=None, names=names)
    n_cols = 4
    for raw in path.read_text().splitlines():
        if not raw or raw.startswith("#"):
            continue
        n_cols = 6 if len(raw.split("\t")) >= 6 else 4
        break
    names = _REPORT_COLS_6 if n_cols == 6 else _REPORT_COLS_4
    return pd.read_csv(path, sep="\t", comment="#", header=None, names=names)


def analyse(
    run: RunConfig,
    *,
    top_n: int = TOP_N,
    stage: str | None = None,
    kind: str = KIND_PATHS,
    include_full: bool = False,
    apply_correction: bool = False,
    batch_size: int = DEFAULT_CORRECTED_BATCH,
    include_debug: bool = False,
) -> pd.DataFrame:
    """Run STA on the phase dir and return the worst-logical-{paths,blocks} rpt.

    `stage` overrides the latest produced stage; `kind` picks bus-index dedup
    ("paths") vs full numeric dedup ("blocks"); `include_full` opts into the
    6-column rpt format. `apply_correction` switches to the corrector-aware
    top-N extractor (synth-side, blocks) with `batch_size` paths pulled per
    iteration; default emits a minimal 4-column rank/corr_slack/start/end
    schema, `include_debug` adds beta/iter metadata and extra columns."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}, expected one of {KINDS}")
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

    rpt_name = (
        f"top_corrected_blocks.{stage}.rpt"
        if apply_correction
        else f"worst_logical_{kind}.{stage}.rpt"
    )
    out_path = reports_dir / rpt_name
    # Generate thread-safe partial files for concurrent writes
    staging = out_path.with_name(f"{out_path.name}.{secrets.token_hex(8)}.partial")
    run_openroad(
        odb,
        sdc,
        spef,
        set_rc,
        libs,
        top_n,
        stage,
        top_module,
        staging,
        kind,
        include_full,
        apply_correction,
        batch_size,
        include_debug,
    )
    staging.replace(out_path)
    return _read_report(
        out_path, apply_correction=apply_correction, include_debug=include_debug
    )


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
    parser.add_argument(
        "--kind",
        choices=list(KINDS),
        default=KIND_PATHS,
        help="logical-path dedup level (default: paths)",
    )
    parser.add_argument(
        "--include-full",
        action="store_true",
        help="add start_full / end_full columns (6-col rpt instead of 4-col)",
    )
    parser.add_argument(
        "--apply-correction",
        action="store_true",
        help="use the predictor-aware top-N corrected-blocks extractor instead",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_CORRECTED_BATCH,
        help="paths pulled per iteration (only with --apply-correction)",
    )
    parser.add_argument(
        "--include-debug",
        action="store_true",
        help="add beta / iter / synth_slack / port_class metadata + columns",
    )
    args = parser.parse_args()

    phase_dir = args.phase_dir.resolve()
    run = replace(RunConfig.load(phase_dir / RunConfig.FILENAME), output_dir=phase_dir)
    df = analyse(
        run,
        top_n=args.top_n,
        stage=args.stage,
        kind=args.kind,
        include_full=args.include_full,
        apply_correction=args.apply_correction,
        batch_size=args.batch_size,
        include_debug=args.include_debug,
    )
    # Long hierarchical names would otherwise be truncated to 50 chars.
    with pd.option_context(
        "display.max_rows", None, "display.max_colwidth", None, "display.width", None
    ):
        print(df.to_string(index=False))


if __name__ == "__main__":
    main()
