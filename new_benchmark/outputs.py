"""Per-run output directory layout for new_benchmark.

Outputs land at `outputs/<RUN_TIMESTAMP>/<sample_id>/` under the repo root, where
`RUN_TIMESTAMP` is captured *once at module import time* — i.e., once per
`inspect eval` invocation, so all samples in a single run share a directory.

The timestamp format mirrors the existing `outputs/2026-05-17_22-56-50/` layout
in the repo to keep file-browser sorting predictable across benchmarks.
"""
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_ROOT = REPO_ROOT / "outputs"

RUN_TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
RUN_OUTPUT_DIR = OUTPUTS_ROOT / RUN_TIMESTAMP


def sample_output_dir(sample_id: str) -> Path:
    p = RUN_OUTPUT_DIR / sample_id
    p.mkdir(parents=True, exist_ok=True)
    return p
