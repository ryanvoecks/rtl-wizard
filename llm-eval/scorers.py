"""Scorers for llm-eval tasks.

All scorers share the diff-persistence pattern: before the correctness check
they read each file in `metadata["original_files"]` back out of the sandbox,
compute a unified diff against the host-side original, and write a single
`diff.patch` into `llm-results/<RUN_TIMESTAMP>/<sample_id>/`. This lands on disk
even when the actual check fails, so a broken run is still debuggable.

The full agent transcript (messages, tool calls, tool results, usage) is
captured in `state.messages` / `state.output` by the solver and lands in the
run's `.eval` log under the same per-run dir.
"""
import asyncio
import difflib
import shutil
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
    stderr,
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


async def _read_final(filename: str) -> str:
    """Return the agent's final version of `filename` from the sandbox, or
    empty string if the file is gone (deleted/renamed by the agent)."""
    try:
        return await sandbox().read_file(filename)
    except Exception:
        return ""


def _file_diff(filename: str, original: str, final: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            final.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


async def _save_artifacts(state: TaskState) -> None:
    out_dir = sample_output_dir(str(state.sample_id))
    original_files = state.metadata.get("original_files", {})
    diff_parts: list[str] = []
    for fname, original in original_files.items():
        final = await _read_final(fname)
        diff_parts.append(_file_diff(fname, original, final))
    (out_dir / "diff.patch").write_text("".join(diff_parts))


def _resolve_yosys() -> str | None:
    path = shutil.which("yosys")
    if path:
        return path
    if Path(_DEVCONTAINER_YOSYS).exists():
        return _DEVCONTAINER_YOSYS
    return None


@scorer(metrics=[accuracy(), stderr()])
def yosys_synthesisable(design: DesignConfig) -> Scorer:
    """Cheap synthesisability check for an arbitrary RTL design.

    Pulls the agent's final `rtl/*.v` out of the sandbox to a host temp dir
    and runs host yosys through
    `hierarchy -check -top <design.top_module>; proc; opt; clean`.
    No techmap, no abc — we only care that the RTL still elaborates. CORRECT
    iff yosys exits 0 within `_YOSYS_TIMEOUT_S`.
    """
    async def score(state: TaskState, target: Target) -> Score:
        await _save_artifacts(state)

        rtl_paths = list(state.metadata.get("original_files", {}).keys())
        if not rtl_paths:
            return Score(value=INCORRECT, explanation="no original_files in metadata")

        yosys = _resolve_yosys()
        if yosys is None:
            return Score(
                value=INCORRECT,
                explanation=(
                    "yosys not found on host (PATH or "
                    f"{_DEVCONTAINER_YOSYS}); cannot run synthesisability check"
                ),
            )

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            for sandbox_path in rtl_paths:
                dest = td_path / sandbox_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(await _read_final(sandbox_path))

            script = (
                f"read_verilog -sv {' '.join(rtl_paths)}; "
                f"hierarchy -check -top {design.top_module}; proc; opt; clean"
            )
            proc = await asyncio.create_subprocess_exec(
                yosys, "-q", "-p", script,
                cwd=td_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), _YOSYS_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return Score(
                    value=INCORRECT,
                    explanation=f"yosys timed out after {_YOSYS_TIMEOUT_S}s",
                )

        if proc.returncode != 0:
            tail = (stderr_b or stdout_b).decode(errors="replace")[-1000:]
            return Score(
                value=INCORRECT,
                explanation=f"yosys exited rc={proc.returncode}:\n{tail}",
            )
        return Score(value=CORRECT)

    return score


def _rtl_overlay_targets(design: DesignConfig) -> dict[str, Path]:
    """Map each sandbox path (`rtl/<basename>`) to the repo-relative
    destination under `design.tb_repo_root` where the agent's modified file
    should be written before running the testbench."""
    if design.tb_repo_root is None:
        return {}
    overlay: dict[str, Path] = {}
    for rtl_file in design.rtl_files:
        sandbox_path = f"rtl/{rtl_file.name}"
        overlay[sandbox_path] = rtl_file.relative_to(design.tb_repo_root)
    return overlay


@scorer(metrics=[accuracy(), stderr()])
def testbench_passes(design: DesignConfig) -> Scorer:
    """Run the design's upstream testbench against the agent's RTL.

    Copies `design.tb_repo_root` to a tempdir, overlays the agent's final
    `rtl/*.v` onto each file's repo-relative location, then calls
    `design.run_tb(repo_copy)` and grades on its returncode (0 == pass). The
    full testbench stdout is saved to
    `llm-results/<RUN_TIMESTAMP>/<sample_id>/testbench.log` for debugging.
    """
    if design.tb_repo_root is None or design.run_tb is None:
        raise ValueError(
            f"design {design.name} has no testbench harness configured "
            "(tb_repo_root / run_tb are None) — don't register this scorer"
        )
    overlay_map = _rtl_overlay_targets(design)
    run_tb = design.run_tb

    async def score(state: TaskState, target: Target) -> Score:
        await _save_artifacts(state)
        out_dir = sample_output_dir(str(state.sample_id))

        rtl_paths = list(state.metadata.get("original_files", {}).keys())
        if not rtl_paths:
            return Score(value=INCORRECT, explanation="no original_files in metadata")
        if not design.tb_repo_root.exists():
            return Score(
                value=INCORRECT,
                explanation=(
                    f"{design.tb_repo_root} not found — run "
                    "`git submodule update --init` to fetch the upstream repo"
                ),
            )

        with tempfile.TemporaryDirectory() as td:
            repo_copy = Path(td) / design.tb_repo_root.name
            # Exclude .git (it's a submodule pointer file) and any stale build
            # artifacts so the copy is a clean tree to build against.
            shutil.copytree(
                design.tb_repo_root,
                repo_copy,
                ignore=shutil.ignore_patterns(".git", "*.sim", "*.vcd"),
            )

            for sandbox_path in rtl_paths:
                dest_rel = overlay_map.get(sandbox_path)
                if dest_rel is None:
                    return Score(
                        value=INCORRECT,
                        explanation=(
                            f"sandbox path {sandbox_path!r} has no overlay "
                            "destination — sample built from a different design?"
                        ),
                    )
                dest = repo_copy / dest_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(await _read_final(sandbox_path))

            stdout, rc = await asyncio.to_thread(run_tb, repo_copy)

        (out_dir / "testbench.log").write_text(stdout)
        if rc == 0:
            return Score(value=CORRECT, answer="testbench passed")
        tail = stdout[-1500:]
        return Score(
            value=INCORRECT,
            answer="testbench failed",
            explanation=f"testbench failed (rc={rc}):\n{tail}",
        )

    return score
