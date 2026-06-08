#!/usr/bin/env python3
"""Per-design scatter of slack delta vs. raw synth slack, with async/non-DFF-D
paths DROPPED entirely (data-pin endpoints only).

Same classification as `plot_path_slack_delta_async_origin_vs_slack.py` --
yellow if endpoint pin is non-D OR the startpoint's driver also fans out to
a non-D destination elsewhere -- but this variant just omits those rows from
the scatter instead of recolouring them. The remaining points are the
"genuine" data-to-data setup paths, useful for seeing the data-path slack
landscape without async-recovery / shared-driver outliers dominating the
plot (notably e203's leftmost band).

Inputs:
  - analysis/data/path_slack_union/<design>.tsv
  - analysis/data/logical_blocks/<design>__{1_synth,6_final}.rpt
  - analysis/data/critical_paths/<design>__{1_synth,6_final}.tsv

Outputs:
  - analysis/plots/path_slack_delta_async_filtered_vs_slack.png

Usage:
    uv run --with matplotlib python analysis/plot_path_slack_delta_async_filtered_vs_slack.py
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
CP_DIR = REPO / "analysis" / "data" / "critical_paths"
OUT_PNG = REPO / "predictor" / "plots" / "output" / "path_slack_delta_async_filtered_vs_slack.png"
TEXT_COLOR = "#222222"

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
    CAT_NON_DATA: "async endpoint or shared driver",
}
CAT_ORDER = (CAT_NEITHER, CAT_INPUT, CAT_OUTPUT, CAT_BOTH, CAT_NON_DATA)

_PORT_DECL = re.compile(r"^\s*(input|output|inout)\s+(?:\[[^\]]+\]\s+)?(\w+)\s*;")
_BUS_INDEX_TAIL = re.compile(r"\[[A-Z0-9]+\]$")
# All bracketed indices, used to reduce a full name like `key_mem[12][78]` to
# the bare register stem `key_mem` for cross-source matching.
_BUS_INDEX_ALL = re.compile(r"\[[A-Z0-9]+\]")


def _bus_stem_collapsed(name: str) -> str:
    return _BUS_INDEX_TAIL.sub("", name)


def _bus_stem_full(full_name: str) -> str:
    """Canonicalise a startpoint/endpoint full name to its bus stem.

    Strip pin suffix (`/Q`), yosys cell-type tail (`$_DFFE_PN0P_`), and every
    bracketed index. The result matches what the collapsed union TSV ends up
    with after the same transformation, so the set of "async-driving" stems
    built from critical_paths can be looked up against either the union row
    or the rpt's `start_full`.
    """
    name = full_name.rsplit("/", 1)[0] if "/" in full_name else full_name
    name = name.split("$", 1)[0]
    name = _BUS_INDEX_ALL.sub("", name)
    return name


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
    return ports.get(_bus_stem_collapsed(full_name))


def _is_input(full_name: str, ports: dict[str, str]) -> bool:
    return _direction(full_name, ports) in ("input", "inout")


def _is_output(full_name: str, ports: dict[str, str]) -> bool:
    return _direction(full_name, ports) in ("output", "inout")


def _endpoint_pin(end_full: str) -> str | None:
    if "/" not in end_full:
        return None
    return end_full.rsplit("/", 1)[-1]


def _is_non_data_pin(end_full: str) -> bool:
    pin = _endpoint_pin(end_full)
    return pin is not None and pin not in DATA_ENDPOINT_PINS


def _is_non_data(end_fulls: list[str]) -> bool:
    return any(_is_non_data_pin(ef) for ef in end_fulls)


def _async_driving_stems(design_name: str) -> set[str]:
    """Bus stems of startpoints that drive AT LEAST ONE non-D endpoint.

    Built from the bit-level critical_paths pool (both stages OR'd) so we see
    every register's full fanout, not just its top-100 representative. A path
    whose own endpoint is a D pin (or even a primary output port) still
    inherits async-recovery pressure if its driver register also feeds a
    clock-gate / reset / scan pin -- the driver is constrained by the
    tightest sibling arc, and that propagates to all of its outgoing paths.
    """
    stems: set[str] = set()
    for stage in ("1_synth", "6_final"):
        cp = CP_DIR / f"{design_name}__{stage}.tsv"
        if not cp.is_file():
            continue
        for line in cp.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            sp, ep = parts[1], parts[2]
            if _is_non_data_pin(ep):
                stems.add(_bus_stem_full(sp))
    return stems


def _classify(
    start_full: str,
    end_full: str,
    end_fulls: list[str],
    ports: dict[str, str],
    async_starts: set[str],
) -> str:
    if _is_non_data(end_fulls):
        return CAT_NON_DATA
    if _bus_stem_full(start_full) in async_starts:
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
        for cat in CAT_ORDER:
            xs = [r["slack_synth"] for r in rows if r["category"] == cat]
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
            ax.set_xlabel("synth slack (ns)")
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
        async_starts = _async_driving_stems(name)
        rows = []
        missing_full = 0
        for s, e, ss, sr in union:
            stage_fulls = full_map.get((s, e), {})
            if not stage_fulls:
                start_full, end_full = s, e
                end_fulls: list[str] = []
                missing_full += 1
            else:
                start_full, end_full = next(iter(stage_fulls.values()))
                end_fulls = [v[1] for v in stage_fulls.values()]
            category = _classify(
                start_full, end_full, end_fulls, ports, async_starts
            )
            # Drop async/shared-driver paths entirely -- this plot is the
            # data-pin-only view.
            if category == CAT_NON_DATA:
                continue
            rows.append(
                {
                    "start": s,
                    "end": e,
                    "start_full": start_full,
                    "end_full": end_full,
                    "slack_synth": ss,
                    "slack_route": sr,
                    "category": category,
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
