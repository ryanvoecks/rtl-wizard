#!/usr/bin/env python3
"""Three-step deterministic calibration:

  1. Synth-only at an over-constrained clock (default 0.5 ns) on an
     under-constrained 1mm x 1mm die. Read post-synth worst slack and
     cell area.
  2. First full P&R at T = T1 - synth_ws (the synth's projected period)
     and side derived from the synth cell area at `cfg.target_utilization`.
  3. Second full P&R at T = T2 - route_ws (predicts ws = 0) and side
     re-derived from step 2's post-route stdcell area (corrects the
     utilisation now that CTS / repair buffers are accounted for).

Step 3's (period, side) are the calibrated targets for the design.

Usage:
    uv run eda_eval/calibrate.py
    uv run eda_eval/calibrate.py --benchmark secworks --name aes
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Literal

from common.config import (
    ALL_FLOW_TARGETS,
    EDA_RUNS,
    SYNTH_FLOW_TARGETS,
    DesignConfig,
    RunConfig,
    StudyConfig,
    TargetConfig,
)
from common.designs import all_designs
from eda_eval.extract_metrics import extract, extract_synth
from eda_eval.run import run_job

Phase = Literal["synth", "pnr"]

# Number of corrective P&R passes after the initial over-constrained synth.
PNR_PASSES = 2


def iter_output_dir(
    design: DesignConfig, batch_dir: Path, phase: Phase, i: int
) -> Path:
    """Per-step phase dir."""
    return batch_dir / design.benchmark / design.name / f"iter_{phase}_{i}"


def derive_final_side_um(
    cell_area_um2: float, io_pin_count: int, cfg: StudyConfig
) -> float:
    """Square-die side that satisfies all three lower bounds: cell-area
    at `target_utilization`, IO perimeter at `effective_pin_width` per
    pin, and the absolute `minimum_side_um`."""
    area_side = math.sqrt(cell_area_um2 / cfg.target_utilization)
    area_side += 2 * cfg.core_margin_um
    pin_side = io_pin_count * cfg.effective_pin_width / 4
    return max(area_side, pin_side, cfg.minimum_side_um)


def run_iteration(
    design: DesignConfig,
    batch_dir: Path,
    phase: Phase,
    i: int,
    period_ns: float,
    side_um: float,
    cfg: StudyConfig,
    flow_targets: tuple[str, ...],
    num_threads: int,
) -> tuple[RunConfig, dict]:
    """One ORFS pass at the given period and die size. Returns the run
    config plus parsed metrics."""
    run = RunConfig(
        synth_target=TargetConfig(
            design=design,
            period_ns=period_ns,
            side_um=side_um,
            cfg=cfg,
        ),
        output_dir=iter_output_dir(design, batch_dir, phase, i),
        flow_targets=flow_targets,
        num_threads=num_threads,
    )
    rc = run_job(run)
    if rc != 0:
        raise RuntimeError(
            f"iter_{phase}_{i} ORFS run failed: see {run.output_dir}/flow.log"
        )
    extractor = extract_synth if phase == "synth" else extract
    return run, extractor(run.output_dir, design.top_module)


def fmax_mhz(period_ns: float) -> float:
    return 1000.0 / period_ns


def _require_finite(metrics: dict, key: str) -> float:
    v = float(metrics[key])
    if math.isnan(v):
        raise RuntimeError(f"NaN {key}")
    return v


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--benchmark", default="corpus")
    parser.add_argument("--name", default="counter_array")
    parser.add_argument("--variant", default="claude")
    parser.add_argument(
        "--num-threads",
        type=int,
        default=os.cpu_count() or 1,
        help="ORFS threads. Default: nproc. Lower for multi-job parallelism.",
    )
    args = parser.parse_args()

    cfg = StudyConfig()
    design = all_designs[args.benchmark][args.name][args.variant]

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {batch_dir}")
    print(
        f"Calibrating {design.benchmark}/{design.name}/{design.variant} "
        f"(top={design.top_module})"
    )

    # Single over-constrained synth pass (tight clock, large die).
    print(
        f"\nStep 1: synth-only at T={cfg.calibration_period_ns} ns, "
        f"side={cfg.calibration_side_um:.0f} um (over-constrained)"
    )
    run1, m1 = run_iteration(
        design,
        batch_dir,
        "synth",
        0,
        cfg.calibration_period_ns,
        cfg.calibration_side_um,
        cfg,
        SYNTH_FLOW_TARGETS,
        args.num_threads,
    )
    synth_ws_1 = _require_finite(m1, "synth_ws_ns")
    synth_area_1 = _require_finite(m1, "synth_area_um2")
    io_pin_count = int(_require_finite(m1, "io_pin_count"))
    print(
        f"  synth_ws={synth_ws_1:+.4f} ns, "
        f"synth_area={synth_area_1:.1f} um^2, "
        f"io_pins={io_pin_count}"
    )

    # Multiple P&R passes driving ws -> 0 and re-sizing from the last cell area
    t = cfg.calibration_period_ns
    ws = synth_ws_1
    area = synth_area_1
    pnr_steps: list[dict] = []
    for j in range(PNR_PASSES):
        t = t - ws
        side = derive_final_side_um(area, io_pin_count, cfg)
        print(
            f"\nStep {j + 2}: P&R at T={t:.4f} ns ({fmax_mhz(t):.2f} MHz), "
            f"side={side:.2f} um"
        )
        run, m = run_iteration(
            design,
            batch_dir,
            "pnr",
            j,
            t,
            side,
            cfg,
            ALL_FLOW_TARGETS,
            args.num_threads,
        )
        ws = _require_finite(m, "route_ws_ns")
        area = _require_finite(m, "route_area_um2")
        print(f"  route_ws={ws:+.4f} ns, route_area={area:.1f} um^2")
        pnr_steps.append(
            {
                "period_ns": t,
                "side_um": side,
                "route_ws_ns": ws,
                "route_area_um2": area,
                "output_dir": str(run.output_dir),
            }
        )

    # Report results
    final = pnr_steps[-1]
    meets = ws >= 0
    summary = {
        "design": {
            "benchmark": design.benchmark,
            "name": design.name,
            "variant": design.variant,
            "top_module": design.top_module,
        },
        "result": {
            "period_ns": final["period_ns"],
            "side_um": final["side_um"],
            "fmax_mhz": fmax_mhz(final["period_ns"]),
            "route_ws_ns": ws,
            "route_area_um2": area,
            "meets_timing": meets,
        },
        "step1_synth": {
            "period_ns": cfg.calibration_period_ns,
            "side_um": cfg.calibration_side_um,
            "synth_ws_ns": synth_ws_1,
            "synth_area_um2": synth_area_1,
            "output_dir": str(run1.output_dir),
        },
        "pnr_steps": pnr_steps,
    }
    summary_path = batch_dir / design.benchmark / design.name / "calibration.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    print(
        f"\nResult: period={final['period_ns']:.4f} ns | "
        f"side={final['side_um']:.2f} um | "
        f"fmax={fmax_mhz(final['period_ns']):.2f} MHz | meets_timing={meets}"
    )
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
