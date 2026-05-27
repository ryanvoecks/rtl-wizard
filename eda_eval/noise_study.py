#!/usr/bin/env python3
"""Run every calibrated target at seeds 0..9 to measure noise variance.

Each `TargetConfig` in `common.targets.all_targets` is run with its
StudyConfig swapped for one that pins `seed=i`. Per-job output lands at
`eda_results/<batch>/<benchmark>/<name>/<variant>_seed_<i>/`, so the
existing `extract_metrics.py` walker picks them up unchanged.

Usage:
    uv run eda_eval/noise_study.py --parallel-samples 8
"""

from __future__ import annotations

import argparse
import dataclasses
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from common.config import (
    ALL_FLOW_TARGETS,
    EDA_RUNS,
    RunConfig,
    TargetConfig,
)
from common.targets import all_targets
from eda_eval.run import run_job

SEEDS = tuple(range(10))


def _run_one(
    target: TargetConfig,
    seed: int,
    batch_dir: Path,
    num_threads: int,
) -> tuple[str, int, int]:
    """Run one target at one seed. Returns (tag, seed, rc)."""
    design = target.design
    seeded = dataclasses.replace(
        target,
        cfg=dataclasses.replace(target.cfg, seed=seed),
    )
    output_dir = (
        batch_dir / design.benchmark / design.name / f"{design.variant}_seed_{seed}"
    )
    run = RunConfig(
        synth_target=seeded,
        output_dir=output_dir,
        flow_targets=ALL_FLOW_TARGETS,
        num_threads=num_threads,
    )
    rc = run_job(run)
    tag = f"{design.benchmark}/{design.name}/{design.variant}"
    return tag, seed, rc


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--parallel-samples",
        type=int,
        default=1,
        help="Number of concurrent ORFS jobs.",
    )
    parser.add_argument(
        "--threads-per-run",
        type=int,
        default=2,
        help="NUM_CORES exported to each ORFS invocation.",
    )
    args = parser.parse_args()

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {batch_dir}")

    jobs = [(t, s) for t in all_targets for s in SEEDS]
    total = len(jobs)
    print(
        f"Running {total} jobs ({len(all_targets)} targets x {len(SEEDS)} seeds), "
        f"parallel_samples={args.parallel_samples}, "
        f"threads_per_run={args.threads_per_run}"
    )

    failures: list[tuple[str, int]] = []
    with ProcessPoolExecutor(max_workers=args.parallel_samples) as ex:
        futures = {
            ex.submit(_run_one, t, s, batch_dir, args.threads_per_run): (t, s)
            for t, s in jobs
        }
        done = 0
        for fut in as_completed(futures):
            tag, seed, rc = fut.result()
            done += 1
            status = "OK" if rc == 0 else f"FAIL rc={rc}"
            print(f"[{done}/{total}] {tag} seed={seed}: {status}")
            if rc != 0:
                failures.append((tag, seed))

    if failures:
        print(f"\n{len(failures)} job(s) failed:")
        for tag, seed in failures:
            print(f"  {tag} seed={seed}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
