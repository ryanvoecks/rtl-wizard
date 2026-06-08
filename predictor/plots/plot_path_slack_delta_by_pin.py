#!/usr/bin/env python3
"""Per-design scatter of post-route - post-synth slack delta, coloured by
endpoint pin function (with port incidence as a secondary classification).

A sibling of `plot_path_slack_delta.py`: same axes, same union of logical_blocks
paths, same x-ordering. The only differences are how each point is coloured
and the legend.

Endpoint pin classification is the *primary* category: any path whose endpoint
is a non-data pin (async reset/preset RN/SN/R/S, scan input SI/SE) is coloured
yellow regardless of whether it also happens to touch a primary port. These
arcs are recovery/removal-typed in the liberty model, not setup/hold, and a
fix-the-path agent treating them as functional data is being misled.

The detector reads the endpoint pin name from the raw `end_full` column of
the logical_blocks rpts. yosys's generic cells use the same pin letters that
nangate45 liberty uses (D for data, RN/SN for async reset/preset, SI/SE for
scan), so the rpt text is enough -- no liberty parsing required. A path is
marked non-data if *either* the post-synth rpt or the post-route rpt names a
non-data endpoint pin for that logical group; bit-level differences between
the two stages should not hide the fact that the bus has an async/scan input.

For paths whose endpoint *is* a data pin, the secondary classification matches
the original plot: input port at startpoint and output port at endpoint ->
purple, input only -> blue, output only -> red, neither -> grey.

Inputs:
  - analysis/data/path_slack_union/<design>.tsv
  - analysis/data/logical_blocks/<design>__{1_synth,6_final}.rpt

Outputs:
  - analysis/plots/path_slack_delta_by_pin.png

Usage:
    uv run --with matplotlib python analysis/plot_path_slack_delta_by_pin.py
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

DATA_DIR = REPO / "analysis" / "data" / "path_slack_union"
RPT_DIR = REPO / "analysis" / "data" / "logical_blocks"
OUT_PNG = REPO / "predictor" / "plots" / "output" / "path_slack_delta_by_pin.png"
TEXT_COLOR = "#222222"

# Endpoint pin names that correspond to a real setup/hold timing arc. Anything
# else seen as an endpoint is async control (RN/SN/R/S) or scan (SI/SE/scan_in/
# scan_en) -- both flagged as non-data.
DATA_ENDPOINT_PINS = {"D"}

CAT_INPUT = "input"
CAT_OUTPUT = "output"
CAT_BOTH = "both"
CAT_NEITHER = "neither"
CAT_NON_DATA = "non_data"
CAT_COLORS = {
    CAT_INPUT: "tab:blue",
    CAT_OUTPUT: "tab:red",
    CAT_BOTH: "tab:purple",
    CAT_NEITHER: "#bbbbbb",
    CAT_NON_DATA: "#f1c40f",
}
CAT_LABELS = {
    CAT_INPUT: "input at start",
    CAT_OUTPUT: "output at end",
    CAT_BOTH: "input AND output",
    CAT_NEITHER: "internal",
    CAT_NON_DATA: "non-data endpoint",
}
# Plot order = z-order. Non-data drawn last so the yellow points sit on top.
CAT_ORDER = (CAT_NEITHER, CAT_INPUT, CAT_OUTPUT, CAT_BOTH, CAT_NON_DATA)

_PORT_DECL = re.compile(r"^\s*(input|output|inout)\s+(?:\[[^\]]+\]\s+)?(\w+)\s*;")
_BUS_INDEX = re.compile(r"\[[A-Z0-9]+\]$")


def _bus_stem(name: str) -> str:
    return _BUS_INDEX.sub("", name)


def _parse_yosys_ports(verilog: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in verilog.read_text().splitlines():
        m = _PORT_DECL.match(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def _load_ports(design_name: str) -> dict[str, str]:
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
        return _parse_yosys_ports(cands[0])
    return {}


def _direction(full_name: str, ports: dict[str, str]) -> str | None:
    if "/" in full_name or "." in full_name:
        return None
    return ports.get(_bus_stem(full_name))


def _is_input(full_name: str, ports: dict[str, str]) -> bool:
    return _direction(full_name, ports) in ("input", "inout")


def _is_output(full_name: str, ports: dict[str, str]) -> bool:
    return _direction(full_name, ports) in ("output", "inout")


def _endpoint_pin(end_full: str) -> str | None:
    """Pin name after the final '/' in `end_full`, or None for port endpoints."""
    if "/" not in end_full:
        return None
    return end_full.rsplit("/", 1)[-1]


def _is_non_data(end_fulls: list[str]) -> bool:
    """True if any stage's representative endpoint names a non-data pin.

    A logical group's representative bit can differ between 1_synth and 6_final
    (the worst bit shifts), and one stage may surface a D pin while the other
    surfaces an RN/SN. Flagging on *any* stage keeps the bus consistently
    coloured rather than flickering based on which bit happens to be worst.
    """
    for end_full in end_fulls:
        pin = _endpoint_pin(end_full)
        if pin is not None and pin not in DATA_ENDPOINT_PINS:
            return True
    return False


def _classify(
    start_full: str, end_full: str, end_fulls: list[str], ports: dict[str, str]
) -> str:
    if _is_non_data(end_fulls):
        return CAT_NON_DATA
    si = _is_input(start_full, ports)
    eo = _is_output(end_full, ports)
    if si and eo:
        return CAT_BOTH
    if si:
        return CAT_INPUT
    if eo:
        return CAT_OUTPUT
    return CAT_NEITHER


def _read_union(path: Path) -> list[tuple[str, str, float, float]]:
    out: list[tuple[str, str, float, float]] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            out.append((parts[0], parts[1], float(parts[2]), float(parts[3])))
        except ValueError:
            continue
    return out


def _read_full_name_map(
    design_name: str,
) -> dict[tuple[str, str], dict[str, tuple[str, str]]]:
    """Map (collapsed_start, collapsed_end) -> {stage: (start_full, end_full)}.

    Keeps stages separate so non-data detection can OR across them; a bit that
    looks like a D-pin path in synth may surface as the worst path through an
    RN pin after route, and we want the logical group coloured for the bus
    that contains the non-data pin, not just the bit that won the worst-path
    race in one stage.
    """
    out: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for stage in ("1_synth", "6_final"):
        rpt = RPT_DIR / f"{design_name}__{stage}.rpt"
        if not rpt.is_file():
            continue
        for line in rpt.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            out.setdefault((parts[2], parts[3]), {})[stage] = (parts[4], parts[5])
    return out


def _set_rc() -> None:
    rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["STIX Two Text", "STIXGeneral", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "axes.labelsize": 13,
            "axes.titlesize": 13,
            "axes.edgecolor": TEXT_COLOR,
            "axes.labelcolor": TEXT_COLOR,
            "axes.linewidth": 1.0,
            "xtick.color": TEXT_COLOR,
            "ytick.color": TEXT_COLOR,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.major.size": 3,
            "ytick.major.size": 3,
            "xtick.major.width": 0.9,
            "ytick.major.width": 0.9,
            "legend.fontsize": 11,
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def _plot(designs: list[tuple[str, list[dict]]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n = len(designs)
    ncols = 3
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(
        nrows, ncols, figsize=(ncols * 3.8, nrows * 2.7), sharey=False
    )
    axes_flat = list(axes.flat) if hasattr(axes, "flat") else [axes]

    for idx, (ax, (name, rows)) in enumerate(zip(axes_flat, designs)):
        rows = sorted(rows, key=lambda r: r["slack_synth"])
        for cat in CAT_ORDER:
            xs = [i for i, r in enumerate(rows) if r["category"] == cat]
            ys = [
                r["slack_route"] - r["slack_synth"]
                for r in rows
                if r["category"] == cat
            ]
            if not xs:
                continue
            ax.scatter(
                xs, ys,
                color=CAT_COLORS[cat],
                s=14, alpha=0.8, linewidths=0.0,
            )

        ax.axhline(0, color="#888888", linewidth=0.6, zorder=1)
        ax.set_title(name)
        ax.grid(linestyle="-", linewidth=0.4, color="#d6d6d6", alpha=0.9, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

        row = idx // ncols
        col = idx % ncols
        if row == nrows - 1:
            ax.set_xlabel("path rank (by synth slack)")
        if col == 0:
            ax.set_ylabel("route - synth slack (ns)")

    for ax in list(axes_flat)[n:]:
        ax.set_visible(False)

    seen: set[str] = set()
    for _, rows in designs:
        for r in rows:
            seen.add(r["category"])
    handles = [
        plt.Line2D(
            [], [], linestyle="", marker="o", markersize=8,
            markerfacecolor=CAT_COLORS[c], markeredgecolor="none",
            label=CAT_LABELS[c],
        )
        for c in CAT_ORDER
        if c in seen
    ]
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=len(handles),
        frameon=False,
        bbox_to_anchor=(0.5, -0.02),
    )

    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out_path, bbox_inches="tight")
    print(f"Wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT_PNG)
    args = ap.parse_args()
    _set_rc()

    files = sorted(DATA_DIR.glob("*.tsv"))
    designs: list[tuple[str, list[dict]]] = []
    for f in files:
        name = f.stem
        union = _read_union(f)
        if not union:
            print(f"skip {name}: empty union", file=sys.stderr)
            continue
        ports = _load_ports(name)
        full_map = _read_full_name_map(name)
        rows = []
        missing_full = 0
        for s, e, ss, sr in union:
            stage_fulls = full_map.get((s, e), {})
            if not stage_fulls:
                # No full names for this pair -- fall back to the collapsed
                # values so the point still appears, classified as neither.
                start_full, end_full = s, e
                end_fulls: list[str] = []
                missing_full += 1
            else:
                # Pick any stage for the port-incidence lookup; the bus stem
                # is the same either way. Pass all stages' endpoints into the
                # non-data check so we OR across both.
                start_full, end_full = next(iter(stage_fulls.values()))
                end_fulls = [v[1] for v in stage_fulls.values()]
            rows.append(
                {
                    "start": s,
                    "end": e,
                    "start_full": start_full,
                    "end_full": end_full,
                    "slack_synth": ss,
                    "slack_route": sr,
                    "category": _classify(start_full, end_full, end_fulls, ports),
                }
            )
        if missing_full:
            print(
                f"warn {name}: {missing_full}/{len(union)} pairs had no full-name match",
                file=sys.stderr,
            )
        designs.append((name, rows))

    if not designs:
        print("no per-design TSVs found", file=sys.stderr)
        sys.exit(1)

    print(f"Plotting {len(designs)} designs")
    _plot(designs, args.out)


if __name__ == "__main__":
    main()
