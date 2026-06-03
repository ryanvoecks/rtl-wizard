#!/usr/bin/env python3
"""Calibrate a design's clock period via a synth + P&R sweep at
U=`cfg.target_utilization` (ORFS auto-sizes the die). Writes
`calibration.json` with the chosen period and the per-iteration log.

The sweep targets a constant `INITIAL_PRESSURE` ratio of
post-route-achievable period to clock period. If the ORFS flow fails at
the current pressure, the pressure is backed off by `PRESSURE_STEP`
(1.5 -> 1.4 -> 1.3 -> ...) and retried until either the flow completes
or the floor `PRESSURE_FLOOR` is reached.

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

PNR_PASSES = 3  # Number of successful P&R passes targeted
INITIAL_PRESSURE = 1.5  # Starting ratio of post-route closure period to target T
PRESSURE_STEP = 0.10  # Pressure backoff per flow failure
PRESSURE_FLOOR = 1.0  # Below this, the calibrator gives up


def iter_output_dir(
    design: DesignConfig, batch_dir: Path, phase: Phase, i: int
) -> Path:
    """Per-step phase dir."""
    return batch_dir / design.benchmark / design.name / f"iter_{phase}_{i}"


class RunJobFailed(RuntimeError):
    """The ORFS flow returned a non-zero exit code."""


@dataclass(frozen=True)
class PnrStep:
    """One iteration of the P&R sweep. Route metrics are present iff
    `completed` is True."""

    iter: int
    period_ns: float
    pressure: float
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
        f"(top={design.top_module}, U={cfg.target_utilization})"
    )

    # Step 1: synth-only at the over-constrained calibration clock to
    # estimate where timing closes after synthesis. Used as the anchor for
    # the initial P&R period.
    print(
        f"\nStep 1: synth-only at T={cfg.calibration_period_ns} ns (over-constrained)"
    )
    m1 = run_iteration(
        design,
        batch_dir,
        "synth",
        0,
        cfg.calibration_period_ns,
        cfg,
        SYNTH_FLOW_TARGETS,
        args.num_threads,
    )
    synth_ws_1 = _require_finite(m1, "synth_ws_ns")
    print(f"  synth_ws={synth_ws_1:+.4f} ns")

    # P&R sweep at constant operating pressure. `t_anchor` is the most
    # recent estimate of the period at which post-route timing closes;
    # each pass targets `pressure * t_anchor` and updates `t_anchor` to
    # the period the pass actually achieved (t - ws).
    t_anchor = cfg.calibration_period_ns - synth_ws_1
    pressure = INITIAL_PRESSURE
    pnr_steps: list[PnrStep] = []
    completed: list[PnrStep] = []

    failed_total = 0
    for i in range(PNR_PASSES):
        # Retry at progressively lower pressure until the flow runs or
        # the floor is reached.
        while True:
            t = pressure * t_anchor
            print(
                f"\nP&R pass {i} at T={t:.4f} ns ({fmax_mhz(t):.2f} MHz), "
                f"pressure={pressure:.2f}"
            )
            output_dir = iter_output_dir(design, batch_dir, "pnr", i)
            try:
                m = run_iteration(
                    design,
                    batch_dir,
                    "pnr",
                    i,
                    t,
                    cfg,
                    ALL_FLOW_TARGETS,
                    args.num_threads,
                )
                break
            except RunJobFailed as exc:
                pnr_steps.append(
                    PnrStep(
                        iter=i,
                        period_ns=t,
                        pressure=pressure,
                        output_dir=output_dir,
                        completed=False,
                    )
                )
                failed_total += 1
                print(f"  FAILED: {exc}")
                pressure -= PRESSURE_STEP
                if pressure < PRESSURE_FLOOR:
                    raise RuntimeError(
                        f"calibration gave up: pressure backed off below "
                        f"{PRESSURE_FLOOR} after {failed_total} failures"
                    ) from exc
                print(f"  backing off to pressure={pressure:.2f}")
        ws = _require_finite(m, "route_ws_ns")
        route_area = _require_finite(m, "route_area_um2")
        realised = (t - ws) / t
        step = PnrStep(
            iter=i,
            period_ns=t,
            pressure=pressure,
            output_dir=output_dir,
            completed=True,
            route_ws_ns=ws,
            route_area_um2=route_area,
            realised_pressure=realised,
        )
        completed.append(step)
        pnr_steps.append(step)
        print(
            f"  route_ws={ws:+.4f} ns, route_area={route_area:.1f} um^2, "
            f"realised_pressure={realised:.3f}"
        )
        t_anchor = t - ws

    if not completed:
        raise RuntimeError("no P&R run completed")

    # Pick the completed pass whose realised pressure landed closest to
    # the pressure it was operating at.
    best = min(
        completed,
        key=lambda s: (abs(s.realised_pressure - s.pressure), -s.iter),
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
            "pressure": best.pressure,
            "fmax_mhz": fmax_mhz(best.period_ns),
            "route_ws_ns": best.route_ws_ns,
            "route_area_um2": best.route_area_um2,
            "realised_pressure": best.realised_pressure,
            "selected_iter": best.iter,
        },
        "step1_synth": {
            "period_ns": cfg.calibration_period_ns,
            "synth_ws_ns": synth_ws_1,
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
        f"pressure={best.pressure:.2f} | "
        f"fmax={fmax_mhz(best.period_ns):.2f} MHz | "
        f"realised_pressure={best.realised_pressure:.3f}"
    )
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
