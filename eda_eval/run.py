#!/usr/bin/env python3
"""Drive the ORFS flow for a single calibrated `TargetConfig`.

The target carries its own clock period and floorplan side, so no
calibration step is needed. Artifacts land under
`eda_results/<timestamp>/<benchmark>/<name>/<variant>/`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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
from eda_eval.cache import lookup, record, replay

SDC_TEMPLATE = EDA_EVAL / "templates" / "constraint.sdc.template"
MAKEFILE_TEMPLATE = EDA_EVAL / "templates" / "Makefile.template"


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

    # Generate Makefile with design config
    die_area, core_area = render_floorplan(target.side_um, cfg.core_margin_um)
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
            die_area=die_area,
            core_area=core_area,
            seed=cfg.seed,
            flow_targets=" ".join(run.flow_targets),
            place_pins_args=cfg.place_pins_args,
            synth_memory_max_bits=cfg.synth_memory_max_bits,
            num_threads=run.num_threads,
        )
    )
    return makefile_dst


def run_job(run: RunConfig) -> int:
    """Invoke the rendered per-design Makefile."""
    run.output_dir.mkdir(parents=True, exist_ok=True)
    run.dump(run.output_dir / RunConfig.FILENAME)
    cached = lookup(run)
    if cached is not None:
        replay(cached, run.output_dir)
        return 0
    makefile = snapshot_inputs(run)
    log_path = run.output_dir / "flow.log"
    with log_path.open("w") as log:
        rc = subprocess.run(
            ["make", "-C", str(makefile.parent)],
            stdout=log,
            stderr=subprocess.STDOUT,
        ).returncode
    record(run)
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
