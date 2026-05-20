#!/usr/bin/env python3
"""Find the effort/frequency-push knee in a logical_paths.rpt.

Each row of the input report groups timing paths sharing a (start_stem,
end_stem) and lists the modules touched. This script ranks those groups
by the cumulative slack headroom they unlock per unit of design effort,
where effort is taken as the number of distinct modules plus the number
of distinct start->end pairs that would need to be revisited.

Usage:
    uv run eda-eval/knee.py <path-to-logical_paths.rpt>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

COLS = [
    "rank",
    "worst_slack_ns",
    "best_slack_ns",
    "count",
    "total_loc",
    "start_stem",
    "end_stem",
    "modules",
]


def read_target_period_ns(report: Path) -> float:
    """Pull the `# target_period_ns\t<value>` metadata line from the
    report header."""
    for line in report.read_text().splitlines():
        if not line.startswith("#"):
            break
        parts = line.lstrip("#").strip().split()
        if len(parts) == 2 and parts[0] == "target_period_ns":
            return float(parts[1])
    raise ValueError(f"no '# target_period_ns' header line in {report}")


def analyse(report: Path) -> pd.DataFrame:
    target_period_ns = read_target_period_ns(report)
    df = pd.read_csv(report, sep="\t", comment="#", header=None, names=COLS)

    # Cumulative unique modules touched as we move down the ranked list.
    seen: set[str] = set()
    cum_modules: list[int] = []
    for mods in df["modules"].fillna(""):
        seen |= {m for m in mods.split(",") if m}
        cum_modules.append(len(seen))
    df["cum_modules"] = cum_modules

    # Cumulative number of unique module->module paths. Each stem's
    # owning module is everything before the trailing leaf identifier
    # (e.g. `core.keymem.prev_key1_reg` -> `core.keymem`); a top-level
    # stem like `reset_n` lives in the top module (empty prefix). Two
    # rows count as the same module-path iff their (src, dst) module
    # prefixes match exactly.
    def module_of(stem: str) -> str:
        return stem.rsplit(".", 1)[0] if "." in stem else ""

    paths_seen: set[tuple[str, str]] = set()
    cum_module_paths: list[int] = []
    for sp, ep in zip(df["start_stem"], df["end_stem"], strict=True):
        paths_seen.add((module_of(sp), module_of(ep)))
        cum_module_paths.append(len(paths_seen))
    df["cum_module_paths"] = cum_module_paths

    df["effort"] = df["cum_module_paths"]

    # Fixing rows 1..i lifts the binding slack to the next row's worst.
    # Assume fixing the entire list lands at 0 ns slack.
    baseline = df["worst_slack_ns"].iloc[0]
    next_slack = df["worst_slack_ns"].shift(-1).fillna(0.0)
    slack_gain_ns = next_slack - baseline

    # fmax_old = 1 / target_period_ns; new period after fixing the top i
    # paths is target_period_ns - slack_gain_ns. Report the fractional
    # uplift (new_fmax / old_fmax - 1) so 0.05 = a 5% frequency push.
    df["fmax_improvement"] = (
        target_period_ns / (target_period_ns - slack_gain_ns) - 1.0
    )
    df["improvement_per_effort"] = df["fmax_improvement"] / df["effort"]
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("report", type=Path, help="Path to a logical_paths.rpt file")
    args = ap.parse_args()

    if not args.report.is_file():
        ap.error(f"report not found: {args.report}")

    df = analyse(args.report)
    df.to_csv(sys.stdout, index=False)


if __name__ == "__main__":
    main()
