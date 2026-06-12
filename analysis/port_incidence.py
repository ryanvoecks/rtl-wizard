#!/usr/bin/env python3
"""Per-design fraction of near-critical post-synth paths incident to a primary port.

A path is "port-incident" if its startpoint is an input port (entry to the
timing graph) or its endpoint is an output port (exit). Both are derived from
the existing post-synth critical-path TSVs cached by `extract_topk_paths.py` --
ports are detected as names that contain no '/' (the TCL writes register pins
as <instance>/<pin>).

This is a pre-placement predictor of post-PnR ranking instability: if most
near-critical paths cross the design boundary, slack is dominated by pad
placement and I/O routing rather than internal logic, and PnR can reshuffle
the ranking arbitrarily by moving pads. Designs whose near-critical paths are
internal register-to-register pipeline stages (aes, sha512, viterbi, ...) sit
near zero and are insulated from pad placement.

Optional weighting (`weighted_port_incident_frac_*` columns) weights
output-incident paths by the **bus-stem** fan-in of their endpoint port: the
number of paths in the WHOLE post-synth pool that end at any bit of the same
port bus. This captures convergence (an `o_phy_cmd[*]`-style command bus that
many internal paths funnel into) rather than per-bit incidence. Non-output
paths keep weight 1.

For each band X in {1, 5, 10, 20}% of |WNS|, four metrics are reported:
  input_incident_frac          -- frac. near-crit starting at an input port
  output_incident_frac         -- frac. ending at an output port
  port_incident_frac           -- frac. that are either (union)
  weighted_port_incident_frac  -- union but output-incident weighted by
                                  endpoint bus-stem fan-in across the pool

Computed against three sources (each emits its own CSV) so the
pool-saturation artifact in the raw critical_paths pool (~500 paths often
piling up on one internal slack tier) doesn't hide the structural
boundary pattern visible in the deduplicated logical views.

Inputs:
  - analysis/data/critical_paths/<design>__1_synth.tsv   (physical, raw pool)
  - analysis/data/logical_paths/<design>__1_synth.rpt    (collapsed paths)
  - analysis/data/logical_blocks/<design>__1_synth.rpt   (collapsed blocks)

Outputs (physical has no suffix, logical variants are suffixed):
  - analysis/data/port_incidence.csv
  - analysis/data/port_incidence_logical_paths.csv
  - analysis/data/port_incidence_logical_blocks.csv

Usage:
    uv run python analysis/port_incidence.py
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

DATA_DIR = REPO / "analysis" / "data"
STAGE = "1_synth"
BANDS = (0.01, 0.05, 0.10, 0.20)

# (label, input dir, file ext, slack column index, start col, end col, output suffix).
# critical_paths TSV layout: slack | sp | ep | cells
# logical_*       RPT layout: rank  | slack | start | end
SOURCES: tuple[tuple[str, Path, str, int, int, int, str], ...] = (
    ("critical_paths", DATA_DIR / "critical_paths", ".tsv", 0, 1, 2, ""),
    ("logical_paths", DATA_DIR / "logical_paths", ".rpt", 1, 2, 3, "_logical_paths"),
    ("logical_blocks", DATA_DIR / "logical_blocks", ".rpt", 1, 2, 3, "_logical_blocks"),
)

# Strip a trailing bracketed index ([N] or [I]/[J]/...) so o_phy_cmd[5] and
# o_phy_cmd[12] share a fan-in bucket, and the placeholder-collapsed logical
# variants (`data_in[I]`) also share with their non-collapsed siblings.
_BUS_INDEX = re.compile(r"\[[A-Z0-9]+\]$")

# Yosys writes one `input`/`output`/`inout` per line in the synth .v module
# body (single-line declarations, optional packed range). Multi-line port lists
# don't show up here -- the synthesised netlist has one declaration per port.
_PORT_DECL = re.compile(r"^\s*(input|output|inout)\s+(?:\[[^\]]+\]\s+)?(\w+)\s*;")

# Mirror of `block_template` in eda_eval/tcl/worst_logical_paths.tcl: replace
# each successive digit run with the next letter in I, J, K, ... This is the
# collapse the logical_blocks rpt applies to every Verilog name, so an AXI
# slave port "s05_axi_araddr" lands as "sI_axi_araddr" in those rows. Without
# this mirror, port-incidence lookups against logical_blocks miss every port
# whose name contains digits (verilog_axi, uberddr3, ...).
_BLOCK_LETTERS = "IJKLMNOPQRSTUVWXYZ"
_DIGIT_RUN = re.compile(r"\d+")


def _bus_stem(port: str) -> str:
    """Strip trailing [N] so o_phy_cmd[5] and o_phy_cmd[12] share a fan-in bucket."""
    return _BUS_INDEX.sub("", port)


def _block_template(name: str) -> str:
    """Python mirror of `block_template` in worst_logical_paths.tcl."""
    out: list[str] = []
    rest = name
    i = 0
    while True:
        m = _DIGIT_RUN.search(rest)
        if not m:
            break
        out.append(rest[: m.start()])
        out.append(_BLOCK_LETTERS[i] if i < len(_BLOCK_LETTERS) else "?")
        rest = rest[m.end() :]
        i += 1
    out.append(rest)
    return "".join(out)


def _parse_yosys_ports(verilog: Path) -> dict[str, str]:
    """Return {port_name: 'input'|'output'|'inout'} from a Yosys-generated .v."""
    out: dict[str, str] = {}
    for line in verilog.read_text().splitlines():
        m = _PORT_DECL.match(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def _load_ports(design_name: str) -> dict[str, str]:
    """Locate the baseline synth Verilog for `design_name` and parse its ports."""
    for target in all_targets:
        if target.design.name != design_name:
            continue
        run = RunConfig(
            synth_target=target,
            output_dir=EDA_RUNS / "_dummy_baseline",
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=1,
        )
        cands = list(cache_path(run).glob("results/nangate45/*/base/1_2_yosys.v"))
        if not cands:
            return {}
        port_dirs = _parse_yosys_ports(cands[0])
        # Augment with block-template forms so logical_blocks rows (where every
        # digit run in the port name has been replaced with I/J/K/...) still
        # resolve. Literal names shadow template forms on collision.
        block_dirs = {_block_template(n): d for n, d in port_dirs.items()}
        return block_dirs | port_dirs
    return {}


def _port_direction(name: str, port_dirs: dict[str, str]) -> str | None:
    """Return direction ('input'/'output'/'inout') if `name` refers to a primary
    port of this design, else None. A port reference has no '/' and no '.' (it
    is a top-level identifier), and its bus stem matches one of the parsed
    port declarations."""
    if "/" in name or "." in name:
        return None
    return port_dirs.get(_bus_stem(name))


def _is_input(name: str, port_dirs: dict[str, str]) -> bool:
    d = _port_direction(name, port_dirs)
    return d in ("input", "inout")


def _is_output(name: str, port_dirs: dict[str, str]) -> bool:
    d = _port_direction(name, port_dirs)
    return d in ("output", "inout")


def _read_records(
    path: Path, slack_col: int, start_col: int, end_col: int
) -> list[tuple[float, str, str]]:
    """Return (slack_ns, startpoint, endpoint) per row."""
    out: list[tuple[float, str, str]] = []
    need = max(slack_col, start_col, end_col)
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) <= need:
            continue
        try:
            out.append((float(parts[slack_col]), parts[start_col], parts[end_col]))
        except ValueError:
            continue
    return out


def _process_source(
    label: str,
    in_dir: Path,
    ext: str,
    slack_col: int,
    start_col: int,
    end_col: int,
    suffix: str,
) -> None:
    files = sorted(in_dir.glob(f"*__{STAGE}{ext}"))
    if not files:
        print(f"skip {label}: no inputs under {in_dir}", file=sys.stderr)
        return
    print(f"\n== {label} ({len(files)} designs from {in_dir.relative_to(REPO)}) ==")

    rows: list[dict] = []
    for infile in files:
        design = infile.name.removesuffix(f"__{STAGE}{ext}")
        recs = _read_records(infile, slack_col, start_col, end_col)
        if not recs:
            print(f"skip {design}: empty pool", file=sys.stderr)
            continue
        port_dirs = _load_ports(design)
        if not port_dirs:
            print(f"skip {design}: no port list (baseline cache?)", file=sys.stderr)
            continue
        recs.sort(key=lambda r: r[0])
        wns = recs[0][0]

        # Whole-pool fan-in by output bus stem: how many paths in the entire
        # pool end at any bit of this port bus.
        bus_fanin: dict[str, int] = {}
        for _, _, ep in recs:
            if _is_output(ep, port_dirs):
                stem = _bus_stem(ep)
                bus_fanin[stem] = bus_fanin.get(stem, 0) + 1

        row: dict = {
            "design": design,
            "n_paths": len(recs),
            "wns_ns": f"{wns:.6f}",
        }
        for band in BANDS:
            tag = f"{int(band * 100)}pct"
            thresh = wns + abs(wns) * band
            near = [r for r in recs if r[0] <= thresh]
            n = len(near)
            row[f"n_near_{tag}"] = n
            if n == 0:
                for k in (
                    "input_incident_frac",
                    "output_incident_frac",
                    "port_incident_frac",
                    "weighted_port_incident_frac",
                ):
                    row[f"{k}_{tag}"] = ""
                continue
            n_in = sum(1 for _, sp, _ in near if _is_input(sp, port_dirs))
            n_out = sum(1 for _, _, ep in near if _is_output(ep, port_dirs))
            n_port = sum(
                1
                for _, sp, ep in near
                if _is_input(sp, port_dirs) or _is_output(ep, port_dirs)
            )
            total_w = 0
            port_w = 0
            for _, sp, ep in near:
                w = bus_fanin[_bus_stem(ep)] if _is_output(ep, port_dirs) else 1
                total_w += w
                if _is_input(sp, port_dirs) or _is_output(ep, port_dirs):
                    port_w += w
            row[f"input_incident_frac_{tag}"] = f"{n_in / n:.6f}"
            row[f"output_incident_frac_{tag}"] = f"{n_out / n:.6f}"
            row[f"port_incident_frac_{tag}"] = f"{n_port / n:.6f}"
            row[f"weighted_port_incident_frac_{tag}"] = (
                f"{port_w / total_w:.6f}" if total_w > 0 else ""
            )
        rows.append(row)

        print(
            f"{design:>16}  near10={row['n_near_10pct']:4}  "
            f"in10={row['input_incident_frac_10pct']:>8}  "
            f"out10={row['output_incident_frac_10pct']:>8}  "
            f"port10={row['port_incident_frac_10pct']:>8}  "
            f"wport10={row['weighted_port_incident_frac_10pct']:>8}"
        )

    cols = ["design", "n_paths", "wns_ns"]
    for band in BANDS:
        tag = f"{int(band * 100)}pct"
        cols += [
            f"n_near_{tag}",
            f"input_incident_frac_{tag}",
            f"output_incident_frac_{tag}",
            f"port_incident_frac_{tag}",
            f"weighted_port_incident_frac_{tag}",
        ]
    out_csv = DATA_DIR / f"port_incidence{suffix}.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    print(f"Wrote {out_csv.relative_to(REPO)} ({len(rows)} designs)")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--source",
        choices=[s[0] for s in SOURCES] + ["all"],
        default="all",
        help="which slack-pool source to process (default: all)",
    )
    args = ap.parse_args()

    for label, in_dir, ext, slack_col, start_col, end_col, suffix in SOURCES:
        if args.source not in ("all", label):
            continue
        _process_source(label, in_dir, ext, slack_col, start_col, end_col, suffix)


if __name__ == "__main__":
    main()
