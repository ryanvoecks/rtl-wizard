"""Scorer for the broken-Hello-World smoke test.

The full agent transcript (messages, tool calls, tool results, usage) is now
captured in `state.messages` / `state.output` by the solver and lands directly
in the run's `.eval` log under `outputs/<RUN_TIMESTAMP>/`. The only per-sample
side artifact persisted here is a `diff.patch` — convenient for humans/scripts
that just want to see what files the agent changed without parsing the log.

Persistence happens *before* the correctness check so a failed run is still
debuggable from disk. The check itself runs the agent's `hello.py` inside the
solver sandbox and matches stdout against `Hello, World!` exactly.
"""
import difflib

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

from new_benchmark.outputs import sample_output_dir

_EXPECTED = "Hello, World!"
_RUN_TIMEOUT_S = 30


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
