"""Run llm_eval's task via the inspect_ai Python API.

Owns the per-run output dir AND the shared sandbox container's lifecycle:
the container is built once (first run only -- subsequent runs just restart
the existing container so the OAUTH login is preserved) and stopped at the
end. Every sample's ClaudeEnv attaches to the same running container (which
keeps one OAUTH Claude Code session across samples) and mounts onto the same
eval-wide MCP host (so `sse_starlette`'s process-global shutdown signal
never fires between samples). To force a rebuild + fresh OAUTH login, call
`Container.teardown()` explicitly.

    uv run python llm_eval/run.py
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from components.container import Container
from components.solvers import claude_code_iterative_solver
from components.tasks import optimize_timing
from inspect_ai import Task, eval_async, eval_retry_async
from inspect_ai.log import read_eval_log

from common.config import ARTIFACTS, LLM_RESULTS

# Inspect requires an API key (working or not) in environment, so set a fake one
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

# Eval config
MAX_PARALLEL_SESSIONS = 4
MODEL = "anthropic/claude-haiku-4-5"
EPOCHS = 1
SOLVER = claude_code_iterative_solver


async def run(run_dir: Path, task: Task, **kwargs: Any) -> None:
    """Start, retry, or skip based on any existing .eval log in `run_dir`."""
    existing = sorted(run_dir.glob("*.eval"))
    if not existing:
        await eval_async(task, log_dir=str(run_dir), fail_on_error=False, **kwargs)
        return
    log = read_eval_log(str(existing[-1]), header_only=True)
    if log.status == "success":
        print(f"Skipping (success): {existing[-1]}")
        return
    print(f"Retrying ({log.status}): {existing[-1]}")
    await eval_retry_async(log, fail_on_error=False)


async def main_async() -> None:
    """We need to run this async to allow shared MCPService __aenter__ and __aexit__"""
    # run_dir = ARTIFACTS / "llm" / "synth_feedback_agents
    run_dir = LLM_RESULTS / "iterative_basic_test_haiku_6"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {run_dir}", flush=True)

    async with Container():
        await run(
            run_dir,
            optimize_timing(str(run_dir), SOLVER()),
            model=MODEL,
            max_samples=MAX_PARALLEL_SESSIONS,
            epochs=EPOCHS,
            sample_id="aes",
        )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
