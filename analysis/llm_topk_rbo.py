#!/usr/bin/env python3
"""Per-design Rank-Biased Overlap of top-K paths, synth vs route.

RBO (Webber, Moffat, Zobel 2010), RBO_EXT variant at persistence p, is a
top-weighted set similarity: an item that appears in both lists' first
ranks contributes heavily, an item at depth 50 in one list and depth 70
in the other contributes only a little. RBO does not require the two
lists to share items, which makes it the right tool when membership
disagreement is part of what we want to measure (the long, non-conjoint
case).

Boundary to keep in mind: for short fully-enumerated universes where the
two lists share most or all of the same items, RBO_EXT's residual term
`(X_k/k) * p^k` saturates near 1.0 by construction (membership is
identical). RBO cannot distinguish "perfectly preserved short ranking"
from "scrambled short ranking" in that regime -- a correlation metric
over the shared items is the right complement for those cases, but is
not computed here.

Lists are truncated to min(|S|, |T|) so the EXT residual stays
equal-length and well-defined; the Eq. 32 uneven-list carry-forward is
not implemented. n_synth, n_route, and n_common are reported alongside
rbo so list-length and membership signals are not smuggled into the
similarity score.

Inputs:
  - analysis/data/logical_<kind>/<design>__{1_synth,6_final}.rpt
Outputs:
  - analysis/data/llm_topk_rbo_<kind>.csv
  - stdout table
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import TypeVar

REPO = Path(__file__).resolve().parents[1]
PATHS_DIR_TMPL = REPO / "analysis" / "data" / "logical_{kind}"
OUT_CSV_TMPL = REPO / "analysis" / "data" / "llm_topk_rbo_{kind}.csv"
RET_CSV = REPO / "analysis" / "data" / "llm_retention.csv"
TOP_K = 100
DEFAULT_P = 0.9
KINDS = ("paths", "blocks")
DEFAULT_KIND = "paths"

T = TypeVar("T")


def _load_ranked(rpt: Path, k: int) -> list[tuple[str, str]]:
    """Top-k (start, end) pairs in rank order (worst slack first), deduped."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in rpt.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        key = (parts[2], parts[3])
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= k:
            break
    return out


def rbo_ext(s: list[T], t: list[T], p: float) -> float:
    """RBO_EXT (Webber, Moffat, Zobel 2010, Eq. 32) for two equal-length
    ranked lists."""

    if not s or not t:
        return 0.0
    if len(s) > len(t):
        s, t = t, s
    n_s, n_t = len(s), len(t)

    set_s, set_t = set(), set()
    weighted_sum = 0.0
    x_d = 0
    # Phase 1: both lists present, depth 1..n_s
    for d in range(1, n_s + 1):
        set_s.add(s[d - 1])
        set_t.add(t[d - 1])
        x_d = len(set_s & set_t)
        weighted_sum += (x_d / d) * (p ** (d - 1))
    x_s = x_d  # overlap at the short list's end

    # Phase 2: only t present, depth n_s+1..n_t.
    # Eq. 32 carry-forward: agreement at depth d is
    #   A_d = (x_d + x_s * (d - n_s) / n_s) / d
    for d in range(n_s + 1, n_t + 1):
        set_t.add(t[d - 1])
        x_d = len(set_s & set_t)
        agreement = (x_d + x_s * (d - n_s) / n_s) / d
        weighted_sum += agreement * (p ** (d - 1))
    x_l = x_d

    base = (1 - p) * weighted_sum
    # WMZ Eq. 32 residual for uneven lists
    residual = ((x_l - x_s) / n_t + x_s / n_s) * (p**n_t)
    return base + residual


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument(
        "--p",
        type=float,
        default=DEFAULT_P,
        help="RBO persistence (0..1); higher = deeper weighting",
    )
    ap.add_argument(
        "--kind",
        choices=list(KINDS),
        default=DEFAULT_KIND,
        help="which dedup level to read: 'paths' or 'blocks'",
    )
    ap.add_argument(
        "--paths-dir",
        type=Path,
        default=None,
        help="override input dir (default: analysis/data/logical_<kind>)",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--ret", type=Path, default=RET_CSV)
    args = ap.parse_args()
    k, p = args.top_k, args.p
    kind: str = args.kind
    paths_dir: Path = args.paths_dir or Path(str(PATHS_DIR_TMPL).format(kind=kind))
    out_csv: Path = args.out or Path(str(OUT_CSV_TMPL).format(kind=kind))

    labels: dict[str, str] = {}
    with args.ret.open() as f:
        for raw in csv.DictReader(f):
            row = {(kk or "").strip(): (vv or "").strip() for kk, vv in raw.items()}
            labels[row["design"]] = row["label"]

    rows: list[dict] = []
    for synth_rpt in sorted(paths_dir.glob("*__1_synth.rpt")):
        design = synth_rpt.name.split("__")[0]
        route_rpt = paths_dir / f"{design}__6_final.rpt"
        if not route_rpt.is_file():
            continue
        synth = _load_ranked(synth_rpt, k)
        route = _load_ranked(route_rpt, k)
        n_common = len(set(synth) & set(route))

        # RBO is computed on an equal-length truncation so the EXT residual
        # is well-defined.
        n_eval = min(len(synth), len(route))
        rbo = rbo_ext(synth[:n_eval], route[:n_eval], p) if n_eval else 0.0

        rows.append(
            {
                "design": design,
                "label": labels.get(design, "?"),
                "kind": kind,
                "top_k": k,
                "p": p,
                "n_synth": len(synth),
                "n_route": len(route),
                "n_common": n_common,
                "rbo": rbo,
            }
        )

    rows.sort(key=lambda r: (r["label"], -r["rbo"]))

    print(f"top-{k} logical-{kind} RBO_EXT (p={p}), synth vs route\n")
    print(
        f"{'design':<16}  {'label':<10}  {'n_s':>4}  {'n_r':>4}  "
        f"{'n_ovl':>5}  {'rbo':>6}"
    )
    print("-" * 56)
    for r in rows:
        print(
            f"{r['design']:<16}  {r['label']:<10}  "
            f"{r['n_synth']:>4}  {r['n_route']:>4}  {r['n_common']:>5}  "
            f"{r['rbo']:>6.3f}"
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with out_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {out_csv.relative_to(REPO)}")


if __name__ == "__main__":
    main()
