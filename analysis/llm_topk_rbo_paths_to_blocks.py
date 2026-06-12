#!/usr/bin/env python3
"""Cross-seed RBO at top-100 logical blocks, computed via paths->collapse.

For each rewriter-seed variant `<batch>/<bench>/<design>/<seed>/`, we read

    reports/<plat>/<top>/base/worst_logical_paths_top1000.<stage>.rpt

(produced by `eda_eval/variance/extract_paths_top1000.py`). The paths-mode
TCL masks `set_false_path` only at the bus-glob level, so unlike the
blocks-mode rpt it doesn't silently drop rows whose hashed signal names
share a digit pattern with an earlier row. We then:

  1. Substitute every `s_<12-hex>` back to its original name using the
     variant's `rename_map.json` (already on disk from
     `tmp/unhash_reports.py`).
  2. Collapse start_full/end_full to logical-block form with the python
     port of TCL's `block_base` (strip trailing `/pin`, strip the yosys
     `$...` cell marker, replace every digit run with positional letters).
  3. Dedupe in rank order, keep the first occurrence of each
     (start_base, end_base), truncate to top-K (default 100).

Then for each (design, stage) we compute RBO_EXT (Webber, Moffat, Zobel
2010, Eq. 32) over every C(8, 2) = 28 seed pair at p in {0.6, 0.9, 0.96}
and write:

  analysis/data/llm_topk_rbo_paths_to_blocks.csv         (per (design,
                                                          stage, p): n_seeds,
                                                          n_pairs, mean/std/
                                                          min/max RBO,
                                                          mean topk drops)
  analysis/data/llm_topk_rbo_paths_to_blocks_pairs.csv   (per (design,
                                                          stage, p, seed_a,
                                                          seed_b): RBO)
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import TypeVar

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BATCH = REPO / "eda_results" / "2026-06-05_00-44-57"
OUT_SUMMARY = REPO / "analysis" / "data" / "llm_topk_rbo_paths_to_blocks.csv"
OUT_PAIRS = REPO / "analysis" / "data" / "llm_topk_rbo_paths_to_blocks_pairs.csv"
TOP_K = 100
DEFAULT_PS = (0.6, 0.9, 0.96)
STAGES = ("1_synth", "6_final")
PATHS_RPT_NAME = "worst_logical_paths_top1000.{stage}.rpt"
UNHASHED_SUFFIX = ".unhashed"

_BLOCK_LETTERS = "IJKLMNOPQRSTUVWXYZ"
_DIGIT_RUN = re.compile(r"[0-9]+")
_TRAIL_PIN = re.compile(r"/[^/]+$")
_TRAIL_YOSYS = re.compile(r"\$[^/.]*$")
_HASH_RE = re.compile(r"s_[0-9a-f]{12}")

T = TypeVar("T")


def block_template(name: str) -> str:
    """Python port of TCL `block_template`: replace each digit run with the
    next positional placeholder letter (`I`, `J`, ...), `?` after `Z`."""
    out: list[str] = []
    pos = 0
    i = 0
    for m in _DIGIT_RUN.finditer(name):
        out.append(name[pos : m.start()])
        out.append(_BLOCK_LETTERS[i] if i < len(_BLOCK_LETTERS) else "?")
        pos = m.end()
        i += 1
    out.append(name[pos:])
    return "".join(out)


def block_base(full: str) -> str:
    """Strip trailing `/pin`, then yosys `$...` cell tail, then apply
    `block_template`."""
    name = _TRAIL_PIN.sub("", full)
    name = _TRAIL_YOSYS.sub("", name)
    return block_template(name)


def rbo_ext(s: list[T], t: list[T], p: float) -> float:
    """RBO_EXT (Webber, Moffat, Zobel 2010, Eq. 32) for two ranked lists."""
    if not s or not t:
        return 0.0
    if len(s) > len(t):
        s, t = t, s
    n_s, n_t = len(s), len(t)
    set_s, set_t = set(), set()
    weighted_sum = 0.0
    x_d = 0
    for d in range(1, n_s + 1):
        set_s.add(s[d - 1])
        set_t.add(t[d - 1])
        x_d = len(set_s & set_t)
        weighted_sum += (x_d / d) * (p ** (d - 1))
    x_s = x_d
    for d in range(n_s + 1, n_t + 1):
        set_t.add(t[d - 1])
        x_d = len(set_s & set_t)
        agreement = (x_d + x_s * (d - n_s) / n_s) / d
        weighted_sum += agreement * (p ** (d - 1))
    x_l = x_d
    base = (1 - p) * weighted_sum
    residual = ((x_l - x_s) / n_t + x_s / n_s) * (p**n_t)
    return base + residual


def _apply_unhash(text: str, inv_map: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(0)
        return inv_map.get(key, key)

    return _HASH_RE.sub(repl, text)


def _unhash_report(rpt: Path, inv_map: dict[str, str]) -> Path:
    """Apply inv_map to rpt text and write `<rpt>.unhashed`. Returns the
    unhashed path. No-op (still writes the file) if no substitutions occur."""
    out = rpt.with_suffix(rpt.suffix + UNHASHED_SUFFIX)
    out.write_text(_apply_unhash(rpt.read_text(), inv_map))
    return out


def _load_topk_blocks(rpt: Path, k: int) -> tuple[list[tuple[str, str]], int]:
    """Read the 6-col paths rpt, collapse to (block_base(start_full),
    block_base(end_full)) per row, dedupe in rank order, return top-k +
    the number of rows that were dropped as duplicates while building it."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    drops = 0
    for line in rpt.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        # cols: rank, slack, start, end, start_full, end_full
        start = block_base(parts[4])
        end = block_base(parts[5])
        key = (start, end)
        if key in seen:
            drops += 1
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= k:
            break
    return out, drops


def _find_variants(batch: Path, glob: str, stage: str) -> dict[str, dict[int, Path]]:
    """Walk `<batch>/<glob>/reports/.../base/worst_logical_paths_top1000.<stage>.rpt`
    and group by design name keyed by seed int.

    Returns {design_name: {seed: rpt_path}}.
    """
    out: dict[str, dict[int, Path]] = defaultdict(dict)
    rpt_glob = f"{glob}/reports/*/*/base/" + PATHS_RPT_NAME.format(stage=stage)
    seed_re = re.compile(r"_rewrite_seed_(\d+)$")
    for rpt in batch.glob(rpt_glob):
        # parts: <bench>/<design>/<seed_dir>/reports/<plat>/<top>/base/<rpt>
        parts = rpt.relative_to(batch).parts
        design = parts[1]
        seed_dir = parts[2]
        m = seed_re.search(seed_dir)
        if not m:
            continue
        out[design][int(m.group(1))] = rpt
    return out


def _variant_root(rpt: Path, batch: Path) -> Path:
    rel = rpt.relative_to(batch)
    return batch / rel.parts[0] / rel.parts[1] / rel.parts[2]


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--batch", type=Path, default=DEFAULT_BATCH)
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument(
        "--ps",
        type=float,
        nargs="+",
        default=list(DEFAULT_PS),
        help="RBO persistence values (multiple ok).",
    )
    ap.add_argument(
        "--variant-glob",
        default="*/*/reference_rewrite_seed_*",
        help="glob (relative to batch root) selecting variant dirs.",
    )
    ap.add_argument("--summary-out", type=Path, default=OUT_SUMMARY)
    ap.add_argument("--pairs-out", type=Path, default=OUT_PAIRS)
    ap.add_argument(
        "--no-unhash",
        action="store_true",
        help="reuse existing .unhashed files instead of rewriting them.",
    )
    args = ap.parse_args()

    batch: Path = args.batch
    summary_rows: list[dict] = []
    pair_rows: list[dict] = []

    for stage in STAGES:
        designs = _find_variants(batch, args.variant_glob, stage)
        for design in sorted(designs):
            seed_to_rpt = designs[design]
            seeds = sorted(seed_to_rpt)
            if len(seeds) < 2:
                continue

            ranked: dict[int, list[tuple[str, str]]] = {}
            dedup_drops: dict[int, int] = {}
            for sd in seeds:
                rpt = seed_to_rpt[sd]
                vroot = _variant_root(rpt, batch)
                if args.no_unhash:
                    unh = rpt.with_suffix(rpt.suffix + UNHASHED_SUFFIX)
                    if not unh.is_file():
                        unh = rpt  # nothing to do, paths rpt has no hashes
                else:
                    map_path = vroot / "rename_map.json"
                    if not map_path.is_file():
                        print(
                            f"  WARN no rename_map for {vroot.relative_to(batch)}, "
                            f"using raw paths rpt"
                        )
                        unh = rpt
                    else:
                        inv_map = json.loads(map_path.read_text())
                        unh = _unhash_report(rpt, inv_map)
                ranking, drops = _load_topk_blocks(unh, args.top_k)
                ranked[sd] = ranking
                dedup_drops[sd] = drops

            lens = [len(ranked[sd]) for sd in seeds]
            mean_drops = sum(dedup_drops.values()) / len(dedup_drops)
            max_drops = max(dedup_drops.values())

            for p in args.ps:
                rbos: list[float] = []
                for a, b in itertools.combinations(seeds, 2):
                    la, lb = ranked[a], ranked[b]
                    n_eval = min(len(la), len(lb))
                    if n_eval == 0:
                        continue
                    val = rbo_ext(la[:n_eval], lb[:n_eval], p)
                    rbos.append(val)
                    pair_rows.append(
                        {
                            "design": design,
                            "stage": stage,
                            "p": p,
                            "seed_a": a,
                            "seed_b": b,
                            "n_eval": n_eval,
                            "rbo": val,
                        }
                    )
                if not rbos:
                    continue
                mean = sum(rbos) / len(rbos)
                summary_rows.append(
                    {
                        "design": design,
                        "stage": stage,
                        "p": p,
                        "n_seeds": len(seeds),
                        "n_pairs": len(rbos),
                        "top_k": args.top_k,
                        "min_list_len": min(lens),
                        "max_list_len": max(lens),
                        "mean_rbo": mean,
                        "std_rbo": _stddev(rbos),
                        "min_rbo": min(rbos),
                        "max_rbo": max(rbos),
                        "mean_dedup_drops": mean_drops,
                        "max_dedup_drops": max_drops,
                    }
                )

    summary_rows.sort(key=lambda r: (r["design"], r["stage"], r["p"]))
    pair_rows.sort(
        key=lambda r: (r["design"], r["stage"], r["p"], r["seed_a"], r["seed_b"])
    )

    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    if summary_rows:
        with args.summary_out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)
    if pair_rows:
        with args.pairs_out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(pair_rows[0].keys()))
            w.writeheader()
            w.writerows(pair_rows)

    print(
        f"pairwise top-{args.top_k} logical-block RBO_EXT across seeds, "
        f"via paths->collapse (1000-deep paths)\n"
    )
    print(
        f"{'design':<16}  {'stage':<8}  {'p':>5}  {'n_p':>4}  "
        f"{'mean':>6}  {'std':>6}  {'min':>6}  {'max':>6}  "
        f"{'lenmin':>6}  {'drops':>5}"
    )
    print("-" * 88)
    for r in summary_rows:
        print(
            f"{r['design']:<16}  {r['stage']:<8}  {r['p']:>5.2f}  "
            f"{r['n_pairs']:>4}  "
            f"{r['mean_rbo']:>6.3f}  {r['std_rbo']:>6.3f}  "
            f"{r['min_rbo']:>6.3f}  {r['max_rbo']:>6.3f}  "
            f"{r['min_list_len']:>6}  {r['mean_dedup_drops']:>5.0f}"
        )

    if summary_rows:
        print(f"\nWrote {args.summary_out.relative_to(REPO)}")
        print(f"Wrote {args.pairs_out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
