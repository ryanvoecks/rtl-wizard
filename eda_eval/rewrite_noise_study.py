#!/usr/bin/env python3
"""Run every calibrated target through 10 rewriter seeds to measure
RTL-rewrite variance.

For each `TargetConfig` in `common.targets.all_targets`, the design's
RTL is rewritten by `eda_eval.rewriter.rewrite_design` at seeds 0..9
into a per-job tempdir, then driven through the full ORFS flow with
`StudyConfig.seed` pinned to its default so OR_SEED is constant across
runs and the observed variance is attributable to the rewriter. Per-job
output lands at
`eda_results/<batch>/<benchmark>/<name>/<variant>_rewrite_seed_<i>/`, so
the existing `extract_metrics.py` walker picks them up unchanged.

Usage:
    uv run eda_eval/rewrite_noise_study.py --parallel-samples 8
"""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import tempfile
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
from eda_eval.rewriter import rewrite_design
from eda_eval.run import run_job

SEEDS = tuple(range(10))


def _run_one(
    target: TargetConfig,
    seed: int,
    batch_dir: Path,
    num_threads: int,
) -> int:
    """Run one target at one rewriter seed. Returns the ORFS rc."""
    design = target.design
    output_dir = (
        batch_dir
        / design.benchmark
        / design.name
        / f"{design.variant}_rewrite_seed_{seed}"
    )
    tmp = Path(tempfile.mkdtemp(prefix=f"rtl_rewrite_{design.name}_seed{seed}_"))
    try:
        rewritten_root = tmp / design.root.name
        rewritten = rewrite_design(design, rewritten_root, seed=seed)
        seeded = dataclasses.replace(target, design=rewritten)
        run = RunConfig(
            synth_target=seeded,
            output_dir=output_dir,
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=num_threads,
        )
        return run_job(run)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        f"Running {total} jobs ({len(all_targets)} targets x "
        f"{len(SEEDS)} rewriter seeds), "
        f"parallel_samples={args.parallel_samples}, "
        f"threads_per_run={args.threads_per_run}"
    )

    failures: list[tuple[TargetConfig, int]] = []
    with ProcessPoolExecutor(max_workers=args.parallel_samples) as ex:
        futures = {
            ex.submit(_run_one, t, s, batch_dir, args.threads_per_run): (t, s)
            for t, s in jobs
        }
        done = 0
        for fut in as_completed(futures):
            t, s = futures[fut]
            rc = fut.result()
            done += 1
            d = t.design
            tag = f"{d.benchmark}/{d.name}/{d.variant}"
            status = "OK" if rc == 0 else f"FAIL rc={rc}"
            print(f"[{done}/{total}] {tag} rewrite_seed={s}: {status}")
            if rc != 0:
                failures.append((t, s))

    if failures:
        print(f"\n{len(failures)} job(s) failed:")
        for t, s in failures:
            d = t.design
            print(f"  {d.benchmark}/{d.name}/{d.variant} rewrite_seed={s}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
