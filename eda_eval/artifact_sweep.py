#!/usr/bin/env python3
"""Run each LLM-eval artifact through the full ORFS flow.

For every `artifacts/llm/<agent>/<design>/epoch_<n>/` produced by the
LLM eval, this script reads the saved `target_config.json` + `diff.patch`,
reflink-copies the on-disk `design.root`, applies the diff at `rtl_dir`,
and submits a `RunConfig` against the copy. Per-job output lands at
`eda_results/<batch>/<agent>/<benchmark>/<name>/<variant>_<epoch>/`.

Usage:
    uv run eda_eval/artifact_sweep.py \
        --parallel-samples 7 --threads-per-run 2
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

from tqdm import tqdm

from common.config import (
    ALL_FLOW_TARGETS,
    ARTIFACTS,
    EDA_RUNS,
    RunConfig,
    TargetConfig,
    _from_json,
)
from common.executor import run_parallel
from eda_eval.run import run_job

ARTIFACT_ROOT = ARTIFACTS / "llm"


def _discover_epochs() -> list[tuple[str, str, str, Path]]:
    """Return list of (agent, design, epoch, epoch_dir)."""
    out: list[tuple[str, str, str, Path]] = []
    for agent_dir in sorted(ARTIFACT_ROOT.iterdir()):
        if not agent_dir.is_dir():
            continue
        for design_dir in sorted(agent_dir.iterdir()):
            if not design_dir.is_dir():
                continue
            for epoch_dir in sorted(design_dir.iterdir()):
                if not epoch_dir.is_dir():
                    continue
                if not (epoch_dir / "target_config.json").is_file():
                    continue
                out.append((agent_dir.name, design_dir.name, epoch_dir.name, epoch_dir))
    return out


def _load_target(epoch_dir: Path) -> TargetConfig:
    blob = json.loads((epoch_dir / "target_config.json").read_text())
    return _from_json(TargetConfig, blob)


def _create_patched_copy(target: TargetConfig, diff: str) -> Path:
    """Reflink-copy `design.root`, apply diff under `rtl_dir`, return copy root."""
    design = target.design
    dest = Path(tempfile.mkdtemp(prefix="artifact_root_")) / design.root.name
    proc = subprocess.run(
        ["cp", "-R", "--reflink=auto", str(design.root), str(dest)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"copy failed: {proc.stderr}")
    if diff.strip():
        rtl_dest = dest / design.rtl_dir
        patch = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(rtl_dest)],
            input=diff,
            text=True,
            capture_output=True,
        )
        if patch.returncode != 0:
            raise RuntimeError(
                f"patch failed:\nSTDOUT: {patch.stdout}\nSTDERR: {patch.stderr}"
            )
    return dest


def _run_one(
    agent: str,
    design_name: str,
    epoch_name: str,
    epoch_dir: Path,
    batch_dir: Path,
    num_threads: int,
) -> tuple[int, str, str]:
    """Apply the patch and run the full ORFS flow. Returns (rc, output_dir, msg)."""
    try:
        target = _load_target(epoch_dir)
        diff = (epoch_dir / "diff.patch").read_text()
        copy_root = _create_patched_copy(target, diff)
        patched_design = dataclasses.replace(target.design, root=copy_root)
        patched_target = dataclasses.replace(target, design=patched_design)
        design = target.design
        output_dir = (
            batch_dir
            / agent
            / design.benchmark
            / design.name
            / f"{design.variant}_{epoch_name}"
        )
        run = RunConfig(
            synth_target=patched_target,
            output_dir=output_dir,
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=num_threads,
        )
        rc = run_job(run)
        return rc, str(output_dir), ""
    except Exception as e:
        tb = traceback.format_exc()
        return 99, str(epoch_dir), f"{type(e).__name__}: {e}\n{tb}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--parallel-samples", type=int, default=7)
    parser.add_argument("--threads-per-run", type=int, default=2)
    args = parser.parse_args()

    epochs = _discover_epochs()
    if not epochs:
        raise SystemExit(f"No epochs found under {ARTIFACT_ROOT}")

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / f"{batch_ts}__artifacts"
    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / "sweep.log"
    print(f"Output dir: {batch_dir}")
    print(f"Log: {log_path}")
    print(
        f"Running {len(epochs)} job(s), "
        f"parallel_samples={args.parallel_samples}, "
        f"threads_per_run={args.threads_per_run}"
    )

    submitted = [
        (
            (agent, design_name, epoch_name),
            (agent, design_name, epoch_name, ep_dir, batch_dir, args.threads_per_run),
        )
        for agent, design_name, epoch_name, ep_dir in epochs
    ]

    failures: list[tuple[str, str, str, int, str]] = []
    with log_path.open("w", buffering=1) as log:
        log.write(
            f"# artifact sweep started {batch_ts} "
            f"parallel={args.parallel_samples} threads={args.threads_per_run}\n"
        )
        log.write(f"# {len(epochs)} jobs to run\n")
        for (agent, design_name, epoch_name), (
            rc,
            out_dir,
            msg,
        ) in run_parallel(
            _run_one,
            submitted,
            max_workers=args.parallel_samples,
            description="artifact sweep",
        ):
            ts = time.strftime("%H:%M:%S")
            tag = f"{agent}/{design_name}/{epoch_name}"
            if rc == 0:
                log.write(f"[{ts}] PASS {tag} -> {out_dir}\n")
                tqdm.write(f"PASS {tag}")
            else:
                log.write(f"[{ts}] FAIL {tag} rc={rc} -> {out_dir}\n")
                if msg:
                    log.write(f"  {msg}\n")
                tqdm.write(f"FAIL {tag} rc={rc}")
                failures.append((agent, design_name, epoch_name, rc, out_dir))

        log.write(f"# done. {len(failures)} failure(s) of {len(epochs)}\n")

    if failures:
        print(f"\n{len(failures)} job(s) failed:")
        for agent, design_name, epoch_name, rc, out_dir in failures:
            print(f"  {agent}/{design_name}/{epoch_name} rc={rc} -> {out_dir}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
