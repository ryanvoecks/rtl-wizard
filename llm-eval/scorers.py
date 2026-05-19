"""Diff-based scorers for llm-eval tasks.

Each scorer diffs the agent's sandbox RTL against on-disk originals, saves
`diff.patch`, and hands `(design, diff)` to a pure evaluator that reapplies
the diff into a tempdir and runs the check — so a saved diff can be replayed.
"""
import asyncio
import difflib
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
from inspect_ai.util import sandbox

from common.config import YOSYS_BIN, DesignConfig, Result
from outputs import sample_output_dir

# Config
YOSYS_TIMEOUT = 60          # Timeout for synthesisability check
SANDBOX_RTL_ROOT = "rtl"    # Root inside sandbox where agent edits design


def _rel(design: DesignConfig, rtl_file: Path) -> Path:
    return rtl_file.relative_to(design.rtl_dir)


def _sandbox_path(design: DesignConfig, rtl_file: Path) -> str:
    return f"{SANDBOX_RTL_ROOT}/{_rel(design, rtl_file)}"


async def _read_sandbox_file(path: str) -> str:
    """Return the file's contents from the sandbox, or '' if it's gone."""
    try:
        return await sandbox().read_file(path)
    except Exception:
        return ""


async def build_diff_from_sandbox(design: DesignConfig) -> str:
    """Unified diff of the agent's edits against `design`'s on-disk originals.
    Output paths are relative to `design.rtl_dir`, so diff applies cleanly."""

    parts: list[str] = []
    for rtl_file in design.rtl_files:
        rel = _rel(design, rtl_file)
        original = rtl_file.read_text()
        final = await _read_sandbox_file(_sandbox_path(design, rtl_file))
        parts.append(
            "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    final.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
        )
    return "".join(parts)


def _apply_diff(diff: str, dest_dir: Path) -> None:
    """Apply a unified diff (paths `a/<rel>` `b/<rel>`) under `dest_dir`."""
    proc = subprocess.run(
        ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(dest_dir)],
        input=diff, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"patch failed:\n{proc.stdout}\n{proc.stderr}")


def _create_copy(design: DesignConfig, diff: str) -> Path:
    """Reflink-copy `design.root` into a fresh tempdir and apply `diff` at
    the rtl_dir location inside the copy. Returns the path to the copy."""
    dest = Path(tempfile.mkdtemp()) / design.root.name
    proc = subprocess.run(
        ["cp", "-R", "--reflink=auto", str(design.root), str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"copy failed:\n{proc.stderr}")
    _apply_diff(diff, dest / design.rtl_dir.relative_to(design.root))
    return dest


def evaluate_synthesis(design: DesignConfig, diff: str) -> Result:
    """Apply `diff` to a shallow copy of `design.rtl_dir` and run yosys
    `hierarchy -check -top <top>; proc; opt; clean`. Returns (log, rc):
    rc == 0 on success, non-zero (reason in `log`) on any failure."""
    try:
        root = _create_copy(design, diff)
        rtl_path = root / design.rtl_dir.relative_to(design.root)
        rel_paths = [str(_rel(design, f)) for f in design.rtl_files]
        script = (
            f"read_verilog -sv {' '.join(rel_paths)}; "
            f"hierarchy -check -top {design.top_module}; proc; opt; clean"
        )
        proc = subprocess.run(
            [YOSYS_BIN, "-q", "-p", script],
            cwd=rtl_path, capture_output=True, text=True,
            timeout=YOSYS_TIMEOUT,
        )
    except RuntimeError as e:
        return str(e), 1
    except subprocess.TimeoutExpired:
        return f"yosys timed out after {YOSYS_TIMEOUT}s", 124

    return proc.stdout + proc.stderr, proc.returncode


def evaluate_testbench(design: DesignConfig, diff: str) -> Result:
    """Apply `diff` into a copy of `design.root` and run the upstream
    testbench. Returns (stdout, rc). Never raises."""
    try:
        repo_copy = _create_copy(design, diff)
        return design.run_tb(repo_copy)
    except RuntimeError as e:
        return str(e), 1


async def _save_diff(state: TaskState, design: DesignConfig) -> str:
    diff = await build_diff_from_sandbox(design)
    out_dir = sample_output_dir(str(state.sample_id))
    (out_dir / "diff.patch").write_text(diff)
    return diff


@scorer(metrics=[accuracy()])
def synthesis() -> Scorer:
    """Cheap synthesisability check for an arbitrary RTL design."""

    async def score(state: TaskState, target: Target) -> Score:
        design = state.metadata.get("design")
        diff = await _save_diff(state, design)
        log, rc = await asyncio.to_thread(evaluate_synthesis, design, diff)

        out_dir = sample_output_dir(str(state.sample_id))
        (out_dir / "synthesis.log").write_text(log)
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
def testbench() -> Scorer:
    """Run the design's upstream testbench against the agent's RTL."""

    async def score(state: TaskState, target: Target) -> Score:
        design = state.metadata.get("design")
        diff = await _save_diff(state, design)
        log, rc = await asyncio.to_thread(evaluate_testbench, design, diff)

        out_dir = sample_output_dir(str(state.sample_id))
        (out_dir / "testbench.log").write_text(log)
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
