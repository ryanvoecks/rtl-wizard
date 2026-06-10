#!/usr/bin/env python3
"""Pick a calibrated clock period and utilisation for one design.

Starts at a tight anchor period and doubles until P&R completes, then
sweeps tighter periods targeting a constant closure/T pressure ratio.
Backs off utilization on IO-perimeter overflow and pressure on other
flow failures. Writes `calibration.json`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
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
PRESSURE_STEP = 0.10  # Pressure backoff per non-IO flow failure
PRESSURE_FLOOR = 1.0  # Below this, the calibrator gives up
UTIL_STEP = 10  # Utilization (percent) backoff per IO-pin overflow failure
UTIL_FLOOR = 30  # Below this, the calibrator gives up on IO-bound designs

# Hard fail from OpenROAD's IO placer when die perimeter can't fit IO pins
PPL_IO_OVERFLOW = re.compile(r"\[ERROR PPL-0024\]")
# Hard fail from OpenROAD's global router when nets can't be routed
GRT_CONGESTION = re.compile(r"\[ERROR GRT-0116\]")


def _is_io_pin_overflow(output_dir: Path) -> bool:
    """True if `flow.log` contains the PPL-0024 IO-overflow error."""
    log = output_dir / "flow.log"
    if not log.exists():
        return False
    return bool(PPL_IO_OVERFLOW.search(log.read_text(errors="replace")))


def _is_density_failure(output_dir: Path) -> bool:
    """True if `flow.log` shows a failure that lowering utilization can
    fix: IO pin overflow (PPL-0024) or global-route congestion (GRT-0116)."""
    log = output_dir / "flow.log"
    if not log.exists():
        return False
    text = log.read_text(errors="replace")
    return bool(PPL_IO_OVERFLOW.search(text) or GRT_CONGESTION.search(text))


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
    target_utilization: float,
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
            target_utilization=target_utilization,
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
    util = float(TargetConfig.target_utilization)

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {batch_dir}")
    print(
        f"Calibrating {design.benchmark}/{design.name}/{design.variant} "
        f"(top={design.top_module}, U={util:.0f})"
    )

    # Step 1: find a period at which P&R completes. Start tight at
    # `cfg.anchor_period_ns` and double on any non-IO failure; back off
    # utilization on IO-pin overflow. The first passing pass becomes the
    # anchor that the constant-pressure sweep zooms in on.
    print(
        f"\nStep 1: anchor P&R starting at T={cfg.anchor_period_ns} ns "
        f"(double on failure until pass)"
    )
    period = cfg.anchor_period_ns
    anchor_attempts: list[dict] = []
    attempt = 0
    while True:
        print(f"  attempt {attempt}: T={period:.4f} ns, U={util:.0f}")
        anchor_dir = iter_output_dir(design, batch_dir, "anchor", attempt)
        try:
            m1 = run_iteration(
                design,
                batch_dir,
                "anchor",
                attempt,
                period,
                util,
                cfg,
                ALL_FLOW_TARGETS,
                args.num_threads,
            )
            break
        except RunJobFailed as exc:
            anchor_attempts.append(
                {
                    "attempt": attempt,
                    "period_ns": period,
                    "target_utilization": util,
                    "output_dir": str(anchor_dir),
                }
            )
            if _is_density_failure(anchor_dir):
                print(f"  FAILED (density: PPL-0024/GRT-0116) at U={util:.0f}")
                util = round(util - UTIL_STEP)
                if util < UTIL_FLOOR:
                    raise RuntimeError(
                        f"calibration gave up: util backed off below "
                        f"{UTIL_FLOOR} (design still too dense)"
                    ) from exc
                print(f"  reducing utilization to U={util:.0f}")
            else:
                print(f"  FAILED at T={period:.4f} ns")
                period *= 2
                print(f"  doubling period to T={period:.4f} ns")
        attempt += 1
    anchor_period = period
    anchor_ws = _require_finite(m1, "route_ws_ns")
    anchor_closure = anchor_period - anchor_ws
    print(
        f"  route_ws={anchor_ws:+.4f} ns -> closure={anchor_closure:.4f} ns "
        f"(T={anchor_period:.4f} ns, U={util:.0f})"
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
        # Two backoffs, classified by parsing the flow log: density
        # errors (PPL-0024 IO overflow / GRT-0116 congestion) -> lower
        # util (and persist it); other failures -> lower pressure.
        while True:
            t = t_anchor / pressure
            print(
                f"\nP&R pass {i} at T={t:.4f} ns ({fmax_mhz(t):.2f} MHz), "
                f"pressure={pressure:.2f}, U={util:.0f}"
            )
            output_dir = iter_output_dir(design, batch_dir, "pnr", i)
            try:
                m = run_iteration(
                    design,
                    batch_dir,
                    "pnr",
                    i,
                    t,
                    util,
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
                if _is_density_failure(output_dir):
                    util = round(util - UTIL_STEP)
                    if util < UTIL_FLOOR:
                        raise RuntimeError(
                            f"calibration gave up: util backed off below "
                            f"{UTIL_FLOOR} after {failed_total} failures"
                        ) from exc
                    print(f"  reducing utilization to U={util:.0f}")
                else:
                    # Round to one decimal: keeps the ladder on the
                    # nominal 1.5 -> 1.4 -> ... -> 1.0 grid instead of
                    # drifting onto 0.9999... after a few subtractions.
                    pressure = round(pressure - PRESSURE_STEP, 1)
                    if pressure < PRESSURE_FLOOR:
                        raise RuntimeError(
                            f"calibration gave up: pressure backed off "
                            f"below {PRESSURE_FLOOR} after "
                            f"{failed_total} failures"
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
            "target_utilization": util,
            "selected_iter": best.iter,
        },
        "step1_anchor": {
            "start_period_ns": cfg.anchor_period_ns,
            "period_ns": anchor_period,
            "route_ws_ns": anchor_ws,
            "closure_ns": anchor_closure,
            "output_dir": str(iter_output_dir(design, batch_dir, "anchor", attempt)),
            "failed_attempts": anchor_attempts,
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
        f"U={util:.0f} | "
        f"fmax={fmax_mhz(best.period_ns):.2f} MHz | "
        f"realised_pressure={best.realised_pressure:.3f}"
    )
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
