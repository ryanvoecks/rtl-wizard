"""View llm_eval's inspect_ai .eval logs.

`inspect view` defaults to `./logs/` and won't find our runs, which live
under `llm_results/<timestamp>/`. This wrapper points it at LLM_RESULTS
(recursive=True) so every run shows up.

    uv run python llm_eval/view.py
"""

from inspect_ai import view

from common.config import LLM_RESULTS

# Fix for broken VSCode port forwarding
VIEW_PORT = 7676


def main() -> None:
    view(log_dir=str(LLM_RESULTS), recursive=True, port=VIEW_PORT)


if __name__ == "__main__":
    main()
