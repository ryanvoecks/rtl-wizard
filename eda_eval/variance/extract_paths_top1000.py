#!/usr/bin/env python3
"""Sweep a rewriter-seed batch and emit top-1000 paths-mode reports per stage.

Per variant `<batch>/<bench>/<design>/<seed>/`, for each of 1_synth and 6_final
the script drives openroad with `eda_eval/tcl/worst_logical_paths.tcl` at
top_n=1000 and include_full=1, and writes the rpt next to the existing
worst_logical_blocks reports as

    reports/<platform>/<top>/base/worst_logical_paths_top1000.<stage>.rpt

The 1000/include_full schema is what the cross-seed RBO analysis consumes:
paths-mode masking only collapses bus indices, so on hashed designs the report
isn't subject to the blocks-mode hash-digit over-mask that drops rows from
the per-seed ranking.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from common.config import EDA_EVAL, EDA_RUNS, ORFS_FLOW, RunConfig
from common.executor import run_parallel
from eda_eval.analyse import liberty_files, locate_results

DEFAULT_BATCH = EDA_RUNS / "2026-06-05_00-44-57"
STAGES = ("1_synth", "6_final")
DEFAULT_TOP_N = 1000
DEFAULT_BATCH_SIZE = 25
RPT_NAME = "worst_logical_paths_top1000.{stage}.rpt"
WLP_TCL = EDA_EVAL / "tcl" / "worst_logical_paths.tcl"


def _tcl_braces(value: str) -> str:
    if "{" in value or "}" in value:
        raise ValueError(f"cannot brace-quote for Tcl: {value!r}")
    return "{" + value + "}"


def _run_openroad_batched(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    set_rc: Path,
    libs: list[Path],
    top_n: int,
    stage: str,
    top_module: str,
    out_path: Path,
    batch_size: int,
) -> None:
    """Drive openroad to dump batched top-N paths-mode rpt with include_full=1.

    Mirrors `eda_eval.analyse.run_openroad` but calls
    `find_worst_logical_paths_batched` instead of `find_worst_logical_paths`,
    which pulls batch_size paths per `find_timing_paths` and fires one
    set_false_path per unique selector pair at the end of each batch."""
    spef_arg = _tcl_braces(str(spef)) if spef is not None else "{}"
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {WLP_TCL}"]
    lines.append(
        f"find_worst_logical_paths_batched "
        f"{_tcl_braces(str(out_path))} {top_n} {_tcl_braces(stage)} "
        f"{spef_arg} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)} "
        f"{batch_size} 1"
    )
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input="\n".join(lines),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad batched paths extraction failed")


def _extract_one(
    variant: Path,
    stage: str,
    top_n: int,
    skip_existing: bool,
    batch_size: int,
) -> tuple[Path, str, str]:
    """Run openroad to dump `worst_logical_paths_top1000.<stage>.rpt`.

    Returns (variant, stage, status) where status is "ok", "skipped",
    "missing-odb", or "fail:<msg>"."""
    rc_path = variant / RunConfig.FILENAME
    if not rc_path.is_file():
        return variant, stage, "missing-run_config"
    run = RunConfig.load(rc_path)
    run = replace(run, output_dir=variant)
    design = run.synth_target.design
    top_module = design.top_module
    platform = run.synth_target.cfg.platform
    reports_dir = variant / "reports" / platform / top_module / "base"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / RPT_NAME.format(stage=stage)
    if skip_existing and out_path.is_file():
        return variant, stage, "skipped"
    try:
        odb, sdc, spef = locate_results(variant, top_module, platform, stage)
    except Exception as exc:  # noqa: BLE001
        return variant, stage, f"fail:locate:{exc}"

    set_rc = ORFS_FLOW / "platforms" / platform / "setRC.tcl"
    libs = liberty_files(platform)
    try:
        _run_openroad_batched(
            odb,
            sdc,
            spef,
            set_rc,
            libs,
            top_n,
            stage,
            top_module,
            out_path,
            batch_size,
        )
    except Exception as exc:  # noqa: BLE001
        return variant, stage, f"fail:openroad:{exc}"
    return variant, stage, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--batch", type=Path, default=DEFAULT_BATCH)
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    ap.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="paths pulled per find_timing_paths call (default: %(default)s).",
    )
    ap.add_argument(
        "--parallel",
        type=int,
        default=8,
        help="ProcessPool workers (default: %(default)s).",
    )
    ap.add_argument(
        "--variant-glob",
        default="*/*/reference_rewrite_seed_*",
        help="glob (relative to batch root) selecting variant dirs.",
    )
    ap.add_argument(
        "--rerun",
        action="store_true",
        help="redo variants whose rpt already exists.",
    )
    args = ap.parse_args()

    batch: Path = args.batch
    variants = sorted(batch.glob(args.variant_glob))
    print(f"Found {len(variants)} variant dirs under {batch}")
    jobs = [(v, st) for v in variants for st in STAGES]
    submitted = [
        ((v, st), (v, st, args.top_n, not args.rerun, args.batch_size))
        for v, st in jobs
    ]

    n_ok = n_skip = n_fail = 0
    for (v, st), result in run_parallel(
        _extract_one,
        submitted,
        max_workers=args.parallel,
        description=f"paths top-{args.top_n}",
    ):
        _, _, status = result
        rel = v.relative_to(batch)
        if status == "ok":
            n_ok += 1
        elif status == "skipped":
            n_skip += 1
        else:
            n_fail += 1
            print(f"  {status}  {rel}  {st}")

    print(f"\nDone: ok={n_ok}  skipped={n_skip}  fail={n_fail}  total={len(jobs)}")
    if n_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
