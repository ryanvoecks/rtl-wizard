#!/usr/bin/env python3
"""Iterative k-based calibration: converge on the clock period that
satisfies a target worst-slack-to-period ratio `k = W / T`.

Each iteration drives the full ORFS synth + P&R flow at a candidate
period `t_i`, reads post-route worst slack `w_i`, then computes the next
candidate using the fixed-point recurrence

    t_{i+1} = (t_i - w_i) / (1 - k)

After `n` iterations, the next-computed period is reported as the
converged value. Existing single-shot calibration in `run.py` is left
intact -- this is a parallel method, not a replacement.

Usage:
    uv run eda_eval/calibrate.py
    uv run eda_eval/calibrate.py --benchmark corpus --name counter_array \\
        --variant claude --k -0.5 --initial-period-ns 1.0 --iterations 3
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

from common.config import (
    EDA_RUNS,
    ORFS_HOME,
    DesignConfig,
    RunConfig,
    StudyConfig,
)
from common.loader import AllDesigns
from extract_metrics import extract
from run import run_job

ITER_CAL_DIR = "__iter_calibration__"


def iter_output_dir(design: DesignConfig, batch_dir: Path, i: int) -> Path:
    """Per-iteration phase dir: shares the same shape as the existing
    `__calibration__` siblings so `extract` and downstream tooling work
    unchanged."""
    return batch_dir / ITER_CAL_DIR / design.benchmark / design.name / f"iter_{i}"


def resolve_design(benchmark: str, name: str, variant: str) -> DesignConfig:
    """Locate the single (benchmark, name, variant) design across all loaders."""
    try:
        return AllDesigns.designs()[benchmark][name][variant]
    except KeyError as exc:
        raise ValueError(
            f"no design found for benchmark={benchmark!r} name={name!r} "
            f"variant={variant!r}"
        ) from exc


def run_iteration(
    design: DesignConfig,
    batch_dir: Path,
    i: int,
    period_ns: float,
    side_um: float,
    cfg: StudyConfig,
) -> tuple[RunConfig, float]:
    """Single ORFS pass at `period_ns`. Returns the run plus its
    post-route worst slack in ns."""
    run = RunConfig(
        design=design,
        output_dir=iter_output_dir(design, batch_dir, i),
        period_ns=period_ns,
        side_um=side_um,
        cfg=cfg,
    )
    rc = run_job(run)
    if rc != 0:
        raise RuntimeError(
            f"iter {i} ORFS run failed (rc={rc}); see {run.output_dir}/flow.log"
        )
    metrics = extract(run.output_dir, design.top_module)
    ws = metrics["route_ws_ns"]
    if math.isnan(ws):
        raise RuntimeError(f"iter {i} produced NaN route_ws_ns")
    return run, ws


def iterate(
    design: DesignConfig,
    cfg: StudyConfig,
    k: float,
    t0: float,
    n: int,
    side_um: float,
    batch_dir: Path,
) -> tuple[list[dict], float]:
    """Run `n` calibration iterations. Returns per-iter rows and the
    converged period (the `t_next` computed after the final iteration)."""
    t = t0
    rows: list[dict] = []
    for i in range(n):
        print(f"[iter {i}] running at period={t:.4f} ns, side={side_um:.1f} um ...")
        _, ws = run_iteration(design, batch_dir, i, t, side_um, cfg)
        t_next = (t - ws) / (1.0 - k)
        rows.append({"iter": i, "t_in": t, "ws": ws, "t_next": t_next})
        print(f"[iter {i}] ws={ws:+.4f} ns -> t_next={t_next:.4f} ns")
        t = t_next
    return rows, t


def print_table(rows: list[dict], final_period_ns: float) -> None:
    header = f"{'iter':>4} | {'t_in (ns)':>10} | {'ws (ns)':>10} | {'t_next (ns)':>12}"
    print()
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['iter']:>4} | {r['t_in']:>10.4f} | {r['ws']:>+10.4f} | "
            f"{r['t_next']:>12.4f}"
        )
    print()
    print(f"Converged period: {final_period_ns:.4f} ns")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--benchmark", default="corpus")
    parser.add_argument("--name", default="counter_array")
    parser.add_argument("--variant", default="claude")
    parser.add_argument(
        "--k", type=float, default=-0.5, help="Target worst-slack / period ratio."
    )
    parser.add_argument("--initial-period-ns", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--die-side-um", type=float, default=1000.0)
    args = parser.parse_args()

    if not (ORFS_HOME / "Makefile").is_file():
        raise FileNotFoundError("ORFS flow not found - set config.ORFS_HOME")

    cfg = StudyConfig()
    design = resolve_design(args.benchmark, args.name, args.variant)

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {batch_dir}")
    print(
        f"Calibrating {design.benchmark}/{design.name}/{design.variant} "
        f"(top={design.top_module}) for k={args.k} over "
        f"{args.iterations} iter(s) starting at {args.initial_period_ns} ns"
    )

    rows, final_period = iterate(
        design=design,
        cfg=cfg,
        k=args.k,
        t0=args.initial_period_ns,
        n=args.iterations,
        side_um=args.die_side_um,
        batch_dir=batch_dir,
    )
    print_table(rows, final_period)


if __name__ == "__main__":
    main()
