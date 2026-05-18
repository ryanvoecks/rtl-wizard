#!/usr/bin/env python3
"""Stage a fresh copy of a design and ask Claude Code to optimise its
timing, seeded with the latest logical-paths report.

For now the target is hardcoded to `secworks/aes`. The script:

  1. Copies `external/aes/` to `outputs/<timestamp>/secworks/aes/`.
  2. Locates the most recent
     `eda_runs/*/__iter_calibration__/secworks/aes/iter_0/.../logical_paths.rpt`.
  3. Invokes `claude -p --dangerously-skip-permissions` in the staged
     copy with the report inlined into the prompt.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

from calibrate import ITER_CAL_DIR
from common.config import AES, EDA_RUNS, OUTPUTS

BENCHMARK = "secworks"
NAME = "aes"
PLATFORM = "nangate45"


def find_latest_logical_paths(benchmark: str, name: str) -> Path:
    """Most recent logical_paths.rpt from an iter_0 calibration run. The
    timestamp format `%Y-%m-%d_%H-%M-%S` sorts lexicographically, so the
    last glob hit is the latest run."""
    pattern = (
        f"*/{ITER_CAL_DIR}/{benchmark}/{name}/iter_0/"
        f"reports/{PLATFORM}/{name}/base/logical_paths.rpt"
    )
    hits = sorted(EDA_RUNS.glob(pattern))
    if not hits:
        raise FileNotFoundError(
            f"no logical_paths.rpt under {EDA_RUNS}/{pattern} - "
            f"run `uv run eda_eval/calibrate.py --benchmark {benchmark} "
            f"--name {name}` first"
        )
    return hits[-1]


def build_prompt(report_text: str) -> str:
    return f"""You are working in a fresh copy of the secworks/aes design.
The Verilog sources live in src/rtl/*.v. Your task is to modify them
to optimise post-synthesis timing.

Below is the logical-paths report from the most recent synthesis run
(ranked by worst slack -- negative entries are timing-critical). Focus
on the negative-slack paths at the top.

--- logical_paths.rpt ---
{report_text}
--- end report ---

Edit the RTL files in place to reduce worst-slack paths. When done,
stop.
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()

    report_path = find_latest_logical_paths(BENCHMARK, NAME)
    report_text = report_path.read_text()

    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    target = OUTPUTS / ts / BENCHMARK / NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(AES, target)
    print(f"Staged {AES} -> {target}")
    print(f"Seeding with {report_path}")

    prompt = build_prompt(report_text)
    subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions", prompt],
        cwd=target,
        check=True,
    )


if __name__ == "__main__":
    main()
