#!/usr/bin/env python3
"""Extract PPA metrics from the non-calibration phases of an ORFS batch.

Run `./run.py` first to populate `eda_runs/<batch>/`; this walks every
variant phase dir (`<benchmark>/<name>/<variant>/`, excluding the
`__calibration__` siblings) and emits one CSV row per variant with the
post-synth and post-route metrics joined together.

Most values are pulled straight from ORFS-emitted JSON; the synth-side
area / cell / FF count come from yosys's `synth_stat.txt` because
ORFS's `1_synth.json` doesn't record them.

Usage:
    ./extract_metrics.py 2026-05-12_17-29-08
    ./extract_metrics.py 2026-05-12_17-29-08 -o metrics.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from common.config import EDA_RUNS

# Cell-name prefixes that count as flip-flops/latches in the standard-cell
# libraries we use (nangate45 primarily; the prefix list is intentionally
# broad so it also works on sky130/asap7 without per-platform tweaking).
SEQUENTIAL_PREFIXES = ("DFF", "SDFF", "EDFF", "TDFF", "DLH", "DLL", "LATCH")

# Full-precision total stdcell area from yosys `stat`. The `cells` totals
# row uses %g formatting and flips to scientific notation (e.g.
# `1.32E+03`) past ~1000 um^2; the `Chip area for module '<top>'` line
# yosys prints below the per-cell breakdown carries the same number to
# full precision.
_CHIP_AREA_RE = re.compile(
    r"^\s*Chip area for module\s+'[^']+'\s*:\s*([\d.eE+-]+)\s*$", re.MULTILINE,
)

COLUMNS = [
    "batch", "design", "variant", "target_period_ps",
    "synth_area_um2", "synth_cell_count", "synth_ff_count",
    "synth_wns_ns", "synth_tns_ns",
    "route_area_um2", "route_cell_count",
    "route_ws_ns", "route_wns_ns", "route_tns_ns",
    "route_wirelength_um", "route_power_mw", "route_drc_count",
]


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
        Chip area for module '<top>': <area>             <-- below the block
    The totals row ends in `cells`; each cell-type row has the library cell
    name in the trailing column. Anything starting with one of
    SEQUENTIAL_PREFIXES is counted as a flop/latch. We take the total cell
    area from the `Chip area` line (full precision) rather than the totals
    row, which prints in scientific notation past ~1000 um^2.
    """
    text = path.read_text()
    chip_m = _CHIP_AREA_RE.search(text)
    if not chip_m:
        raise ValueError(f"no 'Chip area for module' line in {path}")
    total_area = float(chip_m.group(1))

    total_cells: int | None = None
    ff_count = 0
    in_cells_section = False
    # Area columns are matched loosely (`\S+`) so scientific notation in the
    # totals row doesn't break parsing; only the integer cell counts matter.
    totals_re = re.compile(r"^\s*(\d+)\s+\S+\s+\d+\s+\S+\s+cells\s*$")
    cell_re = re.compile(r"^\s*(\d+)\s+\S+\s+\d+\s+\S+\s+(\S+)\s*$")

    for line in text.splitlines():
        m = totals_re.match(line)
        if m:
            total_cells = int(m.group(1))
            in_cells_section = True
            continue
        if in_cells_section:
            if not line.strip():
                # Blank line ends the per-cell block.
                break
            m = cell_re.match(line)
            if m and any(m.group(2).startswith(p) for p in SEQUENTIAL_PREFIXES):
                ff_count += int(m.group(1))

    if total_cells is None:
        raise ValueError(f"could not find cells totals row in {path}")
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


def find_unique(phase_dir: Path, glob_pat: str, label: str) -> Path:
    """Return the single match for `glob_pat` under `phase_dir`. The
    platform/variant segments are glob-discovered rather than hardcoded so
    changing `PLATFORM` in Makefile.template doesn't require code edits."""
    matches = list(phase_dir.glob(glob_pat))
    if not matches:
        raise FileNotFoundError(f"no {label} under {phase_dir}/{glob_pat}")
    if len(matches) > 1:
        joined = ", ".join(str(m) for m in matches)
        raise RuntimeError(f"multiple {label} matches: {joined}")
    return matches[0]


def resolve_design_name(phase_dir: Path) -> str:
    """Read `DESIGN_NAME` (= top_module) out of the phase's rendered
    `inputs/Makefile`. That's what run.py wrote and what ORFS used for its
    `reports/<platform>/<DESIGN_NAME>/...` and `logs/...` hierarchy, so it
    matches the glob patterns in `extract()` regardless of how many RTL
    files the design has."""
    makefile = phase_dir / "inputs" / "Makefile"
    m = re.search(
        r"^\s*export\s+DESIGN_NAME\s*=\s*(\S+)\s*$",
        makefile.read_text(),
        re.MULTILINE,
    )
    if not m:
        raise ValueError(f"no `DESIGN_NAME` line in {makefile}")
    return m.group(1)


def extract(phase_dir: Path, design: str) -> dict:
    synth_stat = find_unique(
        phase_dir, f"reports/*/{design}/*/synth_stat.txt", "synth_stat.txt",
    )
    post_synth = find_unique(
        phase_dir, f"reports/*/{design}/*/1_Post_synthesis.rpt",
        "1_Post_synthesis.rpt",
    )
    finish_log = find_unique(
        phase_dir, f"logs/*/{design}/*/6_report.json", "6_report.json",
    )
    route_log = find_unique(
        phase_dir, f"logs/*/{design}/*/5_2_route.json", "5_2_route.json",
    )

    synth_area, synth_cells, synth_ff = parse_synth_stat(synth_stat)
    synth_wns, synth_tns = parse_post_synth_timing(post_synth)
    finish = json.loads(finish_log.read_text())
    route = json.loads(route_log.read_text())

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
        "route_ws_ns": route_ws,
        "route_wns_ns": route_wns,
        "route_tns_ns": finish["finish__timing__setup__tns"],
        "route_wirelength_um": route["detailedroute__route__wirelength"],
        # ORFS reports total power in watts; the metrics row uses mW.
        "route_power_mw": finish["finish__power__total"] * 1e3,
        "route_drc_count": route["detailedroute__route__drc_errors"],
    }


def resolve_batch(arg: str) -> Path:
    """Accept either a batch name (resolved under <repo>/eda_runs/) or a
    direct path to a batch dir."""
    p = Path(arg)
    if p.is_dir():
        return p.resolve()
    candidate = EDA_RUNS / arg
    if candidate.is_dir():
        return candidate
    raise SystemExit(f"error: batch not found as path or under {EDA_RUNS}: {arg}")


def iter_variant_phases(batch_dir: Path) -> list[Path]:
    """Every `<benchmark>/<name>/<variant>/` phase dir under the batch,
    sorted for deterministic CSV order. Skips the `__calibration__`
    siblings, and anything without a rendered SDC — that catches both
    spurious matches and phases that aborted before snapshot_inputs ran."""
    return sorted(
        p for p in batch_dir.glob("*/*/*")
        if p.is_dir()
        and p.name != "__calibration__"
        and (p / "inputs" / "constraint.sdc").is_file()
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "batch",
        help="Batch identifier — either a name under <repo>/eda_runs/ "
             "(e.g. 2026-05-12_17-29-08) or a direct path to a batch dir.",
    )
    ap.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Write CSV here in addition to stdout.",
    )
    args = ap.parse_args(argv)

    batch_dir = resolve_batch(args.batch)
    phases = iter_variant_phases(batch_dir)
    if not phases:
        sys.stderr.write(f"error: no variant phase dirs under {batch_dir}\n")
        return 1

    rows: list[dict] = []
    for phase_dir in phases:
        try:
            design = resolve_design_name(phase_dir)
            period_ps = parse_period_ps(phase_dir / "inputs" / "constraint.sdc")
            metrics = extract(phase_dir, design)
        except Exception as exc:
            sys.stderr.write(
                f"warning: skipping {phase_dir.relative_to(batch_dir)}: {exc}\n"
            )
            continue
        rows.append({
            "batch": batch_dir.name,
            "design": design,
            "variant": phase_dir.name,
            "target_period_ps": period_ps,
            **metrics,
        })

    writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)

    if args.output is not None:
        with args.output.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
