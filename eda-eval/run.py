#!/usr/bin/env python3
"""Drive the ORFS flow over every discovered design (or a glob-filtered
subset via --benchmark/--name/--variant).

Each (benchmark, name) group first runs a shared `__calibration__` on the
reference variant at a loose period and large die. The calibration's
worst setup slack and cell area derive the period and floorplan used for
the routed run of every variant in the group.

Per-batch artifacts land under
`eda-results/<timestamp>/<benchmark>/<name>/{__calibration__,<variant>}/`,
plus a top-level `runs.csv` summarizing each run's error status.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import math
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from extract_metrics import extract
from tqdm import tqdm

from common.config import (
    EDA_RUNS,
    ORFS_HOME,
    RUN_CONFIG_FILENAME,
    DesignConfig,
    RunConfig,
    RunJob,
    StudyConfig,
    dump_run_config,
)
from common.loader import AllDesigns, DesignTree

HERE = Path(__file__).resolve().parent
SDC_TEMPLATE = HERE / "templates" / "constraint.sdc.template"
MAKEFILE_TEMPLATE = HERE / "templates" / "Makefile.template"


def filter_designs(
    designs: DesignTree,
    benchmark_globs: list[str] | None,
    name_globs: list[str] | None,
    variant_globs: list[str] | None,
) -> DesignTree:
    """Keep designs whose benchmark/name/variant match at least one glob in
    each non-empty filter list. A `None` filter means that field is
    unconstrained, so an unfiltered call returns `designs` unchanged."""

    def matches(value: str, patterns: list[str] | None) -> bool:
        return patterns is None or any(fnmatch.fnmatchcase(value, p) for p in patterns)

    out: DesignTree = {}
    for b, names in designs.items():
        if not matches(b, benchmark_globs):
            continue
        for n, variants in names.items():
            if not matches(n, name_globs):
                continue
            for v, d in variants.items():
                if matches(v, variant_globs):
                    out.setdefault(b, {}).setdefault(n, {})[v] = d
    return out


def design_variant_dir(design: DesignConfig, batch_dir: Path) -> Path:
    """Output dir for this variant's run."""
    return batch_dir / design.benchmark / design.name / design.variant


def design_calibration_dir(design: DesignConfig, batch_dir: Path) -> Path:
    """Output dir for the calibration step."""
    return batch_dir / design.benchmark / design.name / "__calibration__"


def render_floorplan(side_um: float, core_margin_um: float) -> tuple[str, str]:
    """ORFS DIE_AREA/CORE_AREA strings for a square `side_um` x `side_um`
    die with a `core_margin_um` boundary on each edge."""
    inner = side_um - core_margin_um
    die_area = f"0 0 {side_um:.3f} {side_um:.3f}"
    core_area = f"{core_margin_um:.3f} {core_margin_um:.3f} {inner:.3f} {inner:.3f}"
    return die_area, core_area


def snapshot_inputs(run: RunConfig) -> Path:
    """Copy RTL, generate Makefile, generate constraints. Returns the path to
    the rendered Makefile."""
    inputs = run.output_dir / "inputs"

    # Copy RTL
    rtl_dst = inputs / "rtl"
    rtl_dst.mkdir(parents=True)
    verilog_dsts: list[Path] = []
    for src in run.design.rtl_files:
        dst = rtl_dst / src.name
        shutil.copy2(src, dst)
        verilog_dsts.append(dst)

    # Generate SDC with clock period
    sdc_dst = inputs / "constraint.sdc"
    sdc_dst.write_text(
        SDC_TEMPLATE.read_text().format(
            period_ns=run.period_ns,
            io_delay_ns=run.cfg.io_delay_ns,
        )
    )

    # Generate Makefile with design config
    die_area, core_area = render_floorplan(run.side_um, run.cfg.core_margin_um)
    makefile_dst = inputs / "Makefile"
    makefile_dst.write_text(
        MAKEFILE_TEMPLATE.read_text().format(
            top_module=run.design.top_module,
            design_dir=rtl_dst,
            verilog_files=" ".join(str(v) for v in verilog_dsts),
            sdc_file=sdc_dst,
            work_home=run.output_dir,
            orfs_home=ORFS_HOME,
            platform=run.cfg.platform,
            place_density=run.cfg.place_density,
            die_area=die_area,
            core_area=core_area,
            seed=run.cfg.seed,
            flow_targets="synth synth-report floorplan place cts route do-finish",
        )
    )
    return makefile_dst


def run_job(run: RunConfig) -> int:
    """Invoke the rendered per-design Makefile."""
    run.output_dir.mkdir(parents=True, exist_ok=True)
    dump_run_config(run, run.output_dir / RUN_CONFIG_FILENAME)
    makefile = snapshot_inputs(run)
    log_path = run.output_dir / "flow.log"
    with log_path.open("w") as log:
        return subprocess.run(
            ["make", "-C", str(makefile.parent)],
            stdout=log,
            stderr=subprocess.STDOUT,
        ).returncode


def derive_final_side_um(cell_area_um2: float, cfg: StudyConfig) -> float:
    """Take a multiple of the cell area, or the minimum area if too small."""
    effective_cell_area = cell_area_um2 * cfg.area_multiplier
    core_side = math.sqrt(effective_cell_area / cfg.target_utilization)
    natural_side = core_side + 2 * cfg.core_margin_um
    return max(natural_side, cfg.minimum_side_um)


def run_jobs(
    runs: list[RunConfig],
    num_threads: int,
    desc: str,
) -> dict[RunConfig, int]:
    """Drive each `RunConfig` through `run_job` on a thread pool, with a
    progress bar."""
    if not runs:
        raise ValueError("No jobs to run")
    workers = max(1, min(num_threads, len(runs)))
    rcs: dict[RunConfig, int] = {}
    passed = failed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_job, r): r for r in runs}
        with tqdm(total=len(runs), desc=desc, unit="run") as pbar:
            pbar.set_postfix(passed=passed, failed=failed)
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    rc = fut.result()
                except Exception:
                    rc = 1
                if rc == 0:
                    passed += 1
                else:
                    failed += 1
                rcs[r] = rc
                pbar.set_postfix(passed=passed, failed=failed)
                pbar.update(1)
    return rcs


def main():
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--benchmark",
        action="append",
        default=None,
        metavar="GLOB",
        help="Restrict to designs whose benchmark matches one of these globs. "
        "Repeatable; a design matches if *any* given glob hits.",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=None,
        metavar="GLOB",
        help="Restrict to designs whose name matches one of these globs. Repeatable.",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=None,
        metavar="GLOB",
        help="Restrict to designs whose variant matches one of these globs. "
        "Repeatable.",
    )
    parser.add_argument(
        "--num-threads",
        type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Worker count for each parallel phase (default: half of host "
        "CPU count). All calibrations run as one batch, then all finals "
        "run as a second batch.",
    )
    args = parser.parse_args()

    cfg = StudyConfig()

    if not (ORFS_HOME / "Makefile").is_file():
        raise FileNotFoundError("ORFS flow not found - set config.ORFS_HOME")

    all_designs = AllDesigns.designs()
    designs = filter_designs(
        all_designs,
        args.benchmark,
        args.name,
        args.variant,
    )
    if not designs:
        raise ValueError("No designs matched specified filters")

    # Group filtered designs and check for valid references
    references: dict[tuple[str, str], DesignConfig] = {}
    groups: dict[tuple[str, str], list[DesignConfig]] = {}
    for b, names in designs.items():
        for n, variants in names.items():
            if "reference" not in all_designs[b][n]:
                raise ValueError(f"no 'reference' variant available for {b}/{n}")
            groups[(b, n)] = list(variants.values())
            references[(b, n)] = all_designs[b][n]["reference"]

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {batch_dir}")

    # Phase 1: run every group's calibration (on the reference variant).
    cal_runs: dict[tuple[str, str], RunConfig] = {
        key: RunConfig(
            design=references[key],
            output_dir=design_calibration_dir(references[key], batch_dir),
            period_ns=cfg.calibration_period_ns,
            side_um=cfg.calibration_side_um,
            cfg=cfg,
        )
        for key in groups
    }
    cal_rcs = run_jobs(
        list(cal_runs.values()),
        args.num_threads,
        "Calibration",
    )

    # Build one RunJob per variant, and record each group's calibration as
    # its own synthetic `__calibration__` row. Calibration failures (rc != 0
    # or unparseable outputs) attach as `error` on every variant in the
    # group and skip execution, but the RunJob still carries the design so
    # the row shows in runs.csv.
    final_runs: dict[DesignConfig, RunJob] = {}
    runs_report: list[dict] = []
    for key, variants in groups.items():
        b, n = key
        cal_run = cal_runs[key]
        cal_rc = cal_rcs[cal_run]
        error: str | None = None
        period_ns = cfg.calibration_period_ns
        side_um = cfg.calibration_side_um
        if cal_rc != 0:
            error = f"calibration FAIL (rc={cal_rc})"
        else:
            try:
                metrics = extract(cal_run.output_dir, cal_run.design.top_module)
                ws_ns = metrics["route_ws_ns"]
                cell_area_um2 = metrics["synth_area_um2"]
                period_ns = (cfg.calibration_period_ns - ws_ns) * cfg.target_multiplier
                side_um = derive_final_side_um(cell_area_um2, cfg)
            except Exception as exc:
                error = f"calibration parse FAIL: {exc}"
        runs_report.append(
            {
                "benchmark": b,
                "name": n,
                "variant": "__calibration__",
                "error": error,
            }
        )
        for d in variants:
            final_runs[d] = RunJob(
                run=RunConfig(
                    design=d,
                    output_dir=design_variant_dir(d, batch_dir),
                    period_ns=period_ns,
                    side_um=side_um,
                    cfg=cfg,
                ),
                error=error,
            )

    runnable = [job.run for job in final_runs.values() if job.error is None]
    final_rcs = run_jobs(runnable, args.num_threads, "Eval       ")

    for d, job in final_runs.items():
        if job.error is not None:
            error = job.error
        else:
            rc = final_rcs[job.run]
            error = f"final FAIL (rc={rc})" if rc != 0 else None
        runs_report.append(
            {
                "benchmark": d.benchmark,
                "name": d.name,
                "variant": d.variant,
                "error": error,
            }
        )
    runs_report.sort(key=lambda e: (e["benchmark"], e["name"], e["variant"]))
    with (batch_dir / "runs.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["benchmark", "name", "variant", "error"])
        writer.writeheader()
        writer.writerows(runs_report)


if __name__ == "__main__":
    main()
