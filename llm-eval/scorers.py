"""Diff-based scorers for llm-eval tasks.

Each scorer pulls the agent's final RTL files out of the sandbox, computes a
unified diff against the originals (using paths relative to `design.rtl_dir`),
saves the diff as `diff.patch` under the run's per-sample output dir, then
hands `(design, diff)` to a pure-function evaluator that reconstructs the
modified RTL tree on disk (via `patch`) and runs the actual check.

That split is deliberate: the pure evaluators (`evaluate_synthesisable`,
`evaluate_functional`) only need a `DesignConfig` and a diff string, so the
same correctness check can be replayed later from a saved `diff.patch`
without re-running the agent. The Inspect wrappers exist only to bridge
between the sandbox + TaskState and that pure interface.
"""
import asyncio
import difflib
import shutil
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

from common.config import DesignConfig
from outputs import sample_output_dir

# Devcontainer's ORFS-bundled yosys, used as a fallback when `yosys` isn't on
# PATH. The lightweight llm-eval sandbox image deliberately doesn't ship
# yosys, so this scorer is host-side; the devcontainer's Dockerfile installs
# the EDA stack at this prefix.
_DEVCONTAINER_YOSYS = "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"
_YOSYS_TIMEOUT_S = 60

# Root inside the sandbox where the agent edits the design. tasks.py mirrors
# `rtl_dir`'s structure under this prefix, so every RTL file lives at
# `<SANDBOX_RTL_ROOT>/<file.relative_to(design.rtl_dir)>`.
SANDBOX_RTL_ROOT = "rtl"


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

    Output paths are relative to `design.rtl_dir`, so the diff applies
    cleanly with `patch -p1` against a copy of `design.root` patched at
    `rtl_dir.relative_to(root)`."""
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
    the rtl_dir location inside the copy. Returns the path to the copy
    root, ready to run yosys / testbench commands against. Raises
    RuntimeError on copy or patch failure."""
    dest = Path(tempfile.mkdtemp()) / design.root.name
    proc = subprocess.run(
        ["cp", "-R", "--reflink=auto", str(design.root), str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"copy failed:\n{proc.stderr}")
    _apply_diff(diff, dest / design.rtl_dir.relative_to(design.root))
    return dest


def _resolve_yosys() -> str | None:
    path = shutil.which("yosys")
    if path:
        return path
    if Path(_DEVCONTAINER_YOSYS).exists():
        return _DEVCONTAINER_YOSYS
    return None


def evaluate_synthesisable(design: DesignConfig, diff: str) -> tuple[str, int]:
    """Apply `diff` to a shallow copy of `design.rtl_dir` and run yosys
    `hierarchy -check -top <top>; proc; opt; clean`. Returns (log, rc):
    rc == 0 on success, non-zero (reason in `log`) on any failure."""
    yosys = _resolve_yosys()
    if yosys is None:
        return (
            "yosys not found on host (PATH or "
            f"{_DEVCONTAINER_YOSYS}); cannot run synthesisability check",
            1,
        )
    try:
        root = _create_copy(design, diff)
    except RuntimeError as e:
        return str(e), 1
    rtl_path = root / design.rtl_dir.relative_to(design.root)
    rel_paths = [str(_rel(design, f)) for f in design.rtl_files]
    existing = [p for p in rel_paths if (rtl_path / p).is_file()]
    if not existing:
        return "diff removed every RTL file in the design", 1
    script = (
        f"read_verilog -sv {' '.join(existing)}; "
        f"hierarchy -check -top {design.top_module}; proc; opt; clean"
    )
    try:
        proc = subprocess.run(
            [yosys, "-q", "-p", script],
            cwd=rtl_path, capture_output=True, text=True,
            timeout=_YOSYS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"yosys timed out after {_YOSYS_TIMEOUT_S}s", 124

    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def evaluate_functional(design: DesignConfig, diff: str) -> tuple[str, int]:
    """Apply `diff` into a copy of `design.root` and run the upstream
    testbench. Returns (stdout, rc); rc == 0 iff the testbench passed,
    non-zero (with the failure reason in `stdout`) for any setup error
    — missing harness, missing repo, patch failure, etc. Never raises."""
    if design.run_tb is None:
        return f"design {design.name} has no testbench harness", 1
    if not design.root.exists():
        return (
            f"{design.root} not found — run "
            "`git submodule update --init` to fetch the upstream repo",
            1,
        )
    try:
        repo_copy = _create_copy(design, diff)
    except RuntimeError as e:
        return str(e), 1
    return design.run_tb(repo_copy)


async def _save_diff(state: TaskState, design: DesignConfig) -> str:
    diff = await build_diff_from_sandbox(design)
    out_dir = sample_output_dir(str(state.sample_id))
    (out_dir / "diff.patch").write_text(diff)
    return diff


@scorer(metrics=[accuracy()])
def synthesisable() -> Scorer:
    """Cheap synthesisability check for an arbitrary RTL design."""

    async def score(state: TaskState, target: Target) -> Score:
        design = state.metadata.get("design")
        diff = await _save_diff(state, design)
        log, rc = await asyncio.to_thread(evaluate_synthesisable, design, diff)

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
def functional() -> Scorer:
    """Run the design's upstream testbench against the agent's RTL."""

    async def score(state: TaskState, target: Target) -> Score:
        design = state.metadata.get("design")
        diff = await _save_diff(state, design)
        log, rc = await asyncio.to_thread(evaluate_functional, design, diff)

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
