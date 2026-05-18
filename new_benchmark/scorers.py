"""Scorer for the broken-Hello-World smoke test.

Runs the agent's (presumably fixed) `hello.py` inside the same solver sandbox
the agent worked in, and checks that stdout matches `Hello, World!` exactly.
Mirrors the simplest pattern in benchmark/scorers.py:rtllm_make_passes — sandbox
exec + stdout check + Score(value=CORRECT/INCORRECT, explanation=...).
"""
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

_EXPECTED = "Hello, World!"
_RUN_TIMEOUT_S = 30


@scorer(metrics=[accuracy(), stderr()])
def hello_world_runs() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
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
