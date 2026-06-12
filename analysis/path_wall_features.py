#!/usr/bin/env python3
"""Per-design pre-placement "path wall" features (Q2 predictors).

For each design and each of three slack-pool sources (physical critical
paths, collapsed logical paths, collapsed logical blocks) at the
post-synth stage, sorts entries by slack ascending and emits four
per-design CSVs covering the predictor families named for "how much
will PnR reshuffle the ranking":

  1. Slack-histogram density near WNS at synth -- fraction of paths
     within X% of |WNS| of WNS, for several X.
  2. Near-critical path count normalised by post-route stdcell count
     (1k cells), for the same X bands.
  3. Slack-histogram entropy/flatness at synth, at several bin counts.
  4. Top-k slack spread: range and coefficient of variation of the k
     worst slacks, both absolute and normalised by |WNS|.

The post-route stdcell count (from the baseline `6_report.json`) is
used as the size denominator for (2); it is a one-line lookup off the
existing ORFS cache.

Inputs:
  - analysis/data/critical_paths/<design>__1_synth.tsv   (physical)
  - analysis/data/logical_paths/<design>__1_synth.rpt    (collapsed)
  - analysis/data/logical_blocks/<design>__1_synth.rpt   (collapsed)
  - baseline ORFS cache (for stdcell_count)

Outputs (per source; physical has no suffix, logical variants are
suffixed `_logical_paths` / `_logical_blocks`):
  - analysis/data/path_wall_density[...].csv
  - analysis/data/path_wall_count_per_kcell[...].csv
  - analysis/data/path_wall_entropy[...].csv
  - analysis/data/path_wall_topk_spread[...].csv

Usage:
    uv run python analysis/path_wall_features.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

DATA_DIR = REPO / "analysis" / "data"
STAGE = "1_synth"

DENSITY_BANDS = (0.01, 0.05, 0.10, 0.20)  # X% of |WNS| above WNS
ENTROPY_BINS = (10, 20, 50)
TOPK_VALUES = (10, 50, 100)

# (label, input dir, file ext, slack column index, output filename suffix)
SOURCES: tuple[tuple[str, Path, str, int, str], ...] = (
    ("critical_paths", DATA_DIR / "critical_paths", ".tsv", 0, ""),
    ("logical_paths", DATA_DIR / "logical_paths", ".rpt", 1, "_logical_paths"),
    ("logical_blocks", DATA_DIR / "logical_blocks", ".rpt", 1, "_logical_blocks"),
)


def _read_slacks(path: Path, slack_col: int) -> list[float]:
    """Return all entry slacks (ns), sorted ascending (WNS first)."""
    out: list[float] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) <= slack_col:
            continue
        try:
            out.append(float(parts[slack_col]))
        except ValueError:
            continue
    out.sort()
    return out


def _stdcell_count(design_name: str) -> int | None:
    """Pull post-route stdcell count from the cached baseline 6_report.json."""
    for target in all_targets:
        if target.design.name != design_name:
            continue
        run = RunConfig(
            synth_target=target,
            output_dir=EDA_RUNS / "_dummy_baseline",
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=1,
        )
        cands = list(cache_path(run).glob("logs/nangate45/*/base/6_report.json"))
        if not cands:
            return None
        d = json.loads(cands[0].read_text())
        v = d.get("finish__design__instance__count__stdcell")
        return int(v) if v is not None else None
    return None


def _density_band(slacks: list[float], wns: float, frac: float) -> int:
    """Count of paths within frac*|WNS| above WNS."""
    threshold = wns + abs(wns) * frac
    # slacks are sorted ascending; could bisect, but linear is fine at <=1k.
    return sum(1 for s in slacks if s <= threshold)


def _entropy(slacks: list[float], bins: int) -> float:
    lo, hi = slacks[0], slacks[-1]
    if hi <= lo:
        return 0.0
    hist = [0] * bins
    span = hi - lo
    for s in slacks:
        i = min(int((s - lo) / span * bins), bins - 1)
        hist[i] += 1
    n = sum(hist)
    return -sum((h / n) * math.log2(h / n) for h in hist if h > 0)


def _mean_std(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return m, math.sqrt(var)


def _write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    try:
        shown = path.relative_to(REPO)
    except ValueError:
        shown = path
    print(f"Wrote {shown} ({len(rows)} designs)")


def _process_source(
    label: str, in_dir: Path, ext: str, slack_col: int, suffix: str
) -> None:
    files = sorted(in_dir.glob(f"*__{STAGE}{ext}"))
    if not files:
        print(f"skip {label}: no inputs under {in_dir}", file=sys.stderr)
        return
    print(f"\n== {label} ({len(files)} designs from {in_dir.relative_to(REPO)}) ==")

    density_rows: list[dict] = []
    count_rows: list[dict] = []
    entropy_rows: list[dict] = []
    topk_rows: list[dict] = []

    for infile in files:
        design = infile.name.removesuffix(f"__{STAGE}{ext}")
        slacks = _read_slacks(infile, slack_col)
        if not slacks:
            print(f"skip {design}: empty pool", file=sys.stderr)
            continue
        n = len(slacks)
        wns = slacks[0]
        stdcell = _stdcell_count(design)

        # (1) Density near WNS.
        d_row: dict = {"design": design, "n_paths": n, "wns_ns": f"{wns:.6f}"}
        for frac in DENSITY_BANDS:
            tag = f"{int(frac * 100)}pct"
            cnt = _density_band(slacks, wns, frac)
            d_row[f"density_at_{tag}"] = f"{cnt / n:.6f}"
            d_row[f"count_at_{tag}"] = cnt
        density_rows.append(d_row)

        # (2) Count per kilo-cell.
        c_row: dict = {
            "design": design,
            "n_paths": n,
            "wns_ns": f"{wns:.6f}",
            "stdcell_count": stdcell if stdcell is not None else "",
        }
        for frac in DENSITY_BANDS:
            tag = f"{int(frac * 100)}pct"
            cnt = _density_band(slacks, wns, frac)
            c_row[f"count_at_{tag}"] = cnt
            if stdcell:
                c_row[f"count_at_{tag}_per_kcell"] = f"{cnt / (stdcell / 1000):.6f}"
            else:
                c_row[f"count_at_{tag}_per_kcell"] = ""
        count_rows.append(c_row)

        # (3) Entropy at several bin counts.
        e_row: dict = {
            "design": design,
            "n_paths": n,
            "wns_ns": f"{wns:.6f}",
            "slack_range_ns": f"{slacks[-1] - slacks[0]:.6f}",
        }
        for b in ENTROPY_BINS:
            ent = _entropy(slacks, b)
            e_row[f"entropy_bits_{b}bins"] = f"{ent:.6f}"
            # Normalised entropy: 1.0 == perfectly uniform, 0.0 == all one bin.
            denom = math.log2(b) if b > 1 else 1.0
            e_row[f"entropy_norm_{b}bins"] = f"{ent / denom:.6f}"
        entropy_rows.append(e_row)

        # (4) Top-k slack spread (tight cluster -> unstable ranking).
        t_row: dict = {
            "design": design,
            "n_paths": n,
            "wns_ns": f"{wns:.6f}",
        }
        for k in TOPK_VALUES:
            if k > n:
                t_row[f"spread_top{k}_ns"] = ""
                t_row[f"spread_top{k}_rel_wns"] = ""
                t_row[f"cv_top{k}"] = ""
                continue
            head = slacks[:k]
            spread = head[-1] - head[0]
            t_row[f"spread_top{k}_ns"] = f"{spread:.6f}"
            t_row[f"spread_top{k}_rel_wns"] = (
                f"{spread / abs(wns):.6f}" if wns != 0 else ""
            )
            mean, std = _mean_std(head)
            t_row[f"cv_top{k}"] = f"{std / abs(mean):.6f}" if mean != 0 else ""
        topk_rows.append(t_row)

        print(
            f"{design:>16}  n={n:4d}  wns={wns:+.4f}  "
            f"d10={float(d_row['density_at_10pct']):.3f}  "
            f"H20={float(e_row['entropy_bits_20bins']):.3f}  "
            f"spr100={t_row.get('spread_top100_ns', '')}"
        )

    _write_csv(
        DATA_DIR / f"path_wall_density{suffix}.csv",
        ["design", "n_paths", "wns_ns"]
        + [f"density_at_{int(b * 100)}pct" for b in DENSITY_BANDS]
        + [f"count_at_{int(b * 100)}pct" for b in DENSITY_BANDS],
        density_rows,
    )
    _write_csv(
        DATA_DIR / f"path_wall_count_per_kcell{suffix}.csv",
        ["design", "n_paths", "wns_ns", "stdcell_count"]
        + [f"count_at_{int(b * 100)}pct" for b in DENSITY_BANDS]
        + [f"count_at_{int(b * 100)}pct_per_kcell" for b in DENSITY_BANDS],
        count_rows,
    )
    _write_csv(
        DATA_DIR / f"path_wall_entropy{suffix}.csv",
        ["design", "n_paths", "wns_ns", "slack_range_ns"]
        + [f"entropy_bits_{b}bins" for b in ENTROPY_BINS]
        + [f"entropy_norm_{b}bins" for b in ENTROPY_BINS],
        entropy_rows,
    )
    _write_csv(
        DATA_DIR / f"path_wall_topk_spread{suffix}.csv",
        ["design", "n_paths", "wns_ns"]
        + [f"spread_top{k}_ns" for k in TOPK_VALUES]
        + [f"spread_top{k}_rel_wns" for k in TOPK_VALUES]
        + [f"cv_top{k}" for k in TOPK_VALUES],
        topk_rows,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--source",
        choices=[s[0] for s in SOURCES] + ["all"],
        default="all",
        help="which slack-pool source to process (default: all)",
    )
    args = ap.parse_args()

    for label, in_dir, ext, slack_col, suffix in SOURCES:
        if args.source not in ("all", label):
            continue
        _process_source(label, in_dir, ext, slack_col, suffix)


if __name__ == "__main__":
    main()
