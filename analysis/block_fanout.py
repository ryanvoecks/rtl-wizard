#!/usr/bin/env python3
"""Per-design average net-fanout of the top-N worst logical blocks.

Reads per-design TSVs cached by `extract_block_fanout.py` (one row per
worst-slack logical-block rank, columns include `mean_fanout`,
`max_fanout`, `n_cells`, `n_output_nets`) and aggregates to one CSV
row per design with:

    design                  -- design name
    n_ranks                 -- number of ranks actually extracted (<= top-N)
    total_cells             -- sum of n_cells across ranks
    total_output_nets       -- sum of n_output_nets across ranks
    avg_block_mean_fanout   -- mean over ranks of per-rank mean_fanout
                               (each block weighted equally)
    pooled_mean_fanout      -- sum(mean_fanout * n_output_nets) / sum(n_output_nets)
                               (each output net weighted equally, so big
                               buses dominate -- closer to "what fanout
                               does the average sink see")
    median_block_mean_fanout
    p90_block_mean_fanout
    max_block_max_fanout    -- worst-case single-net fanout across the top-N
    max_block_sum_fanout    -- worst-case per-block total fanout (sum of net
                               fanouts within a block) across the top-N
    p90_top1000_path_net_fanout
                            -- 90th-percentile fanout across the per-path
                               decomposition of the top-1000 raw worst-slack
                               timing paths (extract_top_path_net_fanouts.py).
                               Reads as "the worst 10% of logic nets on the
                               top-1000 critical paths exceed this fanout".
                               One row per (path, output-net-on-path); a net
                               on K of the 1000 paths is counted K times.
    top_path_net_obs_count  -- number of (path, net) observations behind the
                               p90 (sanity check; blank if no TSV cached).

Both equal- and pooled-weight aggregates are reported because a single
wide bus block (e.g. a 1k-bit memory) can dominate the pooled view.

Output: analysis/data/block_fanout.csv

Usage:
    uv run python analysis/block_fanout.py [--top-n 25]
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IN_DIR = REPO / "analysis" / "data" / "block_fanout"
TOP_PATH_NET_DIR = REPO / "analysis" / "data" / "top_path_net_fanouts"
OUT_CSV = REPO / "analysis" / "data" / "block_fanout.csv"
STAGE = "1_synth"


def _read_top_path_net_fanouts(tsv: Path) -> list[int]:
    """Return per-(path, net) fanout values from a top_path_net_fanouts TSV.
    Columns: path_idx, slack_ns, net_name, fanout."""
    fanouts: list[int] = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            fanout = int(parts[3])
        except ValueError:
            continue
        fanouts.append(fanout)
    return fanouts


def _read_rows(tsv: Path) -> list[dict]:
    rows: list[dict] = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        rows.append(
            {
                "rank": int(parts[0]),
                "slack_ns": float(parts[1]),
                "start": parts[2],
                "end": parts[3],
                "n_cells": int(parts[4]),
                "n_output_nets": int(parts[5]),
                "mean_fanout": float(parts[6]),
                "max_fanout": int(parts[7]),
            }
        )
    return rows


def summarise(rows: list[dict], top_n: int) -> dict:
    rows = rows[:top_n]
    if not rows:
        return {"n_ranks": 0}
    fanouts = [r["mean_fanout"] for r in rows]
    nets = [r["n_output_nets"] for r in rows]
    total_nets = sum(nets)
    pooled = (
        sum(r["mean_fanout"] * r["n_output_nets"] for r in rows) / total_nets
        if total_nets > 0
        else float("nan")
    )
    return {
        "n_ranks": len(rows),
        "total_cells": sum(r["n_cells"] for r in rows),
        "total_output_nets": total_nets,
        "avg_block_mean_fanout": statistics.mean(fanouts),
        "pooled_mean_fanout": pooled,
        "median_block_mean_fanout": statistics.median(fanouts),
        "p90_block_mean_fanout": (
            statistics.quantiles(fanouts, n=10)[-1]
            if len(fanouts) >= 10
            else max(fanouts)
        ),
        "max_block_max_fanout": max(r["max_fanout"] for r in rows),
        "max_block_sum_fanout": max(
            r["mean_fanout"] * r["n_output_nets"] for r in rows
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-n", type=int, default=25)
    ap.add_argument("--csv", type=Path, default=OUT_CSV)
    args = ap.parse_args()

    tsvs = sorted(IN_DIR.glob(f"*__{STAGE}.tsv"))
    if not tsvs:
        raise SystemExit(f"no input TSVs found under {IN_DIR}")

    rows: list[dict] = []
    for tsv in tsvs:
        design = tsv.name.removesuffix(f"__{STAGE}.tsv")
        s = summarise(_read_rows(tsv), args.top_n)
        s["design"] = design

        tpn_tsv = TOP_PATH_NET_DIR / f"{design}__{STAGE}.tsv"
        if tpn_tsv.is_file():
            tpn = _read_top_path_net_fanouts(tpn_tsv)
            if tpn:
                s["top_path_net_obs_count"] = len(tpn)
                s["p90_top1000_path_net_fanout"] = (
                    statistics.quantiles(tpn, n=10, method="inclusive")[-1]
                    if len(tpn) >= 10
                    else max(tpn)
                )
        rows.append(s)
        if s["n_ranks"]:
            print(
                f"{design:>16}  n={s['n_ranks']:2d}  "
                f"avg={s['avg_block_mean_fanout']:.3f}  "
                f"pooled={s['pooled_mean_fanout']:.3f}  "
                f"med={s['median_block_mean_fanout']:.3f}  "
                f"max={s['max_block_max_fanout']}"
            )
        else:
            print(f"{design:>16}  (empty)")

    cols = [
        "design",
        "n_ranks",
        "total_cells",
        "total_output_nets",
        "avg_block_mean_fanout",
        "pooled_mean_fanout",
        "median_block_mean_fanout",
        "p90_block_mean_fanout",
        "max_block_max_fanout",
        "max_block_sum_fanout",
        "p90_top1000_path_net_fanout",
        "top_path_net_obs_count",
    ]
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(
                [
                    r.get("design", ""),
                    r.get("n_ranks", 0),
                    r.get("total_cells", ""),
                    r.get("total_output_nets", ""),
                    f"{r['avg_block_mean_fanout']:.6f}" if r.get("n_ranks") else "",
                    f"{r['pooled_mean_fanout']:.6f}" if r.get("n_ranks") else "",
                    f"{r['median_block_mean_fanout']:.6f}" if r.get("n_ranks") else "",
                    f"{r['p90_block_mean_fanout']:.6f}" if r.get("n_ranks") else "",
                    r.get("max_block_max_fanout", ""),
                    f"{r['max_block_sum_fanout']:.0f}" if r.get("n_ranks") else "",
                    f"{r['p90_top1000_path_net_fanout']:.1f}"
                    if "p90_top1000_path_net_fanout" in r
                    else "",
                    r.get("top_path_net_obs_count", ""),
                ]
            )
    try:
        shown = args.csv.relative_to(REPO)
    except ValueError:
        shown = args.csv
    print(f"Wrote {shown}")


if __name__ == "__main__":
    main()
