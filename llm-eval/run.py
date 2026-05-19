"""Run llm-eval's task via the inspect_ai Python API.

Owns the per-run output dir so the .eval log and per-sample artifacts
(diff.patch, *.log) all land under `llm-results/<timestamp>/`.

    uv run python llm-eval/run.py
"""
from datetime import datetime

from inspect_ai import eval as inspect_eval

from common.config import LLM_RESULTS
from components.tasks import optimize_timing


def main() -> None:
    run_dir = LLM_RESULTS / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {run_dir}", flush=True)

    inspect_eval(
        optimize_timing(run_dir),
        model="none/claude-sonnet-4-5",
        log_dir=str(run_dir),
    )


if __name__ == "__main__":
    main()
