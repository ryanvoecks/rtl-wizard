"""View llm-eval's inspect_ai .eval logs.

`inspect view` defaults to `./logs/` and won't find our runs, which live
under `llm-results/<timestamp>/`. This wrapper points it at LLM_RESULTS
(recursive=True) so every run shows up.

    uv run python llm-eval/view.py
"""

from inspect_ai import view

from common.config import LLM_RESULTS


def main() -> None:
    view(log_dir=str(LLM_RESULTS), recursive=True)


if __name__ == "__main__":
    main()
