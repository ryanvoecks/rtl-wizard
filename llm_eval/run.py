"""Run llm_eval's task via the inspect_ai Python API.

Owns the per-run output dir AND the shared sandbox container's lifecycle:
the container is created/started once for the whole eval and stopped at
the end, so every sample's ClaudeEnv attaches to the same running
container (which keeps one OAUTH Claude Code session across samples).

    uv run python llm_eval/run.py
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from components.container import Container
from components.tasks import optimize_timing
from inspect_ai import Task, eval_retry
from inspect_ai import eval as inspect_eval
from inspect_ai.log import read_eval_log

from common.config import LLM_RESULTS

# Eval config
MAX_PARALLEL_SESSIONS = 4
MODEL = "anthropic/claude-sonnet-4-5"


def run(run_dir: Path, task: Task, **kwargs: Any) -> None:
    """Start, retry, or skip based on any existing .eval log in `run_dir`."""
    existing = sorted(run_dir.glob("*.eval"))
    if not existing:
        inspect_eval(task, run_dir=str(run_dir), **kwargs)
    log = read_eval_log(str(existing[-1]), header_only=True)
    if log.status == "success":
        print(f"Skipping (success): {existing[-1]}")
    print(f"Retrying ({log.status}): {existing[-1]}")
    eval_retry(log)


def main() -> None:
    run_dir = LLM_RESULTS / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {run_dir}", flush=True)

    with Container() as container:
        run(
            run_dir,
            optimize_timing(run_dir, container),
            model=MODEL,
            max_samples=MAX_PARALLEL_SESSIONS,
            sample_id=[
                "secworks/aes/reference",
                "abdelazeem201/systolic_tpu/reference",
            ],
        )


if __name__ == "__main__":
    main()
