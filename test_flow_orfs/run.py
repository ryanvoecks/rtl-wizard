#!/usr/bin/env python3
"""Drive the ORFS make-based flow on any design folder.

The folder must contain exactly one `*.v` file; its stem is used as
DESIGN_NAME. The shared SDC (`constraint.sdc`) and the platform/utilization
knobs in `config.mk` apply to every design — only the design name, source
file, and DESIGN_DIR vary per run.

Artifacts land under `<repo>/eda_runs/`; ORFS already namespaces by design
name internally, so multiple corpus designs can coexist there.

Default targets stop at `do-finish` (final routed STA/area/power report)
rather than ORFS's `finish`, which additionally depends on GDS generation
via KLayout — not installed in every sandbox. Pass `finish`/`gds` explicitly
to get GDS.

Usage:
    ./run.py                          # uses the local design (./counter.v)
    ./run.py ../corpus/adder8         # full flow on a corpus design
    ./run.py ../corpus/adder8 synth   # stop after synthesis
    ./run.py . clean                  # ORFS clean for the local design
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EDA_RUNS = REPO_ROOT / "eda_runs"

DEFAULT_TARGETS = (
    "synth",
    # Post-synth timing report (1_Post_synthesis.rpt). Not strictly required by
    # the PnR flow, but extract_metrics.py needs it for synth WNS/TNS.
    "synth-report",
    "floorplan",
    "place",
    "cts",
    "route",
    "do-finish",
)


def resolve_design(design_dir: Path) -> tuple[str, Path]:
    """Locate the single `*.v` file in `design_dir`. The corpus convention is
    one design per folder; if that ever stops holding we want to fail loudly
    rather than guess."""
    candidates = sorted(design_dir.glob("*.v"))
    if not candidates:
        raise SystemExit(f"error: no *.v file in {design_dir}")
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(f"error: multiple *.v files in {design_dir}: {names}")
    verilog = candidates[0]
    return verilog.stem, verilog


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "design_dir", nargs="?", type=Path, default=HERE,
        help="Folder containing the design's .v file (default: this directory).",
    )
    ap.add_argument(
        "targets", nargs="*",
        help="ORFS make targets (default: full flow through do-finish).",
    )
    args = ap.parse_args(argv)

    design_dir = args.design_dir.resolve()
    if not design_dir.is_dir():
        sys.stderr.write(f"error: design dir not found: {design_dir}\n")
        return 1

    design_name, verilog = resolve_design(design_dir)
    out_dir = EDA_RUNS
    sdc_path = HERE / "constraint.sdc"
    config_mk = HERE / "config.mk"

    flow_home = Path(os.environ.get("FLOW_HOME", "/OpenROAD-flow-scripts/flow"))
    if not (flow_home / "Makefile").is_file():
        sys.stderr.write(
            f"error: ORFS flow Makefile not found at {flow_home / 'Makefile'}\n"
            "       set FLOW_HOME to your OpenROAD-flow-scripts/flow checkout.\n"
        )
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    targets = tuple(args.targets) if args.targets else DEFAULT_TARGETS

    # Per-design values override anything in config.mk; only the platform and
    # utilization knobs come from the config file.
    cmd = [
        "make",
        "-C", str(flow_home),
        f"DESIGN_CONFIG={config_mk}",
        f"DESIGN_NAME={design_name}",
        f"DESIGN_DIR={design_dir}",
        f"VERILOG_FILES={verilog}",
        f"SDC_FILE={sdc_path}",
        f"WORK_HOME={out_dir}",
        *targets,
    ]

    # Stream make's combined stdout+stderr to the terminal and a per-design
    # log file at the same time (replaces the bash `tee` from the old script).
    log_path = out_dir / f"flow_{design_name}.log"
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
            f"{out_dir}/{{logs,objects,reports,results}}/nangate45/{design_name}/base/"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
