#!/usr/bin/env python3
"""Drive the ORFS flow for a single calibrated `TargetConfig`.

The target carries its own clock period and floorplan side, so no
calibration step is needed. Artifacts land under
`eda_results/<timestamp>/<benchmark>/<name>/<variant>/`.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from common.config import (
    ALL_FLOW_TARGETS,
    EDA_EVAL,
    EDA_RUNS,
    ORFS_FLOW,
    RunConfig,
)
from common.targets import resolve_target
from eda_eval.cache import EDA_CACHE, EXIT_CODE_FILE, link, lookup, publish

SDC_TEMPLATE = EDA_EVAL / "templates" / "constraint.sdc.template"
MAKEFILE_TEMPLATE = EDA_EVAL / "templates" / "Makefile.template"

# Files to keep after pruning (overrides PRUNE_SUFFIXES)
KEEP_FILES = frozenset(
    {
        "1_synth.odb",
        "2_floorplan.odb",
        "3_place.odb",
        "4_cts.odb",
        "5_route.odb",
        "6_final.odb",
        "1_2_yosys.v",
    }
)

# File suffixes prune deletes for ORFS outputs
PRUNE_SUFFIXES = frozenset({".odb", ".def", ".guide", ".rtlil", ".v"})


def snapshot_inputs(run: RunConfig) -> Path:
    """Copy RTL, generate Makefile, generate constraints. Returns the path to
    the rendered Makefile."""
    target = run.synth_target
    design = target.design
    cfg = target.cfg
    inputs = run.output_dir / "inputs"

    # Copy RTL
    rtl_dst = inputs / "rtl"
    rtl_dst.mkdir(parents=True)
    verilog_dsts: list[Path] = []
    for src in design.rtl_abs_paths:
        dst = rtl_dst / src.name
        shutil.copy2(src, dst)
        verilog_dsts.append(dst)

    # Generate SDC with clock period
    sdc_dst = inputs / "constraint.sdc"
    sdc_dst.write_text(
        SDC_TEMPLATE.read_text().format(
            period_ns=target.period_ns,
            io_delay_ns=cfg.io_delay_fraction * target.period_ns,
            clock_port=design.clock_port,
        )
    )

    # Generate Makefile with design config. ORFS auto-sizes the die from
    # CORE_UTILIZATION + CORE_ASPECT_RATIO + CORE_MARGIN, growing the die
    # until both the cell-area-at-utilization and IO-perimeter constraints
    # are satisfied.
    rtl_src = design.root / design.rtl_dir
    include_dirs = " ".join(str(rtl_src / d) for d in design.include_dirs)
    makefile_dst = inputs / "Makefile"
    makefile_dst.write_text(
        MAKEFILE_TEMPLATE.read_text().format(
            top_module=design.top_module,
            design_dir=rtl_dst,
            verilog_files=" ".join(str(v) for v in verilog_dsts),
            verilog_include_dirs=include_dirs,
            sdc_file=sdc_dst,
            work_home=run.output_dir,
            orfs_flow=ORFS_FLOW,
            platform=cfg.platform,
            place_density=cfg.place_density,
            core_utilization=target.target_utilization,
            core_aspect_ratio=cfg.core_aspect_ratio,
            core_margin=cfg.core_margin_um,
            seed=cfg.seed,
            flow_targets=" ".join(run.flow_targets),
            place_pins_args=cfg.place_pins_args,
            synth_memory_max_bits=cfg.synth_memory_max_bits,
            num_threads=run.num_threads,
        )
    )
    return makefile_dst


def prune(run: RunConfig) -> None:
    """Delete auto-generated ORFS intermediates from results to reduce disk space."""
    results = run.output_dir / "results"
    if not results.exists():
        return
    for f in results.rglob("*"):
        if not f.is_file() or f.suffix not in PRUNE_SUFFIXES:
            continue
        if f.name in KEEP_FILES:
            continue
        f.unlink()


def run_job(run: RunConfig) -> int:
    """Run the ORFS flow into the content-addressed cache, symlinking the
    requested `output_dir` at the resulting cache entry. On a cache hit
    no flow work is done; the destination is just relinked."""
    cached = lookup(run)
    if cached is not None:
        link(run.output_dir, cached)
        return 0

    EDA_CACHE.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(dir=EDA_CACHE, prefix=".work-"))
    work_run = dataclasses.replace(run, output_dir=work_dir)
    work_run.dump(work_dir / RunConfig.FILENAME)
    makefile = snapshot_inputs(work_run)
    log_path = work_dir / "flow.log"
    with log_path.open("w") as log:
        rc = subprocess.run(
            ["make", "-C", str(makefile.parent)],
            stdout=log,
            stderr=subprocess.STDOUT,
        ).returncode
    prune(work_run)
    (work_dir / EXIT_CODE_FILE).write_text(f"{rc}\n")
    final = publish(work_dir, run)
    link(run.output_dir, final)
    return rc


def main():
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Name of the target to run.",
    )
    parser.add_argument(
        "--threads-per-run",
        type=int,
        default=None,
        help="NUM_CORES exported to each ORFS invocation (default: spread "
        "host CPUs evenly across --num-threads workers). Lower this to "
        "avoid oversubscription when running many parallel jobs.",
    )
    args = parser.parse_args()
    if args.threads_per_run is None:
        host_cpus = os.cpu_count() or args.num_threads
        args.threads_per_run = max(1, host_cpus // args.num_threads)

    target = resolve_target(args.target)
    design = target.design

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    output_dir = batch_dir / design.benchmark / design.name / design.variant
    print(f"Output dir: {output_dir}")

    run = RunConfig(
        synth_target=target,
        output_dir=output_dir,
        flow_targets=ALL_FLOW_TARGETS,
        num_threads=args.threads_per_run,
    )
    rc = run_job(run)
    if rc != 0:
        raise SystemExit(f"flow FAIL: see {output_dir}/flow.log")


if __name__ == "__main__":
    main()
