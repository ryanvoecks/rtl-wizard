#!/usr/bin/env python3
"""Drive the ORFS make-based flow for the counter test design.

Mirrors ../test_flow but replaces all the hand-written TCL with the stock
ORFS Makefile. `config.mk` in this directory configures the design;
`WORK_HOME` is pointed at `./out` so artifacts land next to this script
rather than in the upstream ORFS tree.

Default targets stop at `do-finish` (final routed STA/area/power report)
rather than ORFS's `finish`, which additionally depends on GDS generation
via KLayout — not installed in every sandbox and not part of what the
hand-written flow does either. Pass `finish`/`gds` explicitly to get GDS.

Usage:
    ./run.py                # synth -> ... -> route -> do-finish
    ./run.py synth          # stop after synthesis
    ./run.py clean          # ORFS clean
    ./run.py finish         # full flow including GDS (needs KLayout)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGETS = ("synth", "floorplan", "place", "cts", "route", "do-finish")


def main(argv: list[str]) -> int:
    design_dir = Path(__file__).resolve().parent
    out_dir = design_dir / "out"
    flow_home = Path(os.environ.get("FLOW_HOME", "/OpenROAD-flow-scripts/flow"))

    if not (flow_home / "Makefile").is_file():
        sys.stderr.write(
            f"error: ORFS flow Makefile not found at {flow_home / 'Makefile'}\n"
            "       set FLOW_HOME to your OpenROAD-flow-scripts/flow checkout.\n"
        )
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    targets = tuple(argv) if argv else DEFAULT_TARGETS

    cmd = [
        "make",
        "-C", str(flow_home),
        f"DESIGN_CONFIG={design_dir / 'config.mk'}",
        f"DESIGN_DIR={design_dir}",
        f"WORK_HOME={out_dir}",
        *targets,
    ]

    # Stream make's combined stdout+stderr to the terminal and a log file at
    # the same time (the bash `tee` we replaced).
    log_path = out_dir / "flow.log"
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    ) as proc, log_path.open("w") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
        rc = proc.wait()

    if rc == 0:
        print(
            f"\nDone. ORFS artifacts under "
            f"{out_dir}/{{logs,objects,reports,results}}/nangate45/counter/base/"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
