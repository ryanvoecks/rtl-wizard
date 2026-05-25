"""Run llm_eval's task via the inspect_ai Python API.

Owns the per-run output dir AND the shared sandbox container's lifecycle:
the container is created/started once for the whole eval and stopped at
the end, so every sample's ClaudeEnv attaches to the same running
container (which keeps one OAUTH Claude Code session across samples).

    uv run python llm_eval/run.py
"""

from datetime import datetime

from components.container import Container
from components.tasks import optimize_timing
from inspect_ai import eval as inspect_eval

from common.config import LLM_RESULTS


def main() -> None:
    run_dir = LLM_RESULTS / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {run_dir}", flush=True)

    with Container() as container:
        inspect_eval(
            optimize_timing(run_dir, container),
            model="anthropic/claude-sonnet-4-5",
            log_dir=str(run_dir),
        )


if __name__ == "__main__":
    main()
