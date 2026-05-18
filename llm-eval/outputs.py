"""Per-run output directory layout for new_benchmark.

The single source of truth for this run's outputs is `llm-results/<RUN_TIMESTAMP>/`
under the repo root, where `RUN_TIMESTAMP` is captured *once at module import
time* — i.e., once per `inspect eval` invocation, so all samples share a dir.

Importing this module also redirects Inspect's `.eval` log into the same dir
(monkey-patch on `inspect_ai._eval.eval.create_recorder_for_format`). We can't
influence log_dir via `INSPECT_LOG_DIR` from a task module because Inspect's
Click CLI cements the value at parse time (before any task module is loaded)
and the eval-time fallback at `inspect_ai/_eval/eval.py:654` only fires when
the resolved `log_dir` is empty — Click's `default="./logs"` makes it never
empty. The recorder, however, is constructed *after* `eval_resolve_tasks`
loads task modules (`inspect_ai/_eval/eval.py:613` vs `:656`), so patching the
factory at import time runs in time to redirect that one call.
"""
from datetime import datetime
from pathlib import Path

import inspect_ai._eval.eval as _eval_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_ROOT = REPO_ROOT / "llm-results"

RUN_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
RUN_OUTPUT_DIR = OUTPUTS_ROOT / RUN_TIMESTAMP
RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def sample_output_dir(sample_id: str) -> Path:
    p = RUN_OUTPUT_DIR / sample_id
    p.mkdir(parents=True, exist_ok=True)
    return p


# eval.py imports create_recorder_for_format with `from ... import` (line 51),
# so the call site refers to the LOCAL binding in `inspect_ai._eval.eval` —
# patch that, not the canonical one in `inspect_ai.log._recorders`.
_orig_create_recorder = _eval_mod.create_recorder_for_format


def _create_recorder_in_run_dir(log_format, log_dir):  # type: ignore[no-untyped-def]
    return _orig_create_recorder(log_format, str(RUN_OUTPUT_DIR))


_eval_mod.create_recorder_for_format = _create_recorder_in_run_dir
