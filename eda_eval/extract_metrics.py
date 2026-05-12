#!/usr/bin/env python3
"""Extract a flat PPA metrics row from an ORFS flow run.

Run `./run.py [design_dir]` first; this reads its outputs and emits one JSON
dict with post-synth + post-route metrics joined together, suitable for
appending to a results table.

Most values are pulled straight from ORFS-emitted JSON; the synth-side area /
cell / FF count come from yosys's `synth_stat.txt` because ORFS's
`1_synth.json` doesn't record them.

Usage:
    ./extract_metrics.py --system rtlcoder --sample-id 2 --seed 1
    ./extract_metrics.py --design-dir ../corpus/adder8 \\
        --system rtlcoder --sample-id 2 --seed 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Cell-name prefixes that count as flip-flops/latches in the standard-cell
# libraries we use (nangate45 primarily; the prefix list is intentionally
# broad so it also works on sky130/asap7 without per-platform tweaking).
SEQUENTIAL_PREFIXES = ("DFF", "SDFF", "EDFF", "TDFF", "DLH", "DLL", "LATCH")


def parse_config_mk(path: Path) -> dict[str, str]:
    """Pull `export KEY = VALUE` lines out of a config.mk. Good enough for
    PLATFORM — we don't try to expand `$(...)`."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*export\s+(\w+)\s*[:?]?=\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def resolve_design_name(design_dir: Path) -> str:
    """Stem of the single `*.v` in `design_dir`. Mirrors run.py's rule so the
    same folder argument gives the same DESIGN_NAME on both sides."""
    candidates = sorted(design_dir.glob("*.v"))
    if not candidates:
        raise SystemExit(f"error: no *.v file in {design_dir}")
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(f"error: multiple *.v files in {design_dir}: {names}")
    return candidates[0].stem


def parse_period_ps(sdc_path: Path) -> int:
    """Read `create_clock -period <ns>` out of an SDC. Multi-clock designs
    take the first occurrence — same convention as ORFS's
    ABC_CLOCK_PERIOD_IN_PS extraction."""
    m = re.search(r"-period\s+([\d.]+)", sdc_path.read_text())
    if not m:
        raise ValueError(f"no `-period` clause found in {sdc_path}")
    return int(round(float(m.group(1)) * 1000))


def parse_synth_stat(path: Path) -> tuple[float, int, int]:
    """Pull total cell area, total cell count, and flip-flop count out of
    yosys's `stat` text output.

    Layout of the file:
        ...
              N        area      N        area cells     <-- totals row
                ...
              K        area      K        area   CELL_NAME
                ...
    The totals row ends in `cells`; each cell-type row has the library cell
    name in the trailing column. Anything starting with one of
    SEQUENTIAL_PREFIXES is counted as a flop/latch.
    """
    total_area: float | None = None
    total_cells: int | None = None
    ff_count = 0
    in_cells_section = False

    totals_re = re.compile(r"^\s*(\d+)\s+([\d.]+)\s+\d+\s+[\d.]+\s+cells\s*$")
    cell_re = re.compile(r"^\s*(\d+)\s+[\d.]+\s+\d+\s+[\d.]+\s+(\S+)\s*$")

    for line in path.read_text().splitlines():
        m = totals_re.match(line)
        if m:
            total_cells = int(m.group(1))
            total_area = float(m.group(2))
            in_cells_section = True
            continue
        if in_cells_section:
            if not line.strip():
                # Blank line ends the per-cell block.
                break
            m = cell_re.match(line)
            if m and any(m.group(2).startswith(p) for p in SEQUENTIAL_PREFIXES):
                ff_count += int(m.group(1))

    if total_area is None or total_cells is None:
        raise ValueError(f"could not find cell totals in {path}")
    return total_area, total_cells, ff_count


def parse_post_synth_timing(rpt_path: Path) -> tuple[float, float]:
    """Pull WNS and TNS (both in ns) out of a `report_metrics` rpt file.
    OpenSTA prints `tns max <X>` and `wns max <X>` lines; when timing is
    met both are 0.00."""
    text = rpt_path.read_text()
    wns_m = re.search(r"^\s*wns max\s+(-?[\d.]+)", text, re.MULTILINE)
    tns_m = re.search(r"^\s*tns max\s+(-?[\d.]+)", text, re.MULTILINE)
    if not wns_m or not tns_m:
        raise ValueError(f"could not find wns/tns lines in {rpt_path}")
    return float(wns_m.group(1)), float(tns_m.group(1))


def extract(work_home: Path, platform: str, design: str, variant: str) -> dict:
    base = work_home
    logs = base / "logs" / platform / design / variant
    reports = base / "reports" / platform / design / variant

    synth_area, synth_cells, synth_ff = parse_synth_stat(reports / "synth_stat.txt")
    synth_wns, synth_tns = parse_post_synth_timing(reports / "1_Post_synthesis.rpt")

    finish = json.loads((logs / "6_report.json").read_text())
    route = json.loads((logs / "5_2_route.json").read_text())

    # `finish__timing__setup__ws` is the worst SLACK (positive when met).
    # WNS is the conventional "0 if met, otherwise the negative slack".
    route_ws = finish["finish__timing__setup__ws"]
    route_wns = min(0.0, route_ws)

    return {
        # Post-synth: yosys cell stats + OpenSTA-on-linked-netlist timing.
        "synth_area_um2": synth_area,
        "synth_cell_count": synth_cells,
        "synth_ff_count": synth_ff,
        "synth_wns_ns": synth_wns,
        "synth_tns_ns": synth_tns,
        # Post-route: `stdcell` keys exclude fill+tap so the comparison with
        # synth_cell_count is apples-to-apples (CTS buffers + repair cells
        # are included; physical-only fill is not).
        "route_area_um2": finish["finish__design__instance__area__stdcell"],
        "route_cell_count": finish["finish__design__instance__count__stdcell"],
        "route_wns_ns": route_wns,
        "route_tns_ns": finish["finish__timing__setup__tns"],
        "route_wirelength_um": route["detailedroute__route__wirelength"],
        # ORFS reports total power in watts; the metrics row uses mW.
        "route_power_mw": finish["finish__power__total"] * 1e3,
        "route_drc_count": route["detailedroute__route__drc_errors"],
    }


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    config_defaults = parse_config_mk(here / "templates" / "Makefile.template")
    default_platform = config_defaults.get("PLATFORM", "nangate45")

    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--design-dir", type=Path, default=here,
        help="Folder containing the design's .v file (default: this directory). "
             "Used to derive --design when --design is not given.",
    )
    ap.add_argument(
        "--work-home", type=Path, default=here.parent / "eda_runs",
        help="Phase dir from a run.py batch — typically "
             "`eda_runs/<ts>/<rel>/final/` — containing inputs/, logs/, "
             "reports/, etc. for the run to extract metrics from.",
    )
    ap.add_argument(
        "--design", default=None,
        help="Override the design name (default: stem of the *.v in --design-dir).",
    )
    ap.add_argument("--platform", default=default_platform)
    ap.add_argument("--variant", default="base")
    ap.add_argument(
        "--sdc", type=Path, default=None,
        help="Rendered SDC to read the target period from "
             "(default: <work-home>/inputs/constraint.sdc, which run.py "
             "drops next to the flow outputs for that phase).",
    )
    ap.add_argument("--system", required=True,
                    help="Upstream RTL-generation system name (e.g. rtlcoder).")
    ap.add_argument("--sample-id", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument(
        "--target-period-ps", type=int, default=None,
        help="Override; otherwise derived from the first `-period` in --sdc.",
    )
    ap.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Write JSON here as well as to stdout.",
    )
    args = ap.parse_args(argv)

    design_name = args.design or resolve_design_name(args.design_dir.resolve())
    sdc_path = args.sdc or (args.work_home / "inputs" / "constraint.sdc")
    period_ps = args.target_period_ps or parse_period_ps(sdc_path)

    row = {
        "system": args.system,
        "design": design_name,
        "sample_id": args.sample_id,
        "seed": args.seed,
        "target_period_ps": period_ps,
        **extract(args.work_home, args.platform, design_name, args.variant),
    }

    text = json.dumps(row, indent=2)
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
