#!/usr/bin/env python3
"""Paired significance tests on PnR fmax across feedback arms.

Three head-to-head tests, all on the same 16-design fixture and using PnR
fmax (MHz) as the metric, predict-minus-synth direction by default:

  (A) predict vs synth   -- the main question: does the predictor help?
  (B) pnr vs synth (H)   -- oracle headroom: does any feedback help?
  (C) each arm vs the 8-seed rewriter-sweep baseline -- sanity check.

The per-epoch fmax is read from `tmp/summary.json` at the `best_iter`
chosen by each arm's own per-edit CSV (`analysis/data/llm_per_edit_<arm>.csv`).
This matches the selection rule used by `analysis/llm_feedback_table.py`:
each arm picks its winner using its own feedback metric, not a globally
chosen one.

The per-design standardisation uses the pooled SD of the (n_synth + n_predict)
runs around their own arm means (4 df with n=3 each). The same pooled s_d
denominator is used for all three tests, so they're on comparable noise
units.

The hierarchical bootstrap uses a fixed-denominator variant (resample
designs and runs, but standardise by the originally-observed s_d). The
two-level n=3 inner bootstrap is unstable on s_d itself; fixing the
denominator is the textbook fix.

Outputs:
  - analysis/data/llm_significance_per_design.csv (per-design Delta, delta_std, h_d)
  - analysis/data/llm_significance_summary.csv    (one row per test)
  - stdout: formatted tables for all four sections

Usage:
    uv run python analysis/llm_significance_tests.py
    uv run python analysis/llm_significance_tests.py --bootstrap-iters 50000
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.stats import binomtest, ttest_1samp, wilcoxon

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "analysis" / "data"
SUMMARY_JSON = REPO / "tmp" / "summary.json"
VARIANCE_CSV = DATA / "variance.csv"
OUT_PER_DESIGN_CSV = DATA / "llm_significance_per_design.csv"
OUT_SUMMARY_CSV = DATA / "llm_significance_summary.csv"

PER_EDIT_CSVS = {
    "synth": DATA / "llm_per_edit_synth.csv",
    "predict": DATA / "llm_per_edit_predict.csv",
    "pnr": DATA / "llm_per_edit_pnr.csv",
}
ARMS = ("synth", "predict", "pnr")

DEFAULT_BOOTSTRAP_ITERS = 10_000
DEFAULT_BOOTSTRAP_SEED = 20260613

# Power-analysis constants for the MDE formula (Step 7).
# Two-sided alpha=0.05, power=1-beta=0.80 -> z_{1-alpha/2} + z_{1-beta} ~= 2.80.
POWER_Z_SUM = 2.80
N_RUNS_PER_CELL = 3


# Data loading


def fmax_mhz(period_ns: float, ws_ns: float) -> float | None:
    cp = period_ns - ws_ns
    return None if cp <= 0 else 1000.0 / cp


def load_summary(path: Path) -> dict:
    return json.loads(path.read_text())


def load_per_edit(path: Path) -> list[dict]:
    """One row per (design, epoch) with `best_iter`, skipping fail-token rows."""
    rows: list[dict] = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r["best_synth_pct"].startswith("fail") or r["best_pnr_pct"].startswith(
                "fail"
            ):
                continue
            rows.append(
                {
                    "design": r["design"],
                    "epoch": int(r["epoch"]),
                    "run": r["run"],
                    "best_iter": int(r["best_iter"]),
                }
            )
    return rows


def load_rewriter_baseline_pnr_mhz(path: Path) -> dict[str, float]:
    """8-seed rewriter-sweep mean PnR fmax (MHz) per design."""
    out: dict[str, float] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            short = r["design"].split("/", 1)[1]
            out[short] = float(r["pnr_fmax_mhz_mean"])
    return out


def fmax_at_iter(
    summary: dict, run: str, design: str, epoch: int, k: int
) -> float | None:
    """PnR fmax (MHz) at iter `k` for the (run, design, epoch) sample,
    or None if the iter is missing / partial-failure."""
    b = summary["baselines"].get(design)
    if not b or not b.get("present"):
        return None
    period = b["period_ns"]
    needle = f"|{design}|epoch_{epoch}"
    for key, iters in summary["samples"].items():
        if not key.startswith(run + "|") or not key.endswith(needle):
            continue
        row = iters.get(str(k)) or iters.get(k)
        if row is None or row.get("pnr_ws_ns") is None:
            return None
        return fmax_mhz(period, row["pnr_ws_ns"])
    return None


def collect_per_design(
    summary: dict, per_edit: dict[str, list[dict]]
) -> dict[str, dict[str, list[float]]]:
    """Build {design: {arm: [pnr_fmax_mhz, ...]}} from each arm's best-iter."""
    out: dict[str, dict[str, list[float]]] = {}
    for arm, rows in per_edit.items():
        for r in rows:
            fm = fmax_at_iter(
                summary, r["run"], r["design"], r["epoch"], r["best_iter"]
            )
            if fm is None:
                continue
            out.setdefault(r["design"], {a: [] for a in ARMS})[arm].append(fm)
    return out


# Statistics


def pooled_s_d(synth_runs: np.ndarray, predict_runs: np.ndarray) -> float:
    """Pooled SD around arm means, in MHz. Same definition used for all
    three head-to-head tests so they're on comparable noise units."""
    ss = ((synth_runs - synth_runs.mean()) ** 2).sum() + (
        (predict_runs - predict_runs.mean()) ** 2
    ).sum()
    df = (len(synth_runs) - 1) + (len(predict_runs) - 1)
    if df <= 0 or ss <= 0:
        raise ValueError("not enough runs for a non-degenerate s_d")
    return math.sqrt(ss / df)


@dataclass(frozen=True)
class HeadToHead:
    """Result of a paired-by-design head-to-head test on standardised delta_d."""

    name: str
    designs: tuple[str, ...]
    deltas_mhz: np.ndarray  # per-design Delta_d in MHz
    deltas_std: np.ndarray  # per-design delta_d in noise units
    wilcoxon_w: float
    wilcoxon_p: float
    t_stat: float
    t_p: float
    sign_pos: int
    sign_neg: int
    sign_p: float
    bs_mean_std: float
    ci_std_95: tuple[float, float]
    ci_std_90: tuple[float, float]
    bs_mean_mhz: float
    ci_mhz_95: tuple[float, float]
    bs_frac_pos_std: float

    @property
    def mean_delta_mhz(self) -> float:
        return float(self.deltas_mhz.mean())

    @property
    def median_delta_mhz(self) -> float:
        return float(np.median(self.deltas_mhz))

    @property
    def mean_delta_std(self) -> float:
        return float(self.deltas_std.mean())

    @property
    def median_delta_std(self) -> float:
        return float(np.median(self.deltas_std))


def _signed_rank_pvalue(deltas_std: np.ndarray) -> tuple[float, float]:
    """Wilcoxon W statistic (sum of positive-signed ranks) and two-sided p.

    scipy's wilcoxon returns min(W+, W-); we want W+ for matching the
    convention used in the conversation tables, so we reconstruct it.
    """
    p = float(wilcoxon(deltas_std, alternative="two-sided").pvalue)  # pyright: ignore[reportAttributeAccessIssue]
    abs_d = np.abs(deltas_std)
    # Average ranks for ties (mirrors scipy/wilcoxon's default tie handling).
    order = np.argsort(abs_d, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    i = 0
    n = len(abs_d)
    while i < n:
        j = i
        while j + 1 < n and abs_d[order[j + 1]] == abs_d[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2  # 1-based rank average
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_pos = float(ranks[deltas_std > 0].sum())
    return w_pos, p


def head_to_head(
    name: str,
    designs: Sequence[str],
    arm_a: Sequence[np.ndarray],
    arm_b: Sequence[np.ndarray],
    s_d: Sequence[float],
    *,
    n_boot: int,
    rng: np.random.Generator,
) -> HeadToHead:
    """Paired delta_d = mean(arm_b) - mean(arm_a) per design, standardised by s_d.

    Runs Wilcoxon (primary), one-sample t (companion), sign test (floor),
    and a hierarchical bootstrap with fixed-s_d denominator.
    """
    deltas_mhz = np.array([b.mean() - a.mean() for a, b in zip(arm_a, arm_b)])
    deltas_std = np.array([d / sd for d, sd in zip(deltas_mhz, s_d)])

    w_pos, w_p = _signed_rank_pvalue(deltas_std)
    t = ttest_1samp(deltas_std, 0)
    n_pos = int((deltas_std > 0).sum())
    n_neg = int((deltas_std < 0).sum())
    sign_p = float(binomtest(n_pos, n_pos + n_neg, p=0.5).pvalue)

    nd = len(designs)
    bs_std = np.empty(n_boot)
    bs_mhz = np.empty(n_boot)
    for it in range(n_boot):
        di = rng.integers(0, nd, nd)
        ds_std, ds_mhz = [], []
        for i in di:
            a = arm_a[i]
            b = arm_b[i]
            ab = a[rng.integers(0, len(a), len(a))]
            bb = b[rng.integers(0, len(b), len(b))]
            d_mhz = bb.mean() - ab.mean()
            ds_mhz.append(d_mhz)
            ds_std.append(d_mhz / s_d[i])
        bs_std[it] = float(np.mean(ds_std))
        bs_mhz[it] = float(np.mean(ds_mhz))
    ci_std_95 = (float(np.percentile(bs_std, 2.5)), float(np.percentile(bs_std, 97.5)))
    ci_std_90 = (float(np.percentile(bs_std, 5)), float(np.percentile(bs_std, 95)))
    ci_mhz_95 = (float(np.percentile(bs_mhz, 2.5)), float(np.percentile(bs_mhz, 97.5)))

    return HeadToHead(
        name=name,
        designs=tuple(designs),
        deltas_mhz=deltas_mhz,
        deltas_std=deltas_std,
        wilcoxon_w=w_pos,
        wilcoxon_p=w_p,
        t_stat=float(t.statistic),  # pyright: ignore[reportAttributeAccessIssue]
        t_p=float(t.pvalue),  # pyright: ignore[reportAttributeAccessIssue]
        sign_pos=n_pos,
        sign_neg=n_neg,
        sign_p=sign_p,
        bs_mean_std=float(bs_std.mean()),
        ci_std_95=ci_std_95,
        ci_std_90=ci_std_90,
        bs_mean_mhz=float(bs_mhz.mean()),
        ci_mhz_95=ci_mhz_95,
        bs_frac_pos_std=float((bs_std > 0).mean()),
    )


# Reporting helpers


def print_head_to_head(h: HeadToHead, *, label: str) -> None:
    """Match the in-conversation table style for the head-to-head tests."""
    print()
    print("=" * 70)
    print(f" {label}")
    print("=" * 70)
    print(f"  mean Delta_d (MHz)   = {h.mean_delta_mhz:+8.2f}")
    print(f"  median Delta_d (MHz) = {h.median_delta_mhz:+8.2f}")
    print(f"  mean delta_d (std)   = {h.mean_delta_std:+8.3f}")
    print()
    print(f"  Wilcoxon signed-rank  W = {h.wilcoxon_w:.1f}, p = {h.wilcoxon_p:.4f}")
    print(
        f"  paired t-test         t({len(h.designs) - 1}) = {h.t_stat:+.3f}, "
        f"p = {h.t_p:.4f}"
    )
    print(f"  sign test             {h.sign_pos}+ / {h.sign_neg}-,  p = {h.sign_p:.4f}")
    print()
    print(f"  bootstrap mean delta_std = {h.bs_mean_std:+.3f}")
    print(
        f"  bootstrap 95% CI delta_std = [{h.ci_std_95[0]:+.3f}, {h.ci_std_95[1]:+.3f}]"
    )
    print(
        f"  bootstrap 90% CI delta_std = [{h.ci_std_90[0]:+.3f}, {h.ci_std_90[1]:+.3f}]"
    )
    print(
        f"  bootstrap 95% CI Delta MHz = [{h.ci_mhz_95[0]:+.2f}, {h.ci_mhz_95[1]:+.2f}]"
    )
    print(f"  bootstrap fraction > 0     = {h.bs_frac_pos_std:.3f}")


def print_per_design_table(
    designs: Sequence[str],
    arm_runs: dict[str, list[np.ndarray]],
    s_d: Sequence[float],
    pred_vs_syn: HeadToHead,
    h_test: HeadToHead,
) -> None:
    """Per-design fmax means + Delta_d / delta_std for both head-to-heads."""
    print()
    print("Per-design table (PnR fmax MHz means, paired comparisons):")
    print()
    hdr = (
        f"  {'design':<16} {'mean_s':>8} {'mean_p':>8} {'mean_o':>8} {'s_d':>7} "
        f"{'pred-syn MHz':>12} {'pred-syn std':>12} {'oracle-syn std':>14}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for i, name in enumerate(designs):
        sr, pr, orun = arm_runs["synth"][i], arm_runs["predict"][i], arm_runs["pnr"][i]
        print(
            f"  {name:<16} {sr.mean():>8.2f} {pr.mean():>8.2f} {orun.mean():>8.2f} "
            f"{s_d[i]:>7.2f} {pred_vs_syn.deltas_mhz[i]:>+12.2f} "
            f"{pred_vs_syn.deltas_std[i]:>+12.3f} "
            f"{h_test.deltas_std[i]:>+14.3f}"
        )


# Arm vs baseline (Section C)


@dataclass(frozen=True)
class ArmVsBaseline:
    arm: str
    deltas_mhz: np.ndarray
    deltas_std: np.ndarray
    wilcoxon_w: float
    wilcoxon_p: float
    t_stat: float
    t_p: float
    sign_pos: int
    sign_neg: int
    sign_p: float
    bs_mean_std: float
    ci_std_95: tuple[float, float]
    bs_mean_mhz: float
    ci_mhz_95: tuple[float, float]
    bs_mean_pct: float
    ci_pct_95: tuple[float, float]


def arm_vs_baseline(
    arm: str,
    designs: Sequence[str],
    arm_runs: Sequence[np.ndarray],
    baseline_mhz: Sequence[float],
    s_d: Sequence[float],
    *,
    n_boot: int,
    rng: np.random.Generator,
) -> ArmVsBaseline:
    """arm-mean minus 8-seed rewriter baseline, paired by design."""
    deltas_mhz = np.array([a.mean() - bl for a, bl in zip(arm_runs, baseline_mhz)])
    deltas_std = np.array([d / sd for d, sd in zip(deltas_mhz, s_d)])

    w_pos, w_p = _signed_rank_pvalue(deltas_std)
    t = ttest_1samp(deltas_std, 0)
    n_pos = int((deltas_std > 0).sum())
    n_neg = int((deltas_std < 0).sum())
    sign_p = float(binomtest(n_pos, n_pos + n_neg, p=0.5).pvalue)

    nd = len(designs)
    bs_std = np.empty(n_boot)
    bs_mhz = np.empty(n_boot)
    bs_pct = np.empty(n_boot)
    for it in range(n_boot):
        di = rng.integers(0, nd, nd)
        ds_std, ds_mhz, ds_pct = [], [], []
        for i in di:
            a = arm_runs[i]
            ab = a[rng.integers(0, len(a), len(a))]
            d_mhz = ab.mean() - baseline_mhz[i]
            ds_mhz.append(d_mhz)
            ds_std.append(d_mhz / s_d[i])
            ds_pct.append(100.0 * (ab.mean() / baseline_mhz[i] - 1.0))
        bs_std[it] = float(np.mean(ds_std))
        bs_mhz[it] = float(np.mean(ds_mhz))
        bs_pct[it] = float(np.mean(ds_pct))

    return ArmVsBaseline(
        arm=arm,
        deltas_mhz=deltas_mhz,
        deltas_std=deltas_std,
        wilcoxon_w=w_pos,
        wilcoxon_p=w_p,
        t_stat=float(t.statistic),  # pyright: ignore[reportAttributeAccessIssue]
        t_p=float(t.pvalue),  # pyright: ignore[reportAttributeAccessIssue]
        sign_pos=n_pos,
        sign_neg=n_neg,
        sign_p=sign_p,
        bs_mean_std=float(bs_std.mean()),
        ci_std_95=(
            float(np.percentile(bs_std, 2.5)),
            float(np.percentile(bs_std, 97.5)),
        ),
        bs_mean_mhz=float(bs_mhz.mean()),
        ci_mhz_95=(
            float(np.percentile(bs_mhz, 2.5)),
            float(np.percentile(bs_mhz, 97.5)),
        ),
        bs_mean_pct=float(bs_pct.mean()),
        ci_pct_95=(
            float(np.percentile(bs_pct, 2.5)),
            float(np.percentile(bs_pct, 97.5)),
        ),
    )


def print_arm_vs_baseline_table(rows: list[ArmVsBaseline]) -> None:
    print()
    print("=" * 70)
    print(" (C) Each arm vs 8-seed rewriter baseline")
    print("=" * 70)
    hdr = (
        f"  {'arm':<8} {'mean Delta MHz':>15} {'mean delta std':>15} "
        f"{'mean uplift %':>14}  {'Wilcoxon p':>11}  {'sign':>8}  "
        f"{'95% CI delta':<22}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        sign = f"{r.sign_pos}+/{r.sign_neg}-"
        ci_str = f"[{r.ci_std_95[0]:+.3f}, {r.ci_std_95[1]:+.3f}]"
        print(
            f"  {r.arm:<8} {r.deltas_mhz.mean():>+15.2f} {r.deltas_std.mean():>+15.3f} "
            f"{r.bs_mean_pct:>+13.2f}%  {r.wilcoxon_p:>11.4f}  {sign:>8}  {ci_str:<22}"
        )
        print(
            f"  {'':<8} {'':<15} {'':<15} 95% CI uplift "
            f"[{r.ci_pct_95[0]:+.2f}%, {r.ci_pct_95[1]:+.2f}%]"
        )


# TOST + MDE (Step 6c / Step 7)


def tost_and_mde(H_test: HeadToHead, pred_vs_syn: HeadToHead, n_designs: int) -> None:
    print()
    print("=" * 70)
    print(" (D) TOST (equivalence test) and MDE / power prescription")
    print("=" * 70)
    H = H_test.bs_mean_std
    print(f"  oracle headroom H (bootstrap mean) = {H:+.3f}")
    print(
        f"  predict-vs-synth 90% CI on delta_std = [{pred_vs_syn.ci_std_90[0]:+.3f}, "
        f"{pred_vs_syn.ci_std_90[1]:+.3f}]"
    )
    print()
    if H <= 0:
        print("  H is not positive -- TOST not meaningful. Stopping.")
    else:
        margins = [
            (0.10, "0.10*H (strict) "),
            (0.25, "0.25*H (primary)"),
            (0.50, "0.50*H (loose)  "),
        ]
        ci_lo, ci_hi = pred_vs_syn.ci_std_90
        print(f"  {'margin':<18} {'band':>22}   verdict")
        for frac, label in margins:
            band = abs(frac * H)
            inside = (ci_lo >= -band) and (ci_hi <= band)
            if inside:
                verdict = "EQUIVALENT"
            elif ci_hi < -band or ci_lo > band:
                verdict = "DIFFERENT"
            else:
                verdict = "INCONCLUSIVE"
            print(f"  {label:<18} [{-band:+.3f}, {band:+.3f}]   {verdict}")

    se = math.sqrt(2.0 / N_RUNS_PER_CELL) / math.sqrt(n_designs)
    mde = POWER_Z_SUM * se
    print()
    print(f"  current setup: n_designs={n_designs}, n_runs={N_RUNS_PER_CELL} per cell")
    print(
        f"  SE(delta_bar) = sqrt(2/{N_RUNS_PER_CELL})/sqrt({n_designs}) = "
        f"{se:.3f} noise units"
    )
    print(
        f"  MDE (alpha=0.05, power=0.80) = {POWER_Z_SUM:.2f}*SE = {mde:.3f} noise units"
    )
    if H > 0:
        target = 0.25 * H
        # n_new = 2 / ((target / Z)^2 * n_designs)
        n_new = 2.0 / ((target / POWER_Z_SUM) ** 2 * n_designs)
        print(
            f"  MDE in oracle-headroom units = {mde / H:.2f}*H "
            f"(effects below {mde / H:.2f}*H were invisible)"
        )
        print(
            f"  to resolve 0.25*H = {target:.3f} noise units at n_designs="
            f"{n_designs}: need ~{n_new:.1f} runs per cell"
        )


# CSV outputs


def write_per_design_csv(
    path: Path,
    designs: Sequence[str],
    arm_runs: dict[str, list[np.ndarray]],
    s_d: Sequence[float],
    rewriter_pnr_mhz: dict[str, float],
    pred_vs_syn: HeadToHead,
    h_test: HeadToHead,
    arm_vs_baseline_rows: list[ArmVsBaseline],
) -> None:
    headers = [
        "design",
        "synth_mean_mhz",
        "predict_mean_mhz",
        "pnr_mean_mhz",
        "rewriter_baseline_mhz",
        "s_d_mhz",
        "delta_pred_minus_syn_mhz",
        "delta_pred_minus_syn_std",
        "h_d_oracle_minus_syn_std",
        "synth_vs_baseline_mhz",
        "synth_vs_baseline_std",
        "predict_vs_baseline_mhz",
        "predict_vs_baseline_std",
        "pnr_vs_baseline_mhz",
        "pnr_vs_baseline_std",
    ]
    rows: list[dict] = []
    by_arm = {r.arm: r for r in arm_vs_baseline_rows}
    for i, name in enumerate(designs):
        rows.append(
            {
                "design": name,
                "synth_mean_mhz": float(arm_runs["synth"][i].mean()),
                "predict_mean_mhz": float(arm_runs["predict"][i].mean()),
                "pnr_mean_mhz": float(arm_runs["pnr"][i].mean()),
                "rewriter_baseline_mhz": rewriter_pnr_mhz[name],
                "s_d_mhz": s_d[i],
                "delta_pred_minus_syn_mhz": float(pred_vs_syn.deltas_mhz[i]),
                "delta_pred_minus_syn_std": float(pred_vs_syn.deltas_std[i]),
                "h_d_oracle_minus_syn_std": float(h_test.deltas_std[i]),
                "synth_vs_baseline_mhz": float(by_arm["synth"].deltas_mhz[i]),
                "synth_vs_baseline_std": float(by_arm["synth"].deltas_std[i]),
                "predict_vs_baseline_mhz": float(by_arm["predict"].deltas_mhz[i]),
                "predict_vs_baseline_std": float(by_arm["predict"].deltas_std[i]),
                "pnr_vs_baseline_mhz": float(by_arm["pnr"].deltas_mhz[i]),
                "pnr_vs_baseline_std": float(by_arm["pnr"].deltas_std[i]),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


def write_summary_csv(
    path: Path,
    pred_vs_syn: HeadToHead,
    h_test: HeadToHead,
    arm_vs_baseline_rows: list[ArmVsBaseline],
) -> None:
    """One row per test (predict-vs-synth, oracle-vs-synth, each arm-vs-baseline)."""
    headers = [
        "test",
        "n_designs",
        "mean_delta_mhz",
        "median_delta_mhz",
        "mean_delta_std",
        "wilcoxon_w",
        "wilcoxon_p",
        "t_stat",
        "t_p",
        "sign_pos",
        "sign_neg",
        "sign_p",
        "bs_mean_delta_std",
        "ci_std_95_lo",
        "ci_std_95_hi",
        "ci_std_90_lo",
        "ci_std_90_hi",
        "bs_mean_delta_mhz",
        "ci_mhz_95_lo",
        "ci_mhz_95_hi",
    ]
    rows: list[dict] = []
    for h in (pred_vs_syn, h_test):
        rows.append(
            {
                "test": h.name,
                "n_designs": len(h.designs),
                "mean_delta_mhz": h.mean_delta_mhz,
                "median_delta_mhz": h.median_delta_mhz,
                "mean_delta_std": h.mean_delta_std,
                "wilcoxon_w": h.wilcoxon_w,
                "wilcoxon_p": h.wilcoxon_p,
                "t_stat": h.t_stat,
                "t_p": h.t_p,
                "sign_pos": h.sign_pos,
                "sign_neg": h.sign_neg,
                "sign_p": h.sign_p,
                "bs_mean_delta_std": h.bs_mean_std,
                "ci_std_95_lo": h.ci_std_95[0],
                "ci_std_95_hi": h.ci_std_95[1],
                "ci_std_90_lo": h.ci_std_90[0],
                "ci_std_90_hi": h.ci_std_90[1],
                "bs_mean_delta_mhz": h.bs_mean_mhz,
                "ci_mhz_95_lo": h.ci_mhz_95[0],
                "ci_mhz_95_hi": h.ci_mhz_95[1],
            }
        )
    for r in arm_vs_baseline_rows:
        rows.append(
            {
                "test": f"{r.arm}_vs_baseline",
                "n_designs": len(r.deltas_std),
                "mean_delta_mhz": float(r.deltas_mhz.mean()),
                "median_delta_mhz": float(np.median(r.deltas_mhz)),
                "mean_delta_std": float(r.deltas_std.mean()),
                "wilcoxon_w": r.wilcoxon_w,
                "wilcoxon_p": r.wilcoxon_p,
                "t_stat": r.t_stat,
                "t_p": r.t_p,
                "sign_pos": r.sign_pos,
                "sign_neg": r.sign_neg,
                "sign_p": r.sign_p,
                "bs_mean_delta_std": r.bs_mean_std,
                "ci_std_95_lo": r.ci_std_95[0],
                "ci_std_95_hi": r.ci_std_95[1],
                "ci_std_90_lo": float("nan"),
                "ci_std_90_hi": float("nan"),
                "bs_mean_delta_mhz": r.bs_mean_mhz,
                "ci_mhz_95_lo": r.ci_mhz_95[0],
                "ci_mhz_95_hi": r.ci_mhz_95[1],
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


# main


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--summary", type=Path, default=SUMMARY_JSON)
    ap.add_argument("--variance", type=Path, default=VARIANCE_CSV)
    ap.add_argument(
        "--bootstrap-iters",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERS,
        help=f"number of bootstrap iterations (default: {DEFAULT_BOOTSTRAP_ITERS})",
    )
    ap.add_argument(
        "--bootstrap-seed",
        type=int,
        default=DEFAULT_BOOTSTRAP_SEED,
        help=f"PRNG seed (default: {DEFAULT_BOOTSTRAP_SEED})",
    )
    ap.add_argument("--per-design-out", type=Path, default=OUT_PER_DESIGN_CSV)
    ap.add_argument("--summary-out", type=Path, default=OUT_SUMMARY_CSV)
    args = ap.parse_args()

    if not args.summary.is_file():
        sys.exit(f"missing summary input: {args.summary}")
    if not args.variance.is_file():
        sys.exit(f"missing variance input: {args.variance}")

    summary = load_summary(args.summary)
    per_edit = {arm: load_per_edit(p) for arm, p in PER_EDIT_CSVS.items()}
    rewriter_pnr_mhz = load_rewriter_baseline_pnr_mhz(args.variance)

    per_design_data = collect_per_design(summary, per_edit)

    # Eligible designs: all 3 arms have data, baseline available, plus the
    # head-to-head s_d requires >= 2 runs in both synth and predict.
    designs = sorted(
        n
        for n, a in per_design_data.items()
        if len(a["synth"]) >= 2
        and len(a["predict"]) >= 2
        and len(a["pnr"]) >= 1
        and n in rewriter_pnr_mhz
    )
    if not designs:
        sys.exit("no eligible designs")
    print(f"N designs with all 3 arms (synth>=2, predict>=2, pnr>=1): {len(designs)}")

    arm_runs: dict[str, list[np.ndarray]] = {arm: [] for arm in ARMS}
    s_d: list[float] = []
    for n in designs:
        s_arr = np.array(per_design_data[n]["synth"])
        p_arr = np.array(per_design_data[n]["predict"])
        o_arr = np.array(per_design_data[n]["pnr"])
        arm_runs["synth"].append(s_arr)
        arm_runs["predict"].append(p_arr)
        arm_runs["pnr"].append(o_arr)
        s_d.append(pooled_s_d(s_arr, p_arr))

    rng = np.random.default_rng(args.bootstrap_seed)

    pred_vs_syn = head_to_head(
        "predict_vs_synth",
        designs,
        arm_runs["synth"],
        arm_runs["predict"],
        s_d,
        n_boot=args.bootstrap_iters,
        rng=rng,
    )
    h_test = head_to_head(
        "oracle_vs_synth",
        designs,
        arm_runs["synth"],
        arm_runs["pnr"],
        s_d,
        n_boot=args.bootstrap_iters,
        rng=rng,
    )

    arm_vs_baseline_rows = [
        arm_vs_baseline(
            arm,
            designs,
            arm_runs[arm],
            [rewriter_pnr_mhz[n] for n in designs],
            s_d,
            n_boot=args.bootstrap_iters,
            rng=rng,
        )
        for arm in ARMS
    ]

    print_per_design_table(designs, arm_runs, s_d, pred_vs_syn, h_test)
    print_head_to_head(pred_vs_syn, label="(A) PREDICT vs SYNTH (paired delta_d)")
    print_head_to_head(h_test, label="(B) ORACLE (pnr) vs SYNTH: headroom H")
    print_arm_vs_baseline_table(arm_vs_baseline_rows)
    tost_and_mde(h_test, pred_vs_syn, len(designs))

    write_per_design_csv(
        args.per_design_out,
        designs,
        arm_runs,
        s_d,
        rewriter_pnr_mhz,
        pred_vs_syn,
        h_test,
        arm_vs_baseline_rows,
    )
    write_summary_csv(args.summary_out, pred_vs_syn, h_test, arm_vs_baseline_rows)
    print()
    print(f"Wrote {args.per_design_out.relative_to(REPO)}")
    print(f"Wrote {args.summary_out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
