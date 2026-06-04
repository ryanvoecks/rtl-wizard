#!/usr/bin/env python3
"""Calibrate a design's clock period via a synth + P&R sweep at
U=`cfg.target_utilization` (ORFS auto-sizes the die). Writes
`calibration.json` with the chosen period and the per-iteration log.

Step 1 is an anchor pass: full P&R at the relaxed `cfg.anchor_period_ns`
(default 10 ns) so the design routes cleanly and the post-route closure
period becomes the calibrator's ground-truth anchor. If this anchor
pass fails, it is treated as a real error (no backoff).

The pressure sweep then targets a constant `INITIAL_PRESSURE` ratio of
post-route closure period to clock period, anchored on the realised
P&R closure from step 1. If the ORFS flow fails at the current
pressure, pressure is backed off by `PRESSURE_STEP` (1.5 -> 1.4 -> ...
-> 1.0) and retried; falling below `PRESSURE_FLOOR` gives up.

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
    DesignConfig,
    RunConfig,
    StudyConfig,
    TargetConfig,
)
from common.designs import resolve_design
from eda_eval.extract_metrics import extract
from eda_eval.run import run_job

Phase = Literal["anchor", "pnr"]

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
    return extract(output_dir, design.top_module)


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

    # Step 1: full P&R at the relaxed anchor period. Gives a real,
    # post-route closure number to anchor the pressure sweep on (rather
    # than a synth-only estimate that systematically under-predicts
    # post-route delay). A failure here is a real error, not a
    # calibration backoff condition.
    print(
        f"\nStep 1: anchor P&R at T={cfg.anchor_period_ns} ns (relaxed)"
    )
    m1 = run_iteration(
        design,
        batch_dir,
        "anchor",
        0,
        cfg.anchor_period_ns,
        cfg,
        ALL_FLOW_TARGETS,
        args.num_threads,
    )
    anchor_ws = _require_finite(m1, "route_ws_ns")
    anchor_closure = cfg.anchor_period_ns - anchor_ws
    print(
        f"  route_ws={anchor_ws:+.4f} ns -> closure={anchor_closure:.4f} ns"
    )

    # P&R sweep at constant operating pressure. `t_anchor` is the most
    # recent estimate of the period at which post-route timing closes;
    # each pass targets `t_anchor / pressure` (so realised closure/T
    # converges to `pressure`) and updates `t_anchor` to the period the
    # pass actually achieved (t - ws).
    t_anchor = anchor_closure
    pressure = INITIAL_PRESSURE
    pnr_steps: list[PnrStep] = []
    completed: list[PnrStep] = []

    failed_total = 0
    for i in range(PNR_PASSES):
        # Retry at progressively lower pressure until the flow runs or
        # the floor is reached.
        while True:
            t = t_anchor / pressure
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
                # Round to one decimal: keeps the ladder on the nominal
                # 1.5 -> 1.4 -> ... -> 1.0 grid instead of drifting onto
                # 0.9999... after a few subtractions.
                pressure = round(pressure - PRESSURE_STEP, 1)
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
        "step1_anchor": {
            "period_ns": cfg.anchor_period_ns,
            "route_ws_ns": anchor_ws,
            "closure_ns": anchor_closure,
            "output_dir": str(iter_output_dir(design, batch_dir, "anchor", 0)),
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
