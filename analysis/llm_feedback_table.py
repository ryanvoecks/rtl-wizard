#!/usr/bin/env python3
"""16-row x (synth/predict/pnr-feedback x synth-up/pnr-up/retention) table.

For each (design, feedback) cell:
  - Get every epoch's `best_iter` from the matching
    analysis/data/llm_per_edit_<feedback>.csv. That picker is the
    feedback mode's own predictor (synth WNS for synth-feedback,
    predicted-PnR for predict-feedback, real post-route WS for
    pnr-feedback) -- the source of truth requested by the user.
  - Read the raw synth_wns_ns / pnr_ws_ns at that iter directly from
    tmp/summary.json, convert to fmax via 1000 / (T_target - WS), and
    divide by the 8-seed rewriter-sweep mean in
    analysis/data/variance.csv.
  - Geomean the ratios across epochs. Uplift = (geomean - 1) * 100.
    Retention = gm_pnr_pct / gm_synth_pct.

Drop epochs where the CSV reports a fail token in best_*_pct (no valid
iter) and epochs where the matching iter is missing in summary.json
(typically a partial-failure iter where one stage is unreported).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "analysis" / "data"
SUMMARY_JSON = REPO / "tmp" / "summary.json"
VARIANCE_CSV = DATA / "variance.csv"
OUT_CSV = DATA / "llm_feedback_table.csv"

PER_EDIT_CSVS = {
    "synth": DATA / "llm_per_edit_synth.csv",
    "predict": DATA / "llm_per_edit_predict.csv",
    "pnr": DATA / "llm_per_edit_pnr.csv",
}

TRAINING_DESIGNS = (
    "aes",
    "sha512",
    "double_fpu",
    "bitonic_sorter",
    "e203",
    "reed_solomon",
    "systolic_tpu",
    "viterbi",
    "verilog_axi",
    "uberddr3",
    "wb_dma",
    "jpeg_encoder",
)
TEST_DESIGNS = ("cordic", "fir", "wb_conmax", "modexp")
ALL_DESIGNS = TRAINING_DESIGNS + TEST_DESIGNS
FEEDBACKS = ("synth", "predict", "pnr")


def fmax_mhz(period_ns: float, ws_ns: float) -> float | None:
    cp = period_ns - ws_ns
    if cp <= 0:
        return None
    return 1000.0 / cp


def geomean(xs: list[float]) -> float:
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def load_variance(path: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            short = r["design"].split("/", 1)[1]
            out[short] = {
                "synth_mean": float(r["synth_fmax_mhz_mean"]),
                "pnr_mean": float(r["pnr_fmax_mhz_mean"]),
            }
    return out


def load_summary(path: Path) -> dict:
    return json.loads(path.read_text())


def load_per_edit(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for r in csv.DictReader(f):
            bs = r["best_synth_pct"]
            bp = r["best_pnr_pct"]
            if bs.startswith("fail") or bp.startswith("fail"):
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


def fmax_at_iter(
    summary: dict, run: str, design: str, epoch: int, k: int
) -> tuple[float | None, float | None]:
    """(synth_fmax, pnr_fmax) at iter k for the (run, design, epoch) sample."""
    b = summary["baselines"].get(design)
    if not b or not b.get("present"):
        return None, None
    period = b["period_ns"]
    # summary.json sample keys are "<run>|<bench>|<design>|<epoch_n>"
    needle = f"|{design}|epoch_{epoch}"
    for key, iters in summary["samples"].items():
        if not key.startswith(run + "|") or not key.endswith(needle):
            continue
        row = iters.get(str(k)) or iters.get(k)
        if row is None:
            return None, None
        return (
            fmax_mhz(period, row["synth_wns_ns"])
            if row.get("synth_wns_ns") is not None
            else None,
            fmax_mhz(period, row["pnr_ws_ns"])
            if row.get("pnr_ws_ns") is not None
            else None,
        )
    return None, None


def cell(
    summary: dict,
    variance: dict[str, dict[str, float]],
    epochs: list[dict],
    design: str,
) -> tuple[float | None, float | None, float | None, int]:
    v = variance.get(design)
    if v is None or not epochs:
        return None, None, None, 0
    synth_ratios: list[float] = []
    pnr_ratios: list[float] = []
    for e in epochs:
        fs, fp = fmax_at_iter(summary, e["run"], design, e["epoch"], e["best_iter"])
        if fs is None or fp is None:
            continue
        synth_ratios.append(fs / v["synth_mean"])
        pnr_ratios.append(fp / v["pnr_mean"])
    if not synth_ratios:
        return None, None, None, 0
    gs = (geomean(synth_ratios) - 1) * 100
    gp = (geomean(pnr_ratios) - 1) * 100
    ret = (gp / gs) if gs != 0 else None
    return gs, gp, ret, len(synth_ratios)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--summary", type=Path, default=SUMMARY_JSON)
    ap.add_argument("--variance", type=Path, default=VARIANCE_CSV)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    args = ap.parse_args()

    summary = load_summary(args.summary)
    variance = load_variance(args.variance)
    per_fb: dict[str, list[dict]] = {
        fb: load_per_edit(p) for fb, p in PER_EDIT_CSVS.items()
    }

    if not all(per_fb.values()):
        sys.exit("one of the per-edit CSVs is empty -- check inputs")

    out_rows: list[dict] = []
    for design in ALL_DESIGNS:
        row: dict = {"design": design}
        for fb in FEEDBACKS:
            epochs = [r for r in per_fb[fb] if r["design"] == design]
            gs, gp, ret, n = cell(summary, variance, epochs, design)
            row[f"{fb}_synth_pct"] = gs
            row[f"{fb}_pnr_pct"] = gp
            row[f"{fb}_retention"] = ret
            row[f"n_{fb}"] = n
        out_rows.append(row)

    # Pretty print
    def fmt_pct(v: float | None) -> str:
        return "      n/a" if v is None else f"{v:>+8.2f}%"

    def fmt_ret(v: float | None) -> str:
        return "    n/a" if v is None else f"{v:>+7.3f}"

    hdr = (
        f"{'design':<16} | "
        + " | ".join(f"{fb}-feedback".center(28) for fb in FEEDBACKS)
        + " | n s/p/p"
    )
    sub = (
        f"{'':<16} | "
        + " | ".join(f"{'syn%':>8} {'pnr%':>8} {'ret':>7}" for _ in FEEDBACKS)
        + " |"
    )
    print(hdr)
    print(sub)
    bar = "-" * 16 + " + " + " + ".join("-" * 28 for _ in FEEDBACKS) + " +" + "-" * 9
    print(bar)
    for i, r in enumerate(out_rows):
        if i == len(TRAINING_DESIGNS):
            print(bar)
        cells = []
        for fb in FEEDBACKS:
            cells.append(
                f"{fmt_pct(r[f'{fb}_synth_pct'])} {fmt_pct(r[f'{fb}_pnr_pct'])} "
                f"{fmt_ret(r[f'{fb}_retention'])}"
            )
        ns = "/".join(str(r[f"n_{fb}"]) for fb in FEEDBACKS)
        print(f"{r['design']:<16} | {' | '.join(cells)} | {ns}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print()
    print(f"Wrote {args.out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
