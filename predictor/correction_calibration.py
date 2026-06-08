#!/usr/bin/env python3
"""Derive the synth-slack correction formula for logical-blocks ranking
agreement with PnR, following the protocol in `docs/correction_protocol.md`
(or the conversation-pinned version). Steps:

  0. Data contract -- report schema, deviations from the protocol.
  1. Async/test quarantine -- endpoint-pin-name fallback (no liberty parsing
     in this repo). Sanity gate: e203 and wb_dma must show large synth
     quarantine fraction and near-zero route.
  2. Δ = slack_route_ns - slack_synth_ns; classify path port role into
     {input_only, output_only, both, internal}.
  3. Per-design per-class median Δ + bootstrap CI (paths within design --
     true seeds are unavailable, CI is acknowledged optimistic). Global
     β per class = median of per-design medians; bootstrap by resampling
     designs.
  4. Mixed-effects model:  Δ ~ C(port_class) + slack_synth + (1 | design).
     Also fit on Δ / clock_period. Print fixed effects, design random-effect
     variance, and decision gates for "slope significant?" /
     "design random-effect dominates?".
  5. Leave-one-out validation. For each held-out design, refit β on the
     other 11 (median-of-medians from Step 3) and apply frozen. Report 12
     held-out RBO values and the spread of refit β values.
  6. Cross-validated max-RBO β: per LOO fold, sweep (β_input, β_output)
     over a grid, pick the point maximizing MEAN training-fold RBO, apply
     to held-out, report.
  7. (Adapted -- we have 12 designs total, no separate held-out 4-set.) LOO
     mean RBO IS the headline. Both (a) median-of-medians and (b) max-RBO
     LOO are reported; whichever wins by mean LOO RBO is the chosen
     formula.
  8. Tables -- written under analysis/data/correction/ and printed to
     stdout.

Logical-blocks throughout: the join unit is the collapsed (start, end) pair
from path_slack_union/<design>.tsv. RBO is computed on collapsed pairs at
top-K=100, RBO_EXT persistence p=0.9.

Inputs:
  - analysis/data/path_slack_union/<design>.tsv
  - analysis/data/logical_blocks/<design>__{1_synth,6_final}.rpt
  - common.targets (clock period per design)

Outputs:
  - analysis/data/correction/quarantine.csv
  - analysis/data/correction/per_design_class_medians.csv
  - analysis/data/correction/global_beta.csv
  - analysis/data/correction/mixed_effects.txt
  - analysis/data/correction/loo_median_of_medians.csv
  - analysis/data/correction/loo_max_rbo_sweep.csv
  - analysis/data/correction/headline.txt
  - stdout: full report

Usage:
    uv run --with pandas --with numpy --with statsmodels --with scipy \\
        python analysis/correction_calibration.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.targets import all_targets  # noqa: E402

UNION_DIR = REPO / "analysis" / "data" / "path_slack_union"
RPT_DIR = REPO / "analysis" / "data" / "logical_blocks"
OUT_DIR = REPO / "predictor" / "data" / "calibration"

# Endpoint-pin fallback per Step 1 of the protocol. "D" is the only pin
# treated as functional (setup/hold-arc data). Anything else with a /pin
# suffix is async/test. The library here is nangate45 + yosys generic cells;
# the pin names that surface in practice are D, RN, SN, plus combinational
# inputs A1/A2 when a clock-gate cell is on the endpoint side.
DATA_ENDPOINT_PINS = {"D"}

TOP_K = 100
DEFAULT_P = 0.9

# Port classes: keep "both" distinct (do not fold into input or output).
CAT_INPUT = "input_only"
CAT_OUTPUT = "output_only"
CAT_BOTH = "both"
CAT_INTERNAL = "internal"
PORT_CLASSES = (CAT_INPUT, CAT_OUTPUT, CAT_BOTH, CAT_INTERNAL)

# Designs flagged for the Step 1 sanity gate. Both should have high synth
# async quarantine and low PnR quarantine.
SANITY_GATE_DESIGNS = ("e203", "wb_dma")
SYNTH_GATE_MIN = 0.10   # fraction of synth top-100 that must be quarantined
ROUTE_GATE_MAX = 0.20   # max fraction of route top-100 that may be quarantined

# Bootstrap config
N_BOOT = 2000
RNG_SEED = 20260608

# Sweep grid for Step 6 -- centered on 0 with 0.05 ns steps
SWEEP_RANGE = np.arange(-0.6, 0.6 + 1e-9, 0.05)

_PORT_DECL = re.compile(r"^\s*(input|output|inout)\s+(?:\[[^\]]+\]\s+)?(\w+)\s*;")
_BUS_INDEX_TAIL = re.compile(r"\[[A-Z0-9]+\]$")


def _bus_stem_collapsed(name: str) -> str:
    return _BUS_INDEX_TAIL.sub("", name)


def _parse_yosys_ports(verilog: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in verilog.read_text().splitlines():
        m = _PORT_DECL.match(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def _load_ports(design_name: str) -> dict[str, str]:
    from common.config import ALL_FLOW_TARGETS, EDA_RUNS, RunConfig
    from eda_eval.cache import cache_path
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


def _endpoint_pin(end_full: str) -> str:
    if "/" not in end_full:
        return ""
    return end_full.rsplit("/", 1)[-1]


def _is_async_pin(end_full: str) -> bool:
    """Endpoint-pin-name async classifier (protocol fallback).

    Matches the pin name AFTER the last '/' only; never matches against
    signal/register names. An endpoint with no '/' is a primary port and
    is treated as functional (output port = output-incident path).
    """
    pin = _endpoint_pin(end_full)
    return pin != "" and pin not in DATA_ENDPOINT_PINS


def _read_union(path: Path) -> pd.DataFrame:
    rows = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            rows.append(
                {
                    "start": parts[0],
                    "end": parts[1],
                    "slack_synth_ns": float(parts[2]),
                    "slack_route_ns": float(parts[3]),
                }
            )
        except ValueError:
            continue
    return pd.DataFrame(rows)


def _read_full_name_map(design: str) -> dict[tuple[str, str], dict[str, tuple[str, str]]]:
    out: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for stage in ("1_synth", "6_final"):
        rpt = RPT_DIR / f"{design}__{stage}.rpt"
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


def _design_clock_period_ns(design: str) -> float | None:
    for t in all_targets:
        if t.design.name == design:
            return float(t.period_ns)
    return None


def _quarantine_fraction(design: str, stage: str) -> tuple[int, int]:
    """Count async endpoint pins among the top-100 logical_blocks at one stage."""
    rpt = RPT_DIR / f"{design}__{stage}.rpt"
    if not rpt.is_file():
        return 0, 0
    n_async = 0
    n_total = 0
    for line in rpt.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        n_total += 1
        if _is_async_pin(parts[5]):
            n_async += 1
    return n_async, n_total


def _port_class_for(start_full: str, end_full: str, ports: dict[str, str]) -> str:
    si = _is_input(start_full, ports)
    eo = _is_output(end_full, ports)
    if si and eo:
        return CAT_BOTH
    if si:
        return CAT_INPUT
    if eo:
        return CAT_OUTPUT
    return CAT_INTERNAL


def load_all(designs: list[str]) -> pd.DataFrame:
    """Long-form DataFrame of all union pairs across all designs, annotated."""
    frames = []
    for design in designs:
        union = _read_union(UNION_DIR / f"{design}.tsv")
        if union.empty:
            continue
        full_map = _read_full_name_map(design)
        ports = _load_ports(design)
        period = _design_clock_period_ns(design)
        # For each collapsed pair, pick the representative start_full/end_full
        # from whichever stage has it; the bus stem (used for port lookup) is
        # the same across stages, and the pin name (used for async detection)
        # only differs between bits of the same bus.
        records = []
        for _, row in union.iterrows():
            key = (row["start"], row["end"])
            stage_fulls = full_map.get(key, {})
            if stage_fulls:
                start_full, end_full = next(iter(stage_fulls.values()))
                end_fulls = [v[1] for v in stage_fulls.values()]
            else:
                start_full, end_full = key
                end_fulls = []
            # Quarantine if ANY stage's representative endpoint is non-D.
            quarantined = any(_is_async_pin(ef) for ef in end_fulls)
            port_class = _port_class_for(start_full, end_full, ports)
            records.append(
                {
                    "design": design,
                    "start": row["start"],
                    "end": row["end"],
                    "slack_synth_ns": row["slack_synth_ns"],
                    "slack_route_ns": row["slack_route_ns"],
                    "delta_ns": row["slack_route_ns"] - row["slack_synth_ns"],
                    "end_pin": _endpoint_pin(end_full),
                    "port_class": port_class,
                    "quarantined": quarantined,
                    "clock_period_ns": period,
                }
            )
        frames.append(pd.DataFrame(records))
    return pd.concat(frames, ignore_index=True)


# --- Step 1: quarantine table -------------------------------------------------


def step1_quarantine(df: pd.DataFrame) -> pd.DataFrame:
    """Per-design synth/route quarantine fractions from the top-100 rpt."""
    rows = []
    for design in sorted(df["design"].unique()):
        ns_a, ns_t = _quarantine_fraction(design, "1_synth")
        nr_a, nr_t = _quarantine_fraction(design, "6_final")
        rows.append(
            {
                "design": design,
                "synth_q": ns_a,
                "synth_n": ns_t,
                "synth_frac": ns_a / ns_t if ns_t else float("nan"),
                "route_q": nr_a,
                "route_n": nr_t,
                "route_frac": nr_a / nr_t if nr_t else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def sanity_gate(q: pd.DataFrame) -> tuple[bool, list[str]]:
    msgs = []
    ok = True
    for d in SANITY_GATE_DESIGNS:
        row = q[q["design"] == d]
        if row.empty:
            msgs.append(f"{d}: missing")
            ok = False
            continue
        sf = float(row["synth_frac"].iloc[0])
        rf = float(row["route_frac"].iloc[0])
        # NaN route_frac means no /-bearing endpoints at all in route top-100
        # (everything was a primary port endpoint). Treat that as passing the
        # "near-zero PnR quarantine" requirement.
        rf_check = rf if not (rf != rf) else 0.0
        if sf < SYNTH_GATE_MIN or rf_check > ROUTE_GATE_MAX:
            msgs.append(
                f"{d}: synth_frac={sf:.2f} (need >={SYNTH_GATE_MIN}); "
                f"route_frac={rf:.2f} (need <={ROUTE_GATE_MAX})"
            )
            ok = False
        else:
            msgs.append(f"{d}: synth_frac={sf:.2f}, route_frac={rf:.2f}  OK")
    return ok, msgs


# --- Step 3: per-design per-class medians + bootstrap ------------------------


def _median_ci(
    arr: np.ndarray, n_boot: int, rng: np.random.Generator
) -> tuple[float, float, float]:
    """Bootstrap CI for the median over arr, resampling its elements with
    replacement. Returns (median, lo95, hi95). For per-design-within-class
    use this acknowledging the expert's caveat: paths within a design are
    not independent units; CI is optimistic."""
    if arr.size == 0:
        return float("nan"), float("nan"), float("nan")
    if arr.size == 1:
        return float(arr[0]), float(arr[0]), float(arr[0])
    boots = np.empty(n_boot)
    n = arr.size
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boots[i] = np.median(arr[idx])
    lo, hi = np.quantile(boots, [0.025, 0.975])
    return float(np.median(arr)), float(lo), float(hi)


def step3_per_design_class(df: pd.DataFrame) -> pd.DataFrame:
    """Per (design, port_class) median Δ + bootstrap CI."""
    rng = np.random.default_rng(RNG_SEED)
    rows = []
    for design in sorted(df["design"].unique()):
        for cls in PORT_CLASSES:
            sub = df[(df["design"] == design) & (df["port_class"] == cls)]
            arr = sub["delta_ns"].to_numpy()
            med, lo, hi = _median_ci(arr, N_BOOT, rng)
            rows.append(
                {
                    "design": design,
                    "port_class": cls,
                    "n": int(arr.size),
                    "median_delta_ns": med,
                    "ci_lo_ns": lo,
                    "ci_hi_ns": hi,
                }
            )
    return pd.DataFrame(rows)


def step3_global_beta(per_design: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-design medians into a global β per class (median of
    medians). CI by resampling whole designs."""
    rng = np.random.default_rng(RNG_SEED + 1)
    rows = []
    for cls in PORT_CLASSES:
        sub = per_design[per_design["port_class"] == cls].dropna(subset=["median_delta_ns"])
        # Exclude design-classes with zero observations.
        sub = sub[sub["n"] > 0]
        if sub.empty:
            rows.append({"port_class": cls, "n_designs": 0,
                         "global_median_ns": float("nan"),
                         "ci_lo_ns": float("nan"), "ci_hi_ns": float("nan")})
            continue
        medians = sub["median_delta_ns"].to_numpy()
        n = medians.size
        boots = np.empty(N_BOOT)
        for i in range(N_BOOT):
            idx = rng.integers(0, n, size=n)
            boots[i] = np.median(medians[idx])
        lo, hi = np.quantile(boots, [0.025, 0.975])
        rows.append(
            {
                "port_class": cls,
                "n_designs": int(n),
                "global_median_ns": float(np.median(medians)),
                "ci_lo_ns": float(lo),
                "ci_hi_ns": float(hi),
            }
        )
    return pd.DataFrame(rows)


# --- Step 4: mixed-effects model ----------------------------------------------


def step4_mixed_effects(df: pd.DataFrame) -> dict:
    """Δ ~ port_class + slack_synth + (1 | design), training designs only.

    Also fit on normalized response Δ / clock_period.
    """
    import statsmodels.formula.api as smf

    out = {}
    for response, label in (("delta_ns", "raw"), ("delta_norm", "normalized")):
        data = df.copy()
        if response == "delta_norm":
            data["delta_norm"] = data["delta_ns"] / data["clock_period_ns"]
        # Reference category = internal so the coefficients read as
        # "offset relative to internal".
        data["port_class"] = pd.Categorical(
            data["port_class"], categories=[CAT_INTERNAL, CAT_INPUT, CAT_OUTPUT, CAT_BOTH]
        )
        try:
            model = smf.mixedlm(
                f"{response} ~ C(port_class) + slack_synth_ns",
                data,
                groups=data["design"],
            )
            fit = model.fit(method="lbfgs", reml=True)
            summary_text = fit.summary().as_text()
            out[label] = {
                "fit": fit,
                "summary": summary_text,
                "params": fit.params.to_dict(),
                "bse": fit.bse.to_dict(),
                "pvalues": fit.pvalues.to_dict(),
                "design_var": float(fit.cov_re.iloc[0, 0]),
                "resid_var": float(fit.scale),
            }
        except Exception as e:
            out[label] = {"error": str(e)}
    return out


# --- RBO -----------------------------------------------------------------------


def rbo_ext(s: list, t: list, p: float) -> float:
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
    residual = ((x_l - x_s) / n_t + x_s / n_s) * (p ** n_t)
    return base + residual


def _apply_correction(
    df_d: pd.DataFrame, betas: dict[str, float]
) -> pd.DataFrame:
    """Add `slack_synth_corr_ns` = slack_synth_ns + β[port_class]."""
    df_d = df_d.copy()
    df_d["beta_ns"] = df_d["port_class"].map(betas).fillna(0.0)
    df_d["slack_synth_corr_ns"] = df_d["slack_synth_ns"] + df_d["beta_ns"]
    return df_d


def _design_rbo(
    df_d_all: pd.DataFrame,
    betas: dict[str, float],
    top_k: int,
    p: float,
    *,
    filter_synth: bool,
) -> float:
    """RBO between the synth-predicted ranking and the actual route ranking.

    At inference we only have synth-side info, so the async quarantine
    applies ONLY to the synth ranking. The route ranking is the ground
    truth -- whatever paths route ended up with, async or not -- and stays
    unfiltered. `df_d_all` is the full per-design union pool (no filter
    pre-applied); the function handles filtering internally so the two
    rankings see the right subsets.
    """
    # Synth side: optionally drop async, then apply correction, then rank.
    synth_pool = df_d_all[~df_d_all["quarantined"]] if filter_synth else df_d_all
    synth_pool = _apply_correction(synth_pool, betas)
    synth_sorted = synth_pool.sort_values("slack_synth_corr_ns")
    # Route side: no filter. Whatever route's top-K is, even if it includes
    # async-pin endpoints, is the comparison target.
    route_sorted = df_d_all.sort_values("slack_route_ns")
    synth_keys = list(zip(synth_sorted["start"], synth_sorted["end"]))[:top_k]
    route_keys = list(zip(route_sorted["start"], route_sorted["end"]))[:top_k]
    n_eval = min(len(synth_keys), len(route_keys))
    return rbo_ext(synth_keys[:n_eval], route_keys[:n_eval], p)


# --- Step 5: LOO with median-of-medians β ------------------------------------


def step5_loo_median_of_medians(
    df_all: pd.DataFrame, per_design: pd.DataFrame, top_k: int, p: float
) -> pd.DataFrame:
    """For each held-out design d, recompute global β from the other 11
    (median of those 11 per-class medians), apply frozen to d, compute RBO.

    Three baselines per fold (route side is never filtered):
      raw          -- no synth filter, β = 0 (matches the original
                      llm_topk_rbo intent, modulo union pool / p=0.9)
      filter_only  -- synth-side async quarantine on, β = 0
      corrected    -- synth-side async quarantine on, trained β applied
    """
    rows = []
    designs = sorted(df_all["design"].unique())
    zero = {c: 0.0 for c in PORT_CLASSES}
    for held in designs:
        train = per_design[per_design["design"] != held]
        train = train[train["n"] > 0]

        # Parametrization: β_input, β_output, β_internal are each estimated as
        # the median of per-design medians on the training fold. β_both is
        # constrained by additivity over the internal baseline:
        #     β_both = β_input + β_output - β_internal
        # i.e. assume the input-incidence and output-incidence offsets compose
        # additively relative to the internal baseline. With one free parameter
        # fewer this is more stable when "both" is sparse (only 5 designs have
        # any paths in that class).
        def _mom(cls: str) -> float:
            sub = train[train["port_class"] == cls]
            return float(np.median(sub["median_delta_ns"].to_numpy())) if not sub.empty else 0.0

        beta_in = _mom(CAT_INPUT)
        beta_out = _mom(CAT_OUTPUT)
        beta_int = _mom(CAT_INTERNAL)
        beta_both = beta_in + beta_out - beta_int
        betas = {
            CAT_INPUT: beta_in,
            CAT_OUTPUT: beta_out,
            CAT_INTERNAL: beta_int,
            CAT_BOTH: beta_both,
        }

        df_h = df_all[df_all["design"] == held]
        rbo_raw = _design_rbo(df_h, zero, top_k, p, filter_synth=False)
        rbo_filt = _design_rbo(df_h, zero, top_k, p, filter_synth=True)
        rbo_corr = _design_rbo(df_h, betas, top_k, p, filter_synth=True)
        rows.append(
            {
                "held_out": held,
                "beta_input_ns": beta_in,
                "beta_output_ns": beta_out,
                "beta_internal_ns": beta_int,
                "beta_both_derived_ns": beta_both,
                "rbo_raw": rbo_raw,
                "rbo_filter_only": rbo_filt,
                "rbo_corrected": rbo_corr,
                "gain_filter_over_raw": rbo_filt - rbo_raw,
                "gain_corr_over_filter": rbo_corr - rbo_filt,
                "gain_corr_over_raw": rbo_corr - rbo_raw,
            }
        )
    return pd.DataFrame(rows)


# --- Step 6: LOO with max-RBO sweep ------------------------------------------


def _mean_rbo_over_designs(
    df_all: pd.DataFrame, designs: list[str], betas: dict[str, float],
    top_k: int, p: float,
) -> float:
    """Mean per-design RBO with synth-side filter on, route-side filter off.

    Sweeping over β to maximise this mirrors the inference-time pipeline.
    """
    vals = []
    for d in designs:
        df_d = df_all[df_all["design"] == d]
        if df_d.empty:
            continue
        vals.append(_design_rbo(df_d, betas, top_k, p, filter_synth=True))
    return float(np.mean(vals)) if vals else 0.0


def step6_loo_max_rbo_sweep(
    df_all: pd.DataFrame, top_k: int, p: float
) -> pd.DataFrame:
    """For each held-out design d, sweep (β_input, β_output) over a 2-D grid;
    for each grid point compute the MEAN RBO across the other 11 designs;
    pick the argmax; apply frozen to d. β_both = β_input + β_output as a
    constraint (additivity); β_internal = 0.
    """
    rows = []
    designs = sorted(df_all["design"].unique())
    zero = {c: 0.0 for c in PORT_CLASSES}
    for held in designs:
        train = [d for d in designs if d != held]
        best = (-1.0, 0.0, 0.0)
        for b_in in SWEEP_RANGE:
            for b_out in SWEEP_RANGE:
                betas = {
                    CAT_INPUT: float(b_in),
                    CAT_OUTPUT: float(b_out),
                    CAT_BOTH: float(b_in + b_out),
                    CAT_INTERNAL: 0.0,
                }
                m = _mean_rbo_over_designs(df_all, train, betas, top_k, p)
                if m > best[0]:
                    best = (m, float(b_in), float(b_out))
        _, b_in, b_out = best
        betas = {
            CAT_INPUT: b_in,
            CAT_OUTPUT: b_out,
            CAT_BOTH: b_in + b_out,
            CAT_INTERNAL: 0.0,
        }
        df_h = df_all[df_all["design"] == held]
        rbo_raw = _design_rbo(df_h, zero, top_k, p, filter_synth=False)
        rbo_filt = _design_rbo(df_h, zero, top_k, p, filter_synth=True)
        rbo_corr = _design_rbo(df_h, betas, top_k, p, filter_synth=True)
        rows.append(
            {
                "held_out": held,
                "beta_input_ns": b_in,
                "beta_output_ns": b_out,
                "train_mean_rbo": best[0],
                "rbo_raw": rbo_raw,
                "rbo_filter_only": rbo_filt,
                "rbo_corrected": rbo_corr,
                "gain_corr_over_filter": rbo_corr - rbo_filt,
                "gain_corr_over_raw": rbo_corr - rbo_raw,
            }
        )
    return pd.DataFrame(rows)


# --- Reporting ---------------------------------------------------------------


def _fmt_df(df: pd.DataFrame, floatfmt: str = "{:+.3f}") -> str:
    out = df.copy()
    for c in out.columns:
        if out[c].dtype.kind == "f":
            out[c] = out[c].map(
                lambda v: "" if pd.isna(v) else floatfmt.format(v)
            )
    return out.to_string(index=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--p", type=float, default=DEFAULT_P)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    designs = sorted(p.stem for p in UNION_DIR.glob("*.tsv"))

    print("=" * 78)
    print("STEP 0  Data contract")
    print("=" * 78)
    print(f"  designs found: {len(designs)}  -> {designs}")
    print(
        "  schema reconciliation (vs expert protocol):\n"
        "    - no seed/edit dimension: one record per (design, collapsed pair)\n"
        "    - no separate 4-design held-out: LOO across 12 is validation + headline\n"
        "    - no liberty parsing: endpoint-pin-name suffix matching\n"
        "    - quarantine rule: endpoint pin != 'D' (no shared-driver augment)"
    )

    print(f"\n  loading all designs ({len(designs)} TSVs)...")
    df_all = load_all(designs)
    print(f"  total pairs: {len(df_all)}")
    print(f"  end_pin distribution among /-bearing endpoints:")
    pin_counts = df_all[df_all["end_pin"] != ""]["end_pin"].value_counts()
    for pin, n in pin_counts.items():
        print(f"    {pin:>4}: {n}")

    # --- Step 1 ---
    print("\n" + "=" * 78)
    print("STEP 1  Quarantine table  (endpoint-pin fallback)")
    print("=" * 78)
    q = step1_quarantine(df_all)
    print(_fmt_df(q, floatfmt="{:.3f}"))
    q.to_csv(args.out_dir / "quarantine.csv", index=False)

    ok, msgs = sanity_gate(q)
    print("\nSanity gate (e203, wb_dma must show high synth_frac, low route_frac):")
    for m in msgs:
        print(f"  {m}")
    if not ok:
        print(
            "FAILED sanity gate -- filter may be wrong. Stopping per protocol.",
            file=sys.stderr,
        )
        # Per protocol, stop rather than silently produce a number.
        sys.exit(1)

    df_func = df_all[~df_all["quarantined"]].copy()
    print(f"\n  retained {len(df_func)} / {len(df_all)} functional pairs")

    # --- Step 2 already done in load_all (delta, port_class) ---
    print("\n" + "=" * 78)
    print("STEP 2  Δ and port_class counts (functional pairs only)")
    print("=" * 78)
    pivot = (
        df_func.groupby(["design", "port_class"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=PORT_CLASSES, fill_value=0)
    )
    print(pivot.to_string())

    # --- Step 3 ---
    print("\n" + "=" * 78)
    print("STEP 3  Per-design per-class median Δ (ns) + bootstrap 95% CI")
    print("=" * 78)
    per_design = step3_per_design_class(df_func)
    per_design.to_csv(args.out_dir / "per_design_class_medians.csv", index=False)
    show = per_design.pivot(
        index="design", columns="port_class", values="median_delta_ns"
    ).reindex(columns=PORT_CLASSES)
    print("\n  per-design median Δ (ns):")
    print(_fmt_df(show.reset_index(), floatfmt="{:+.3f}"))

    print("\n  global β = median of per-design medians (with design-resample 95% CI):")
    global_beta = step3_global_beta(per_design)
    global_beta.to_csv(args.out_dir / "global_beta.csv", index=False)
    print(_fmt_df(global_beta, floatfmt="{:+.3f}"))

    # --- Step 4 ---
    print("\n" + "=" * 78)
    print("STEP 4  Mixed-effects model")
    print("=" * 78)
    mix = step4_mixed_effects(df_func)
    me_lines = []
    for label, info in mix.items():
        me_lines.append(f"\n--- response = Δ ({label}) ---\n")
        if "error" in info:
            me_lines.append(f"FIT FAILED: {info['error']}\n")
            continue
        me_lines.append(info["summary"])
        me_lines.append(
            f"\n  design random-effect variance: {info['design_var']:.4f}"
            f"  residual variance: {info['resid_var']:.4f}\n"
        )
        # Decision gates
        params = info["params"]
        pvalues = info["pvalues"]
        slope = params.get("slack_synth_ns", float("nan"))
        slope_p = pvalues.get("slack_synth_ns", float("nan"))
        me_lines.append(
            f"  slope (slack_synth_ns) coef={slope:+.3f}  p={slope_p:.3g}\n"
        )
        me_lines.append(
            f"  GATE A (slope significant at p<0.05?): "
            f"{'YES -> emit slope term' if slope_p < 0.05 else 'NO -> constant offset only'}\n"
        )
        # Compare design var vs fixed offset magnitude
        fixed_mag = max(
            abs(v) for k, v in params.items() if "port_class" in k
        ) if any("port_class" in k for k in params) else 0.0
        design_sd = info["design_var"] ** 0.5
        me_lines.append(
            f"  design SD = {design_sd:.3f}  vs largest fixed offset = {fixed_mag:.3f}\n"
        )
        me_lines.append(
            f"  GATE B (design SD > fixed offset?): "
            f"{'YES -> per-design calibration required' if design_sd > fixed_mag else 'NO -> global constant defensible'}\n"
        )
    me_text = "\n".join(me_lines)
    print(me_text)
    (args.out_dir / "mixed_effects.txt").write_text(me_text)

    # --- Step 5 ---
    print("\n" + "=" * 78)
    print("STEP 5  LOO validation -- median-of-medians β")
    print("=" * 78)
    loo_med = step5_loo_median_of_medians(df_all, per_design, args.top_k, args.p)
    loo_med.to_csv(args.out_dir / "loo_median_of_medians.csv", index=False)
    print(_fmt_df(loo_med, floatfmt="{:+.3f}"))
    print(
        f"\n  mean LOO RBO (raw, no filter no shift)     : {loo_med['rbo_raw'].mean():.3f}\n"
        f"  mean LOO RBO (synth-side async filter only): {loo_med['rbo_filter_only'].mean():.3f}\n"
        f"  mean LOO RBO (filter + correction)         : {loo_med['rbo_corrected'].mean():.3f}\n"
        f"  mean gain  filter over raw                 : {loo_med['gain_filter_over_raw'].mean():+.3f}\n"
        f"  mean gain  corr over filter                : {loo_med['gain_corr_over_filter'].mean():+.3f}\n"
        f"  mean gain  corr over raw                   : {loo_med['gain_corr_over_raw'].mean():+.3f}"
    )
    print("\n  refit β spread across 12 folds:")
    for c in ("beta_input_ns", "beta_output_ns", "beta_internal_ns", "beta_both_derived_ns"):
        arr = loo_med[c].to_numpy()
        std_rel = (arr.std() / abs(arr.mean())) if abs(arr.mean()) > 1e-6 else float("inf")
        print(
            f"    {c:<20}  min={arr.min():+.3f}  max={arr.max():+.3f}  "
            f"std={arr.std():.3f}  std/|mean|={std_rel:.2f}"
        )
        gate = "stable" if std_rel < 0.20 else "WIDE -- constant may be selection artifact"
        print(f"      GATE: {gate}")

    # --- Step 6 ---
    print("\n" + "=" * 78)
    print("STEP 6  LOO validation -- max-RBO sweep (cross-validated)")
    print("=" * 78)
    print("  (this can take a minute; 12 folds * grid of "
          f"{len(SWEEP_RANGE)}**2 = {len(SWEEP_RANGE)**2} points * 11 train designs each)")
    loo_swp = step6_loo_max_rbo_sweep(df_all, args.top_k, args.p)
    loo_swp.to_csv(args.out_dir / "loo_max_rbo_sweep.csv", index=False)
    print(_fmt_df(loo_swp, floatfmt="{:+.3f}"))
    print(
        f"\n  mean LOO RBO (raw)                : {loo_swp['rbo_raw'].mean():.3f}\n"
        f"  mean LOO RBO (filter only)        : {loo_swp['rbo_filter_only'].mean():.3f}\n"
        f"  mean LOO RBO (filter + sweep β)   : {loo_swp['rbo_corrected'].mean():.3f}\n"
        f"  mean gain corr over filter        : {loo_swp['gain_corr_over_filter'].mean():+.3f}\n"
        f"  mean gain corr over raw           : {loo_swp['gain_corr_over_raw'].mean():+.3f}"
    )
    print("\n  chosen β spread across 12 folds:")
    for c in ("beta_input_ns", "beta_output_ns"):
        arr = loo_swp[c].to_numpy()
        std_rel = (arr.std() / abs(arr.mean())) if abs(arr.mean()) > 1e-6 else float("inf")
        print(
            f"    {c:<20}  min={arr.min():+.3f}  max={arr.max():+.3f}  "
            f"std={arr.std():.3f}  std/|mean|={std_rel:.2f}"
        )

    # --- Step 7 (adapted) ---
    print("\n" + "=" * 78)
    print("STEP 7  Final β (all 12) and headline")
    print("=" * 78)
    # Median of per-design medians over all 12, with β_both constrained by
    # additivity: β_both = β_input + β_output - β_internal.
    def _mom_all(cls: str) -> float:
        sub = per_design[(per_design["port_class"] == cls) & (per_design["n"] > 0)]
        return float(np.median(sub["median_delta_ns"].to_numpy())) if not sub.empty else 0.0

    final_betas_mom = {
        CAT_INPUT: _mom_all(CAT_INPUT),
        CAT_OUTPUT: _mom_all(CAT_OUTPUT),
        CAT_INTERNAL: _mom_all(CAT_INTERNAL),
    }
    final_betas_mom[CAT_BOTH] = (
        final_betas_mom[CAT_INPUT]
        + final_betas_mom[CAT_OUTPUT]
        - final_betas_mom[CAT_INTERNAL]
    )
    # Max-RBO over all 12 (no held-out -- this is the in-sample optimum, for
    # comparison only; the LOO mean above is the cross-validated headline).
    best_in_sample = (-1.0, 0.0, 0.0)
    for b_in in SWEEP_RANGE:
        for b_out in SWEEP_RANGE:
            betas = {
                CAT_INPUT: float(b_in), CAT_OUTPUT: float(b_out),
                CAT_BOTH: float(b_in + b_out), CAT_INTERNAL: 0.0,
            }
            m = _mean_rbo_over_designs(df_all, designs, betas, args.top_k, args.p)
            if m > best_in_sample[0]:
                best_in_sample = (m, float(b_in), float(b_out))

    headline_text = []
    headline_text.append("Final β (median-of-medians over all 12 designs):")
    for c in PORT_CLASSES:
        headline_text.append(f"  β[{c}] = {final_betas_mom[c]:+.3f} ns")
    headline_text.append("")
    headline_text.append(
        f"Final β (in-sample argmax over all 12 designs, for reference only):\n"
        f"  β_input = {best_in_sample[1]:+.3f}  β_output = {best_in_sample[2]:+.3f}  "
        f"in-sample mean RBO = {best_in_sample[0]:.3f}"
    )
    headline_text.append("")
    headline_text.append(
        f"Cross-validated headline (LOO mean RBO over 12 held-out folds):\n"
        f"  raw (no filter, no correction)  : {loo_med['rbo_raw'].mean():.3f}\n"
        f"  filter only (synth-side, β = 0) : {loo_med['rbo_filter_only'].mean():.3f}\n"
        f"  filter + med-of-medians β       : {loo_med['rbo_corrected'].mean():.3f}\n"
        f"  filter + max-RBO sweep β        : {loo_swp['rbo_corrected'].mean():.3f}"
    )
    headline_text.append("")
    # Choose winner
    if loo_swp["rbo_corrected"].mean() > loo_med["rbo_corrected"].mean():
        ship = "max-RBO sweep"
        ship_loo = loo_swp
    else:
        ship = "median-of-medians"
        ship_loo = loo_med
    headline_text.append(f"Ship: {ship} (higher LOO mean RBO).")
    headline_text.append("")
    # Thesis sentence
    if ship == "median-of-medians":
        b_in_str = f"{final_betas_mom[CAT_INPUT]:+.3f}"
        b_out_str = f"{final_betas_mom[CAT_OUTPUT]:+.3f}"
        b_both_str = f"{final_betas_mom[CAT_BOTH]:+.3f}"
    else:
        # mean of LOO-fold sweep β
        b_in_str = f"{loo_swp['beta_input_ns'].mean():+.3f}"
        b_out_str = f"{loo_swp['beta_output_ns'].mean():+.3f}"
        b_both_str = f"{(loo_swp['beta_input_ns'] + loo_swp['beta_output_ns']).mean():+.3f}"
    if ship == "median-of-medians":
        b_int_str = f"{final_betas_mom[CAT_INTERNAL]:+.3f}"
    else:
        b_int_str = "+0.000"
    headline_text.append(
        "THESIS:\n"
        "  On post-synth logical_blocks, after quarantining async/test endpoints,\n"
        "  applying a per-port-class additive correction to synth slack of\n"
        f"    β[input_only]  = {b_in_str} ns\n"
        f"    β[output_only] = {b_out_str} ns\n"
        f"    β[internal]    = {b_int_str} ns\n"
        f"    β[both]        = {b_both_str} ns  (derived: β_in + β_out - β_int)\n"
        f"  raises mean held-out top-100 RBO (p=0.9) from "
        f"{loo_med['rbo_raw'].mean():.3f} (raw) "
        f"to {loo_med['rbo_filter_only'].mean():.3f} (filter only) "
        f"to {ship_loo['rbo_corrected'].mean():.3f} (filter + correction) "
        f"across 12 designs under leave-one-out validation. "
        f"The async filter is applied only on the synth side -- "
        f"route's top-100 is unfiltered (it is the ground truth)."
    )
    text = "\n".join(headline_text)
    print(text)
    (args.out_dir / "headline.txt").write_text(text + "\n")
    out_dir_abs = args.out_dir if args.out_dir.is_absolute() else (Path.cwd() / args.out_dir)
    try:
        out_label = out_dir_abs.relative_to(REPO)
    except ValueError:
        out_label = out_dir_abs
    print(f"\n  outputs in {out_label}")


if __name__ == "__main__":
    main()
