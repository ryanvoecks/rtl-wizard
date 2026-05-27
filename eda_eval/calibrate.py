#!/usr/bin/env python3
"""Calibrate a design's clock period and floorplan size via a synth +
P&R sweep. Writes `calibration.json` with the chosen (period, side,
utilization) and the per-iteration log.

Usage:
    uv run eda_eval/calibrate.py --design aes_reference
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass
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
from common.designs import resolve_design
from eda_eval.extract_metrics import extract, extract_synth
from eda_eval.run import run_job

Phase = Literal["synth", "pnr"]

PNR_PASSES = 4  # Total number of P&R passes
PRESSURE = 1.5  # Target ratio of post-route achievable period to clock period
UTILIZATION_STEP = 0.10  # Utilization step used to relax density after a P&R failure


def iter_output_dir(
    design: DesignConfig, batch_dir: Path, phase: Phase, i: int
) -> Path:
    """Per-step phase dir."""
    return batch_dir / design.benchmark / design.name / f"iter_{phase}_{i}"


def derive_final_side_um(
    cell_area_um2: float,
    io_pin_count: int,
    cfg: StudyConfig,
    utilization: float,
) -> float:
    """Square-die side that satisfies all three lower bounds: cell-area
    at `utilization`, IO perimeter at `effective_pin_width` per pin, and
    the absolute `minimum_side_um`."""
    area_side = math.sqrt(cell_area_um2 / utilization)
    area_side += 2 * cfg.core_margin_um
    pin_side = io_pin_count * cfg.effective_pin_width / 4
    return max(area_side, pin_side, cfg.minimum_side_um)


class RunJobFailed(RuntimeError):
    """The ORFS flow returned a non-zero exit code."""


@dataclass(frozen=True)
class PnrStep:
    """One iteration of the P&R sweep. Route metrics are present iff
    `completed` is True."""

    iter: int
    period_ns: float
    side_um: float
    utilization: float
    output_dir: Path
    completed: bool
    route_ws_ns: float = float("NaN")
    route_area_um2: float = float("NaN")
    realised_pressure: float = float("NaN")


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
) -> dict:
    """Run one ORFS pass and return its parsed metrics. Raises
    `RunJobFailed` if the flow did not complete."""
    output_dir = iter_output_dir(design, batch_dir, phase, i)
    run = RunConfig(
        synth_target=TargetConfig(
            design=design,
            period_ns=period_ns,
            side_um=side_um,
            cfg=cfg,
        ),
        output_dir=output_dir,
        flow_targets=flow_targets,
        num_threads=num_threads,
    )
    if run_job(run) != 0:
        raise RunJobFailed(f"see {output_dir}/flow.log")
    extractor = extract_synth if phase == "synth" else extract
    return extractor(output_dir, design.top_module)


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
    parser.add_argument(
        "--design",
        required=True,
        help="Name of the design to calibrate.",
    )
    parser.add_argument(
        "--num-threads",
        type=int,
        default=os.cpu_count() or 1,
        help="ORFS threads. Default: nproc. Lower for multi-job parallelism.",
    )
    args = parser.parse_args()

    cfg = StudyConfig()
    design = resolve_design(args.design)

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {batch_dir}")
    print(
        f"Calibrating {design.benchmark}/{design.name}/{design.variant} "
        f"(top={design.top_module})"
    )

    # Step 1: synth-only at the over-constrained calibration clock.
    print(
        f"\nStep 1: synth-only at T={cfg.calibration_period_ns} ns, "
        f"side={cfg.calibration_side_um:.0f} um (over-constrained)"
    )
    m1 = run_iteration(
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

    # Anchor + corrective sweep.
    # Iteration 0: (T = pressure * T_synth_close, U = U_def).
    # Iterations 1..N-1: shrink T toward T_close on success, relax U on failure.
    t_synth_close = cfg.calibration_period_ns - synth_ws_1
    t = PRESSURE * t_synth_close
    u = cfg.target_utilization
    area = synth_area_1

    pnr_steps: list[PnrStep] = []
    completed: list[PnrStep] = []

    for i in range(PNR_PASSES):
        side = derive_final_side_um(area, io_pin_count, cfg, utilization=u)
        print(
            f"\nStep {i + 2}: P&R at T={t:.4f} ns "
            f"({fmax_mhz(t):.2f} MHz), U={u:.2f}, side={side:.2f} um"
        )
        output_dir = iter_output_dir(design, batch_dir, "pnr", i)
        try:
            m = run_iteration(
                design,
                batch_dir,
                "pnr",
                i,
                t,
                side,
                cfg,
                ALL_FLOW_TARGETS,
                args.num_threads,
            )
        except RunJobFailed as exc:
            pnr_steps.append(
                PnrStep(
                    iter=i,
                    period_ns=t,
                    side_um=side,
                    utilization=u,
                    output_dir=output_dir,
                    completed=False,
                )
            )
            print(f"  FAILED: {exc}")
            u = u - UTILIZATION_STEP
            continue
        ws = _require_finite(m, "route_ws_ns")
        area = _require_finite(m, "route_area_um2")
        realised = (t - ws) / t
        step = PnrStep(
            iter=i,
            period_ns=t,
            side_um=side,
            utilization=u,
            output_dir=output_dir,
            completed=True,
            route_ws_ns=ws,
            route_area_um2=area,
            realised_pressure=realised,
        )
        completed.append(step)
        pnr_steps.append(step)
        print(
            f"  route_ws={ws:+.4f} ns, route_area={area:.1f} um^2, "
            f"realised_pressure={realised:.3f}"
        )
        t = (t - ws) / PRESSURE

    if not completed:
        raise RuntimeError("no P&R run completed")

    best = min(
        completed,
        key=lambda s: abs((s.realised_pressure or 0.0) - PRESSURE),
    )

    summary = {
        "design": {
            "benchmark": design.benchmark,
            "name": design.name,
            "variant": design.variant,
            "top_module": design.top_module,
        },
        "result": {
            "period_ns": best.period_ns,
            "side_um": best.side_um,
            "utilization": best.utilization,
            "fmax_mhz": fmax_mhz(best.period_ns),
            "route_ws_ns": best.route_ws_ns,
            "route_area_um2": best.route_area_um2,
            "realised_pressure": best.realised_pressure,
            "selected_iter": best.iter,
        },
        "step1_synth": {
            "period_ns": cfg.calibration_period_ns,
            "side_um": cfg.calibration_side_um,
            "synth_ws_ns": synth_ws_1,
            "synth_area_um2": synth_area_1,
            "io_pin_count": io_pin_count,
            "output_dir": str(iter_output_dir(design, batch_dir, "synth", 0)),
        },
        "pnr_steps": [asdict(s) for s in pnr_steps],
    }
    summary_path = batch_dir / design.benchmark / design.name / "calibration.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    print(
        f"\nResult: iter={best.iter} | "
        f"period={best.period_ns:.4f} ns | "
        f"side={best.side_um:.2f} um | U={best.utilization:.2f} | "
        f"fmax={fmax_mhz(best.period_ns):.2f} MHz | "
        f"realised_pressure={best.realised_pressure:.3f}"
    )
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
