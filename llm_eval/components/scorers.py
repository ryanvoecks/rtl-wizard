"""Diff-based scorers for llm_eval tasks.

The solver captures the agent's final RTL state into a unified diff in the
sample's `store()` (`rtl_diff`); these scorers re-apply that diff against a
fresh copy of `design.root` and run the check. Lets a saved diff be
replayed without any sandbox/container state.
"""

import asyncio
import dataclasses
import subprocess
import tempfile
from pathlib import Path

from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    scorer,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import store

from common.config import (
    YOSYS_BIN,
    DesignConfig,
    Result,
    TargetConfig,
)

# Config
YOSYS_TIMEOUT = 60  # Timeout for synthesisability check


def _apply_diff(diff: str, dest_dir: Path) -> None:
    """Apply a unified diff (paths `a/<rel>` `b/<rel>`) under `dest_dir`."""
    proc = subprocess.run(
        ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(dest_dir)],
        input=diff,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"patch failed:\n{proc.stdout}\n{proc.stderr}")


def _create_copy(design: DesignConfig, diff: str) -> Path:
    """Reflink-copy `design.root` into a fresh tempdir and apply `diff` at
    the rtl_dir location inside the copy. Returns the path to the copy."""
    dest = Path(tempfile.mkdtemp()) / design.root.name
    proc = subprocess.run(
        ["cp", "-R", "--reflink=auto", str(design.root), str(dest)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"copy failed:\n{proc.stderr}")
    _apply_diff(diff, dest / design.rtl_dir)
    return dest


def evaluate_synthesis(design: DesignConfig, diff: str) -> Result:
    """Apply `diff` to a shallow copy of `design.rtl_dir` and run yosys
    `hierarchy -check -top <top>; proc; opt; clean`. Returns (log, rc):
    rc == 0 on success, non-zero (reason in `log`) on any failure."""
    try:
        root = _create_copy(design, diff)
        rtl_path = root / design.rtl_dir
        rel_paths = [str(f) for f in design.rtl_files]
        script = (
            f"read_verilog -sv {' '.join(rel_paths)}; "
            f"hierarchy -check -top {design.top_module}; proc; opt; clean"
        )
        proc = subprocess.run(
            [YOSYS_BIN, "-q", "-p", script],
            cwd=rtl_path,
            capture_output=True,
            text=True,
            timeout=YOSYS_TIMEOUT,
        )
    except RuntimeError as e:
        return str(e), 1
    except subprocess.TimeoutExpired:
        return f"yosys timed out after {YOSYS_TIMEOUT}s", 124

    return proc.stdout + proc.stderr, proc.returncode


def evaluate_testbench(design: DesignConfig, diff: str) -> Result:
    """Apply `diff` into a copy of `design.root` and run the upstream
    testbench against that copy. Returns (stdout, rc). Never raises."""
    try:
        repo_copy = _create_copy(design, diff)
        return dataclasses.replace(design, root=repo_copy).run_tb()
    except RuntimeError as e:
        return str(e), 1


def _sample_dir(output_dir: Path, state: TaskState) -> Path:
    p = output_dir / str(state.sample_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _stored_diff() -> str:
    """Diff captured by the solver before its ClaudeEnv was torn down."""
    return store().get("rtl_diff") or ""


@scorer(metrics=[accuracy()])
def synthesis(output_dir: Path) -> Scorer:
    """Cheap synthesisability check for an arbitrary RTL design."""

    async def score(state: TaskState, target: Target) -> Score:
        synth_target = state.metadata["synth_target"]
        assert isinstance(synth_target, TargetConfig)
        design = synth_target.design
        sample_dir = _sample_dir(output_dir, state)
        synth_target.dump(sample_dir / TargetConfig.FILENAME)
        diff = _stored_diff()
        (sample_dir / "diff.patch").write_text(diff)
        log, rc = await asyncio.to_thread(evaluate_synthesis, design, diff)
        (sample_dir / "synthesis.log").write_text(log)

        artifacts = {"diff": diff, "log": log}
        if rc == 0:
            return Score(value=CORRECT, metadata=artifacts)
        else:
            return Score(
                value=INCORRECT,
                explanation="synthesis failed",
                metadata=artifacts,
            )

    return score


@scorer(metrics=[accuracy()])
def testbench(output_dir: Path) -> Scorer:
    """Run the design's upstream testbench against the agent's RTL."""

    async def score(state: TaskState, target: Target) -> Score:
        synth_target = state.metadata["synth_target"]
        assert isinstance(synth_target, TargetConfig)
        design = synth_target.design
        sample_dir = _sample_dir(output_dir, state)
        synth_target.dump(sample_dir / TargetConfig.FILENAME)
        diff = _stored_diff()
        (sample_dir / "diff.patch").write_text(diff)
        log, rc = await asyncio.to_thread(evaluate_testbench, design, diff)
        (sample_dir / "testbench.log").write_text(log)

        artifacts = {"diff": diff, "log": log}
        if rc == 0:
            return Score(value=CORRECT, metadata=artifacts)
        else:
            return Score(
                value=INCORRECT,
                explanation="testbench failed",
                metadata=artifacts,
            )

    return score
