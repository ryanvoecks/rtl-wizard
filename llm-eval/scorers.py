"""Scorers for llm-eval tasks.

Both scorers share the diff-persistence pattern: before the correctness check
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

from outputs import REPO_ROOT, sample_output_dir

_EXPECTED = "Hello, World!"
_RUN_TIMEOUT_S = 30

# Devcontainer's ORFS-bundled yosys, used as a fallback when `yosys` isn't on
# PATH. The lightweight llm-eval sandbox image deliberately doesn't ship
# yosys, so this scorer is host-side; the devcontainer's Dockerfile installs
# the EDA stack at this prefix.
_DEVCONTAINER_YOSYS = "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"
_YOSYS_TIMEOUT_S = 60

# Secworks/AES upstream layout: rtl under src/rtl, testbenches under src/tb,
# Makefile under toolruns/. The Makefile uses relative `../src/...` paths, so
# it must be invoked from toolruns/ as cwd.
_AES_REPO = REPO_ROOT / "external" / "aes"
_AES_BUILD_TIMEOUT_S = 120
_AES_RUN_TIMEOUT_S = 300


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


@scorer(metrics=[accuracy(), stderr()])
def hello_world_runs() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        # Persist artifacts first so a failed scoring run is still inspectable.
        await _save_artifacts(state)

        result = await sandbox().exec(
            ["python3", "hello.py"], timeout=_RUN_TIMEOUT_S
        )
        stdout = (result.stdout or "").strip()
        stderr_tail = (result.stderr or "")[-1000:]

        if not result.success:
            return Score(
                value=INCORRECT,
                answer=stdout,
                explanation=f"python3 hello.py exited with rc={result.returncode}:\n{stderr_tail}",
            )
        if stdout != _EXPECTED:
            return Score(
                value=INCORRECT,
                answer=stdout,
                explanation=f"stdout mismatch: expected {_EXPECTED!r}, got {stdout!r}",
            )
        return Score(value=CORRECT, answer=stdout)

    return score


def _resolve_yosys() -> str | None:
    path = shutil.which("yosys")
    if path:
        return path
    if Path(_DEVCONTAINER_YOSYS).exists():
        return _DEVCONTAINER_YOSYS
    return None


@scorer(metrics=[accuracy(), stderr()])
def aes_yosys_synthesisable() -> Scorer:
    """Cheap synthesisability check for the optimize_aes task.

    Pulls the agent's final `rtl/*.v` out of the sandbox to a host temp dir
    and runs host yosys through `hierarchy -check -top aes; proc; opt; clean`.
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
                "hierarchy -check -top aes; proc; opt; clean"
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


async def _run(
    *cmd: str,
    cwd: Path | None = None,
    timeout: float,
) -> tuple[int | None, str, str]:
    """Spawn a subprocess and return (returncode, stdout, stderr) or ('TIMEOUT')."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "", f"timed out after {timeout}s"
    return (
        proc.returncode,
        out_b.decode(errors="replace"),
        err_b.decode(errors="replace"),
    )


@scorer(metrics=[accuracy(), stderr()])
def aes_testbench_passes() -> Scorer:
    """Run the secworks/aes top-level testbench against the agent's RTL.

    Copies `external/aes` to a tempdir, overlays the agent's final `rtl/*.v`
    into `src/rtl/`, then runs `make top.sim` + `./top.sim` from `toolruns/`
    using the upstream Makefile (iverilog). CORRECT iff the testbench prints
    the "All NN test cases completed successfully" success line and does not
    print a failure line. The full simulator stdout is saved to
    `llm-results/<RUN_TIMESTAMP>/<sample_id>/aes_testbench.log` for debugging.
    """
    async def score(state: TaskState, target: Target) -> Score:
        await _save_artifacts(state)
        out_dir = sample_output_dir(str(state.sample_id))

        rtl_paths = list(state.metadata.get("original_files", {}).keys())
        if not rtl_paths:
            return Score(value=INCORRECT, explanation="no original_files in metadata")

        if shutil.which("iverilog") is None:
            return Score(
                value=INCORRECT,
                explanation="iverilog not found on host; cannot run secworks/aes testbench",
            )
        if not _AES_REPO.exists():
            return Score(
                value=INCORRECT,
                explanation=(
                    f"{_AES_REPO} not found — run `git submodule update --init` "
                    "to fetch the secworks/aes submodule"
                ),
            )

        with tempfile.TemporaryDirectory() as td:
            repo_copy = Path(td) / "aes"
            # Exclude .git (it's a submodule pointer file) and any stale build
            # artifacts so the copy is a clean tree to run make against.
            shutil.copytree(
                _AES_REPO,
                repo_copy,
                ignore=shutil.ignore_patterns(".git", "*.sim", "*.vcd"),
            )

            # Overlay agent's final RTL onto src/rtl/. Sandbox paths are
            # "rtl/<name>.v" (see tasks.py:_build_aes_dataset); upstream layout
            # puts them at src/rtl/<name>.v.
            for sandbox_path in rtl_paths:
                if not sandbox_path.startswith("rtl/"):
                    return Score(
                        value=INCORRECT,
                        explanation=f"unexpected sandbox path {sandbox_path!r}; expected rtl/*",
                    )
                dest = repo_copy / "src" / "rtl" / sandbox_path[len("rtl/"):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(await _read_final(sandbox_path))

            toolruns = repo_copy / "toolruns"

            rc, b_out, b_err = await _run(
                "make", "top.sim", cwd=toolruns, timeout=_AES_BUILD_TIMEOUT_S
            )
            if rc != 0:
                tail = (b_err or b_out)[-2000:]
                (out_dir / "aes_testbench.log").write_text(
                    f"# build failed (rc={rc})\n{b_out}\n--- stderr ---\n{b_err}"
                )
                return Score(
                    value=INCORRECT,
                    explanation=f"iverilog build failed (rc={rc}):\n{tail}",
                )

            rc, r_out, r_err = await _run(
                "./top.sim", cwd=toolruns, timeout=_AES_RUN_TIMEOUT_S
            )
            (out_dir / "aes_testbench.log").write_text(
                f"# rc={rc}\n{r_out}\n--- stderr ---\n{r_err}"
            )
            if rc is None:
                return Score(
                    value=INCORRECT,
                    explanation=f"testbench timed out after {_AES_RUN_TIMEOUT_S}s",
                )
            if rc != 0:
                tail = (r_err or r_out)[-2000:]
                return Score(
                    value=INCORRECT,
                    explanation=f"testbench exited rc={rc}:\n{tail}",
                )

        # tb_aes.v emits one of these two lines via display_test_results:
        #   "*** All NN test cases completed successfully"
        #   "*** NN tests completed - MM test cases did not complete successfully."
        # Treat the failure phrase as authoritative since the success substring
        # ("test cases completed successfully") is contained in both.
        if "did not complete successfully" in r_out:
            tail = r_out[-1500:]
            return Score(
                value=INCORRECT,
                answer="testbench reported failures",
                explanation=f"secworks/aes tb_aes reported failures:\n{tail}",
            )
        if "test cases completed successfully" in r_out:
            return Score(value=CORRECT, answer="testbench passed")
        tail = r_out[-1500:]
        return Score(
            value=INCORRECT,
            explanation=f"no pass/fail marker in tb_aes output:\n{tail}",
        )

    return score
