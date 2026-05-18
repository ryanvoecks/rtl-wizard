"""Per-run output directory layout for new_benchmark.

The single source of truth for this run's outputs is `outputs/<RUN_TIMESTAMP>/`
under the repo root, where `RUN_TIMESTAMP` is captured *once at module import
time* — i.e., once per `inspect eval` invocation, so all samples share a dir.

`tasks.py` passes this path to `inspect_ai.eval(log_dir=...)` so the run's
`.eval` log lands here alongside per-sample artifacts. (Setting INSPECT_LOG_DIR
from import doesn't work — Click resolves it at CLI arg-parse time, before the
task module is imported.)
"""
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_ROOT = REPO_ROOT / "outputs"

RUN_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
RUN_OUTPUT_DIR = OUTPUTS_ROOT / RUN_TIMESTAMP
RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def sample_output_dir(sample_id: str) -> Path:
    p = RUN_OUTPUT_DIR / sample_id
    p.mkdir(parents=True, exist_ok=True)
    return p
