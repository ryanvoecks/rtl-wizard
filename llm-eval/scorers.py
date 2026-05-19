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
    cleanly with `patch -p1` against either a copy of `rtl_dir` itself or a
    `tb_repo_root` copy patched at `rtl_dir.relative_to(tb_repo_root)`."""
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


def _stage_rtl(design: DesignConfig, dest_dir: Path) -> None:
    """Copy the design's original RTL into `dest_dir`, preserving each
    file's path relative to `rtl_dir`."""
    for rtl_file in design.rtl_files:
        dest = dest_dir / _rel(design, rtl_file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rtl_file, dest)


def _resolve_yosys() -> str | None:
    path = shutil.which("yosys")
    if path:
        return path
    if Path(_DEVCONTAINER_YOSYS).exists():
        return _DEVCONTAINER_YOSYS
    return None


def evaluate_synthesisable(design: DesignConfig, diff: str) -> tuple[bool, str]:
    """Stage `design.rtl_files` into a tempdir, apply `diff`, and run yosys
    `hierarchy -check -top <top>; proc; opt; clean`. Returns (ok, message).

    Pure function modulo filesystem + subprocess — no TaskState, no sandbox,
    no Inspect imports. Drives the Inspect scorer and can also be called
    from a replay tool with a saved diff.patch."""
    yosys = _resolve_yosys()
    if yosys is None:
        return False, (
            "yosys not found on host (PATH or "
            f"{_DEVCONTAINER_YOSYS}); cannot run synthesisability check"
        )
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        _stage_rtl(design, td_path)
        try:
            _apply_diff(diff, td_path)
        except RuntimeError as e:
            return False, str(e)

        rel_paths = [str(_rel(design, f)) for f in design.rtl_files]
        existing = [p for p in rel_paths if (td_path / p).is_file()]
        if not existing:
            return False, "diff removed every RTL file in the design"
        script = (
            f"read_verilog -sv {' '.join(existing)}; "
            f"hierarchy -check -top {design.top_module}; proc; opt; clean"
        )
        try:
            proc = subprocess.run(
                [yosys, "-q", "-p", script],
                cwd=td_path, capture_output=True, text=True,
                timeout=_YOSYS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return False, f"yosys timed out after {_YOSYS_TIMEOUT_S}s"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-1000:]
        return False, f"yosys exited rc={proc.returncode}:\n{tail}"
    return True, ""


def evaluate_functional(design: DesignConfig, diff: str) -> tuple[str, int]:
    """Apply `diff` into a copy of `design.tb_repo_root` and run the
    upstream testbench. Returns (stdout, rc); rc == 0 iff the testbench
    passed, non-zero (with the failure reason in `stdout`) for any setup
    error — missing harness, missing repo, patch failure, etc. The
    function never raises so the caller can treat the return value as
    the single source of truth."""
    if design.tb_repo_root is None or design.run_tb is None:
        return f"design {design.name} has no testbench harness", 1
    if not design.tb_repo_root.exists():
        return (
            f"{design.tb_repo_root} not found — run "
            "`git submodule update --init` to fetch the upstream repo",
            1,
        )
    rtl_dir_rel = design.rtl_dir.relative_to(design.tb_repo_root)

    with tempfile.TemporaryDirectory() as td:
        repo_copy = Path(td) / design.tb_repo_root.name
        # Exclude .git (it's a submodule pointer file) and any stale build
        # artifacts so the copy is a clean tree to build against.
        shutil.copytree(
            design.tb_repo_root,
            repo_copy,
            ignore=shutil.ignore_patterns(".git", "*.sim", "*.vcd"),
        )
        try:
            _apply_diff(diff, repo_copy / rtl_dir_rel)
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
        ok, msg = await asyncio.to_thread(evaluate_synthesisable, design, diff)
        if ok:
            return Score(value=CORRECT)
        else:
            return Score(value=INCORRECT, explanation=msg)

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
        if rc == 0:
            return Score(value=CORRECT, answer="testbench passed")
        else:
            return Score(value=INCORRECT, explanation=f"testbench failed")

    return score
