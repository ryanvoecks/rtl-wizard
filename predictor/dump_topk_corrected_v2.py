#!/usr/bin/env python3
"""Per-design dump of the v2 corrected-synth union pool used by
`correction_calibration.py` and `correction_per_design_summary.py`.

What's "v2":
  - β from median-of-medians with β_internal estimated and β_both derived:
      β_input    = +0.226
      β_output   = -0.323
      β_internal = -0.050
      β_both     = β_input + β_output - β_internal = -0.047
  - async quarantine uses endpoint pin name only (no shared-driver rule).
  - quarantine applied to SYNTH side only; route ranking is unfiltered.

Per row, in corrected-synth rank order:
  rank_synth_corr  -- rank under (synth_slack + β[port_class]) over kept synth pool
  rank_route       -- rank under raw route slack over the FULL union (no filter)
  rank_delta       -- rank_route - rank_synth_corr (positive = synth says worse
                      than route does; negative = synth says better)
  slack_synth_ns   -- raw synth slack from the union TSV
  beta_ns          -- per-class correction added to synth slack
  slack_synth_corr_ns
  slack_route_ns
  port_cat         -- input_only / output_only / both / internal
  end_pin          -- pin name after the last '/' in end_full (empty = port)
  async_endpoint   -- Y if end_pin not in {D}; otherwise N
  in_synth_topk    -- Y if rank_synth_corr <= TOP_K
  in_route_topk    -- Y if rank_route <= TOP_K
  start, end       -- collapsed bus stems
  start_full, end_full

Async paths are appended at the bottom of the file with empty
rank_synth_corr (they don't appear in the corrected-synth ranking) but
still get rank_route (route doesn't drop them).

Inputs:
  - analysis/data/path_slack_union/<design>.tsv
  - analysis/data/logical_blocks/<design>__{1_synth,6_final}.rpt

Outputs:
  - analysis/data/topk_corrected_v2/<design>.tsv

Usage:
    uv run --with pandas --with numpy --with statsmodels --with scipy \\
        python analysis/dump_topk_corrected_v2.py --design verilog_axi
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from predictor.correction_calibration import (  # noqa: E402
    CAT_BOTH, CAT_INPUT, CAT_INTERNAL, CAT_OUTPUT, UNION_DIR,
    _bus_stem_collapsed, _endpoint_pin, _is_async_pin, _load_ports,
    _port_class_for, _read_full_name_map, _read_union,
)

OUT_DIR = REPO / "predictor" / "data" / "dumps_v2"
TOP_K = 100

BETAS = {
    CAT_INPUT: +0.226,
    CAT_OUTPUT: -0.323,
    CAT_INTERNAL: -0.050,
    CAT_BOTH: +0.226 + (-0.323) - (-0.050),  # = -0.047
}


def _process(design: str, out_dir: Path) -> Path | None:
    union_path = UNION_DIR / f"{design}.tsv"
    if not union_path.is_file():
        return None
    union = _read_union(union_path)
    if union.empty:
        return None
    ports = _load_ports(design)
    full_map = _read_full_name_map(design)

    rows = []
    for _, u in union.iterrows():
        key = (u["start"], u["end"])
        stage_fulls = full_map.get(key, {})
        if stage_fulls:
            sf, ef = next(iter(stage_fulls.values()))
            end_fulls = [v[1] for v in stage_fulls.values()]
        else:
            sf, ef = key
            end_fulls = []
        is_async = any(_is_async_pin(e) for e in end_fulls)
        port_cat = _port_class_for(sf, ef, ports)
        beta = BETAS.get(port_cat, 0.0)
        rows.append(
            {
                "key": key,
                "slack_synth_ns": float(u["slack_synth_ns"]),
                "slack_route_ns": float(u["slack_route_ns"]),
                "start_full": sf,
                "end_full": ef,
                "end_pin": _endpoint_pin(ef),
                "port_cat": port_cat,
                "async_endpoint": "Y" if is_async else "N",
                "kept": "N" if is_async else "Y",
                "beta_ns": 0.0 if is_async else beta,
                "slack_synth_corr_ns": (
                    None if is_async else float(u["slack_synth_ns"]) + beta
                ),
            }
        )

    # Synth ranking: kept pool only, sorted by corrected synth slack ASC.
    kept = [r for r in rows if r["kept"] == "Y"]
    kept.sort(key=lambda r: r["slack_synth_corr_ns"])
    for i, r in enumerate(kept, start=1):
        r["rank_synth_corr"] = i
        r["in_synth_topk"] = "Y" if i <= TOP_K else "N"

    # Route ranking: ALL union pairs, no filter, sorted by route slack ASC.
    route_sorted = sorted(rows, key=lambda r: r["slack_route_ns"])
    for i, r in enumerate(route_sorted, start=1):
        r["rank_route"] = i
        r["in_route_topk"] = "Y" if i <= TOP_K else "N"

    for r in rows:
        if "rank_synth_corr" in r:
            r["rank_delta"] = r["rank_route"] - r["rank_synth_corr"]

    out = out_dir / f"{design}.tsv"
    cols = [
        "rank_synth_corr", "rank_route", "rank_delta",
        "slack_synth_ns", "beta_ns", "slack_synth_corr_ns", "slack_route_ns",
        "port_cat", "end_pin", "async_endpoint", "kept",
        "in_synth_topk", "in_route_topk",
        "start", "end", "start_full", "end_full",
    ]

    def _fmt(v) -> str:
        if v is None or v == "":
            return ""
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write("# v2 corrected-synth union dump\n")
        f.write(f"# design\t{design}\n")
        f.write(
            f"# n_union\t{len(rows)}  n_kept\t{len(kept)}  "
            f"n_dropped\t{len(rows) - len(kept)}\n"
        )
        f.write(
            "# betas\t"
            f"input={BETAS[CAT_INPUT]:+.3f} "
            f"output={BETAS[CAT_OUTPUT]:+.3f} "
            f"internal={BETAS[CAT_INTERNAL]:+.3f} "
            f"both={BETAS[CAT_BOTH]:+.3f} (derived)\n"
        )
        f.write("\t".join(cols) + "\n")
        # Kept rows first, ordered by rank_synth_corr; dropped (async) at end
        # ordered by their rank_route (so the worst-route async paths come first
        # within the dropped block, useful for debugging the filter).
        dropped = [r for r in rows if r["kept"] == "N"]
        dropped.sort(key=lambda r: r["rank_route"])
        for r in kept + dropped:
            line = []
            for c in cols:
                if c == "start":
                    line.append(r["key"][0])
                elif c == "end":
                    line.append(r["key"][1])
                else:
                    line.append(_fmt(r.get(c, "")))
            f.write("\t".join(line) + "\n")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--design",
        action="append",
        default=None,
        help="repeatable; restrict to these designs (default: all)",
    )
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    designs = args.design or sorted(p.stem for p in UNION_DIR.glob("*.tsv"))
    print(f"{'design':<16}  out")
    print("-" * 70)
    for d in designs:
        out = _process(d, args.out_dir)
        if out is None:
            print(f"{d:<16}  (no union TSV)")
            continue
        try:
            rel = out.relative_to(REPO)
        except ValueError:
            rel = out
        print(f"{d:<16}  {rel}")


if __name__ == "__main__":
    main()
