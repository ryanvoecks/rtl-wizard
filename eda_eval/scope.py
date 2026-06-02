#!/usr/bin/env python3
"""Scope out the tractable optimisation headroom of a single design.

Iterates ORFS at progressively tighter periods to find the highest
frequency at which the design fails timing **but** the fix is still
localised: the union of modules / starting registers touched by every
negative-slack path stays inside fixed budgets.

Three phases:

* Phase A: fixed-point with k=0 converges on `T_baseline`, the period
  where worst slack is ~0. Same recurrence calibrate.py uses, but with
  the target ratio pinned to 0 (i.e. just-meets-timing).
* Phase B: shrink period by `step` per iter. At each step compute the
  fix scope across all `slack<0` paths and check against the module /
  start-stem budgets. Continue while criteria hold; stop on the first
  break (or on a period-floor / pool-saturation guard).
* Phase C: bisect `(t_bad, t_good)` for `--bisect-iters` rounds to
  refine `T_sweet`.

A run is suitable for optimisation iff (a) a valid `T_sweet` was found
and (b) `(T_baseline - T_sweet) / T_baseline >= --min-improvement`. The
budget criteria are enforced by construction at `T_sweet`.

Usage:
    uv run eda_eval/scope.py --design aes_reference
    uv run eda_eval/scope.py --design aes_reference --scope-iters 6
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.config import (
    ALL_FLOW_TARGETS,
    EDA_EVAL,
    EDA_RUNS,
    YOSYS_BIN,
    DesignConfig,
    RunConfig,
    StudyConfig,
    TargetConfig,
)
from common.designs import resolve_design
from eda_eval.analyse import liberty_files, locate_results
from eda_eval.extract_metrics import extract
from eda_eval.run import run_job

ITER_SCOPE_DIR = "__iter_scope__"

# tcl paths
EXTRACT_TCL = EDA_EVAL / "tcl" / "extract_critical_paths.tcl"

# Regexes for lexing the path pool / hierarchy names
INDEX_RE = re.compile(r"\[\d+\]")
HIER_SEP_RE = re.compile(r"[/.]")


# --------------------------------------------------------------------------- #
# Path-pool extraction + hierarchy mapping                                    #
# --------------------------------------------------------------------------- #


def run_openroad_extract(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    libs: list[Path],
    pool: int,
    tsv_out: Path,
) -> None:
    """Drive openroad to dump the worst-slack timing pool to tsv_out."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}"]
    if spef is not None:
        lines.append(f"read_spef {spef}")
    lines.append(f"source {EXTRACT_TCL}")
    env = {**os.environ, "ANALYSE_OUT_TSV": str(tsv_out), "ANALYSE_POOL": str(pool)}
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input="\n".join(lines),
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad extraction failed")


def read_pool_tsv(tsv: Path) -> list[tuple[float, str, str, list[str]]]:
    """Parse the timing-pool TSV into (slack_ns, sp, ep, cells) records."""
    records = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        slack, sp, ep, cells = line.split("\t")
        records.append((float(slack), sp, ep, cells.split("|") if cells else []))
    return records


def dump_hierarchy(
    rtl_files: list[Path],
    top_module: str,
    json_out: Path,
    include_dirs: list[Path] | None = None,
) -> None:
    """Have yosys elaborate the module hierarchy and dump it as JSON."""
    inc_args = "".join(f" -I {d}" for d in (include_dirs or []))
    rtl_args = " ".join(str(f) for f in rtl_files)
    script = (
        f"read_verilog -sv{inc_args} {rtl_args}\n"
        f"hierarchy -top {top_module}\n"
        "proc\n"
        f"write_json {json_out}\n"
    )
    proc = subprocess.run(
        [YOSYS_BIN, "-q", "-p", script],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("yosys hierarchy dump failed")


def _clean_instance(name: str) -> str:
    """Strip yosys escape syntax, `[N]` indices, and trailing `$_CELLTYPE_`."""
    name = name.lstrip("\\").rstrip().split("$", 1)[0]
    return INDEX_RE.sub("", name)


def load_hierarchy(json_path: Path) -> dict[str, dict[str, str]]:
    """Module -> {clean_inst_name: child_module} from yosys write_json output."""
    modules_raw = json.loads(json_path.read_text()).get("modules", {})
    valid = set(modules_raw)
    return {
        name: {
            _clean_instance(inst): cell["type"]
            for inst, cell in info.get("cells", {}).items()
            if cell.get("type") in valid
        }
        for name, info in modules_raw.items()
    }


def cell_modules(
    inst_path: str,
    top_module: str,
    hierarchy: dict[str, dict[str, str]],
) -> tuple[set[str], bool]:
    """Walk an instance path; return (modules touched, walk truncated early)."""
    mods = {top_module}
    current = top_module
    parts = HIER_SEP_RE.split(inst_path)
    for inst in parts[:-1]:
        child = hierarchy.get(current, {}).get(_clean_instance(inst))
        if child is None:
            return mods, True
        current = child
        mods.add(current)
    return mods, False


def logical_stem(full_name: str) -> str:
    """Pin name -> register-array stem (drop /<pin>, $-tag, and [N] indices)."""
    return INDEX_RE.sub("", full_name.rsplit("/", 1)[0].split("$", 1)[0])


# --------------------------------------------------------------------------- #
# Per-iteration plumbing                                                      #
# --------------------------------------------------------------------------- #


def design_scope_dir(design: DesignConfig, batch_dir: Path) -> Path:
    """Root for this design's scope artifacts (iter dirs + summary)."""
    return batch_dir / ITER_SCOPE_DIR / design.benchmark / design.name


def iter_output_dir(design: DesignConfig, batch_dir: Path, i: int) -> Path:
    """Per-iteration phase dir; same shape as `__iter_calibration__/iter_i`
    so analyse.py / extract_metrics.py see a familiar layout."""
    return design_scope_dir(design, batch_dir) / f"iter_{i}"


def run_one(
    design: DesignConfig,
    batch_dir: Path,
    i: int,
    period_ns: float,
    side_um: float,
    cfg: StudyConfig,
) -> tuple[RunConfig, float]:
    """One ORFS pass at `period_ns`. Returns the RunConfig and the
    post-route worst slack (ns)."""
    run = RunConfig(
        synth_target=TargetConfig(
            design=design,
            period_ns=period_ns,
            side_um=side_um,
            cfg=cfg,
        ),
        output_dir=iter_output_dir(design, batch_dir, i),
        flow_targets=ALL_FLOW_TARGETS,
    )
    rc = run_job(run)
    if rc != 0:
        raise RuntimeError(
            f"iter {i} ORFS run failed (rc={rc}); see {run.output_dir}/flow.log"
        )
    metrics = extract(run.output_dir, design.top_module)
    ws = metrics["route_ws_ns"]
    if math.isnan(ws):
        raise RuntimeError(f"iter {i} produced NaN route_ws_ns")
    return run, ws


# --------------------------------------------------------------------------- #
# Fix-scope measurement                                                       #
# --------------------------------------------------------------------------- #


def measure_fix_scope(
    phase_dir: Path,
    top_module: str,
    platform: str,
    hier: dict[str, dict[str, str]],
    libs: list[Path],
    pool: int,
) -> dict[str, Any]:
    """Run OpenROAD STA on the routed DB, parse the path pool, and roll
    up modules / starting registers across every `slack<0` path.

    On OpenROAD/parse failure returns `{"scope_error": <msg>, ...}` with
    the count fields set to None -- the caller treats this as fix_ok=False
    but does not abort the whole scope run."""
    try:
        odb, sdc, spef = locate_results(phase_dir, top_module, platform, "6_final")
        reports_dir = phase_dir / "reports" / platform / top_module / "base"
        reports_dir.mkdir(parents=True, exist_ok=True)
        raw_tsv = reports_dir / "critical_paths_raw.tsv"
        run_openroad_extract(odb, sdc, spef, libs, pool, raw_tsv)
        records = read_pool_tsv(raw_tsv)
    except Exception as exc:
        return {
            "n_failing": None,
            "n_modules": None,
            "n_starts": None,
            "modules": None,
            "scope_error": f"{type(exc).__name__}: {exc}",
        }

    failing = [r for r in records if r[0] < 0]
    starts = {logical_stem(sp) for _, sp, _, _ in failing}
    mods: set[str] = set()
    for _, _, _, cells in failing:
        for cell in cells:
            cell_mods, _ = cell_modules(cell, top_module, hier)
            mods |= cell_mods
    return {
        "n_failing": len(failing),
        "n_modules": len(mods),
        "n_starts": len(starts),
        "modules": sorted(mods),
        "scope_error": None,
    }


@dataclass(frozen=True)
class Budgets:
    """Suitability thresholds + the precomputed totals needed for the
    saturation guards."""

    max_modules: int
    max_starts: int
    pool: int


def classify_fix(ws_ns: float, scope_m: dict, b: Budgets) -> tuple[bool, str]:
    """Decide fix_ok for one iteration, with a tag for why. The tag is
    `"timing_met"` for `ws>=0`, `"ok"` when all budgets pass, or one of
    the rejection reasons otherwise."""
    if ws_ns >= 0:
        # ws_ns>=0 means timing is met -- no failing paths to fix. The
        # caller treats this as "still has headroom"; the period gets
        # shrunk and T_baseline is re-anchored to this period (see
        # Phase B body).
        return True, "timing_met"
    if scope_m.get("scope_error"):
        return False, "scope_error"
    n_failing = scope_m["n_failing"]
    if n_failing >= 0.9 * b.pool:
        return False, "pool_saturated"
    if scope_m["n_modules"] > b.max_modules:
        return False, "modules_exceeded"
    if scope_m["n_starts"] > b.max_starts:
        return False, "starts_exceeded"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Phase A: find T_baseline                                                    #
# --------------------------------------------------------------------------- #


def phase_a(
    design: DesignConfig,
    cfg: StudyConfig,
    args: argparse.Namespace,
    batch_dir: Path,
    history: list[dict],
) -> tuple[float | None, float, str | None]:
    """Returns `(T_baseline, last_ws, abort_reason)`. `T_baseline` is the
    converged period (`t - ws` after the last iter) when convergence
    succeeded; otherwise None and `abort_reason` is set.

    Convergence test: `|t_next - t| < eps`. Aborts (T_baseline=None,
    reason="baseline_not_met") if the final iter still has `ws < -eps`
    after exhausting the budget -- the design can't reach timing at any
    period close to where the iteration landed, so headroom analysis
    is meaningless."""
    t = args.initial_period_ns
    t_next = t
    ws = 0.0
    for _ in range(args.baseline_iters):
        i = len(history)
        run, ws = run_one(design, batch_dir, i, t, args.die_side_um, cfg)
        t_next = t - ws
        converged = abs(t_next - t) < args.eps
        history.append(
            {
                "iter": i,
                "phase": "A",
                "period_ns": t,
                "ws_ns": ws,
                "n_failing": None,
                "n_modules": None,
                "n_starts": None,
                "modules": None,
                "scope_error": None,
                "fix_ok": None,
                "fix_reason": "baseline",
                "output_dir": str(run.output_dir),
            }
        )
        print(
            f"[A iter {i}] period={t:.4f} ns -> ws={ws:+.4f} ns"
            f"{'  (converged)' if converged else ''}"
        )
        if converged:
            return t_next, ws, None
        t = t_next
    # Didn't converge within the budget. If the final iter is far below
    # timing-met, we can't trust `t_next` as a baseline.
    if ws < -args.eps:
        return None, ws, "baseline_not_met"
    return t_next, ws, None


# --------------------------------------------------------------------------- #
# Phase B: step past T_baseline                                               #
# --------------------------------------------------------------------------- #


def phase_b(
    design: DesignConfig,
    cfg: StudyConfig,
    args: argparse.Namespace,
    batch_dir: Path,
    history: list[dict],
    t_baseline: float,
    hier: dict,
    libs: list[Path],
    budgets: Budgets,
) -> tuple[float, float, float | None, RunConfig | None, str | None]:
    """Step period down from `t_baseline` by `step` per iter. At each
    step measure fix scope and check budgets.

    Returns
        (t_baseline_after, t_good, t_bad, sweet_run, abort_reason)

    `t_baseline_after` is `t_baseline` unless ws went positive at a
    sub-baseline period, in which case the baseline is re-anchored to
    that period so the reported improvement remains honest.
    `t_good` is the smallest fix_ok period reached; if no Phase B iter
    landed in a fix_ok state, `t_good == t_baseline_after` and the
    bisection bracket runs `(t_bad, t_baseline_after)`.
    `t_bad` is the first non-fix_ok sub-baseline period, or None when
    Phase B exhausted its budget without ever breaking.
    `sweet_run` carries the RunConfig of the best fix_ok period (or
    None when nothing fix_ok was seen).
    `abort_reason` is set if Phase B halted early (period floor)."""
    t_good = t_baseline
    t_bad: float | None = None
    sweet_run: RunConfig | None = None
    abort_reason: str | None = None

    t = t_baseline * args.step
    for _ in range(args.scope_iters):
        if t < t_baseline * args.min_period_frac:
            abort_reason = "period_floor_reached"
            break
        i = len(history)
        run, ws = run_one(design, batch_dir, i, t, args.die_side_um, cfg)
        scope_m = measure_fix_scope(
            run.output_dir,
            design.top_module,
            cfg.platform,
            hier,
            libs,
            args.pool,
        )
        fix_ok, reason = classify_fix(ws, scope_m, budgets)
        history.append(
            {
                "iter": i,
                "phase": "B",
                "period_ns": t,
                "ws_ns": ws,
                **scope_m,
                "fix_ok": fix_ok,
                "fix_reason": reason,
                "output_dir": str(run.output_dir),
            }
        )
        print(_fmt_scope_line("B", i, t, ws, scope_m, fix_ok, reason))

        if ws >= 0:
            # Found extra headroom; re-anchor baseline.
            t_baseline = t
            t_good = t
            sweet_run = run
            t *= args.step
            continue
        if fix_ok:
            t_good = t
            sweet_run = run
            t *= args.step
        else:
            t_bad = t
            break

    return t_baseline, t_good, t_bad, sweet_run, abort_reason


# --------------------------------------------------------------------------- #
# Phase C: bisect                                                             #
# --------------------------------------------------------------------------- #


def phase_c(
    design: DesignConfig,
    cfg: StudyConfig,
    args: argparse.Namespace,
    batch_dir: Path,
    history: list[dict],
    t_good: float,
    t_bad: float,
    sweet_run: RunConfig | None,
    hier: dict,
    libs: list[Path],
    budgets: Budgets,
) -> tuple[float, RunConfig | None]:
    """Bisect `(t_bad, t_good)` for `bisect_iters` rounds. Returns the
    refined `(T_sweet, sweet_run)`; `sweet_run` may stay as-passed if
    no bisection midpoint improved on it."""
    lo, hi = t_bad, t_good
    for _ in range(args.bisect_iters):
        mid = (lo + hi) / 2
        i = len(history)
        run, ws = run_one(design, batch_dir, i, mid, args.die_side_um, cfg)
        scope_m = measure_fix_scope(
            run.output_dir,
            design.top_module,
            cfg.platform,
            hier,
            libs,
            args.pool,
        )
        fix_ok, reason = classify_fix(ws, scope_m, budgets)
        history.append(
            {
                "iter": i,
                "phase": "C",
                "period_ns": mid,
                "ws_ns": ws,
                **scope_m,
                "fix_ok": fix_ok,
                "fix_reason": reason,
                "output_dir": str(run.output_dir),
            }
        )
        print(_fmt_scope_line("C", i, mid, ws, scope_m, fix_ok, reason))
        if fix_ok and ws < 0:
            hi = mid
            sweet_run = run
        else:
            lo = mid
    return hi, sweet_run


# --------------------------------------------------------------------------- #
# Output formatting                                                           #
# --------------------------------------------------------------------------- #


def _fmt_scope_line(
    phase: str,
    i: int,
    t: float,
    ws: float,
    scope_m: dict,
    fix_ok: bool,
    reason: str,
) -> str:
    if scope_m.get("scope_error"):
        body = f"scope_error={scope_m['scope_error']}"
    else:
        body = (
            f"failing={scope_m['n_failing']} modules={scope_m['n_modules']} "
            f"starts={scope_m['n_starts']}"
        )
    tag = "fix_ok" if fix_ok else f"fix_break({reason})"
    return f"[{phase} iter {i}] period={t:.4f} ns -> ws={ws:+.4f} ns  {body}  {tag}"


def print_summary(
    t_baseline: float | None,
    t_sweet: float | None,
    improvement: float | None,
    area_um2: float | None,
    sweet_scope: dict | None,
    suitable: bool,
    reason: str | None,
) -> None:
    print()
    if t_baseline is not None:
        print(f"T_baseline:        {t_baseline:.4f} ns")
    if t_sweet is not None:
        print(f"T_sweet:           {t_sweet:.4f} ns")
    if improvement is not None:
        print(f"Improvement:       {improvement * 100:.1f}%")
    if area_um2 is not None:
        print(f"Sweet-spot area:   {area_um2:.1f} um^2")
    if sweet_scope is not None and sweet_scope.get("modules") is not None:
        print(
            f"Fix scope:         {sweet_scope['n_modules']} modules / "
            f"{sweet_scope['n_starts']} start regs"
        )
        print(f"  modules:         {','.join(sweet_scope['modules'])}")
    print()
    verdict = "YES" if suitable else "NO"
    detail = (
        f"improvement {improvement * 100:.1f}%"
        if improvement is not None
        else "no headroom"
    )
    if not suitable and reason:
        detail = f"{detail}, {reason}"
    print(f"Suitable for optimisation: {verdict} ({detail})")


# --------------------------------------------------------------------------- #
# Main entry point                                                            #
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--design",
        required=True,
        help="Name of the design to scope.",
    )
    parser.add_argument("--initial-period-ns", type=float, default=0.5)
    parser.add_argument("--die-side-um", type=float, default=1000.0)
    parser.add_argument("--baseline-iters", type=int, default=3)
    parser.add_argument("--scope-iters", type=int, default=5)
    parser.add_argument("--bisect-iters", type=int, default=2)
    parser.add_argument(
        "--step",
        type=float,
        default=0.9,
        help="Period reduction factor per Phase B iter.",
    )
    parser.add_argument(
        "--min-period-frac",
        type=float,
        default=0.4,
        help="Phase B halts when period drops below T_baseline * this fraction.",
    )
    parser.add_argument(
        "--eps",
        type=float,
        default=0.02,
        help="Phase A convergence threshold on period delta (ns).",
    )
    parser.add_argument("--max-modules", type=int, default=6)
    parser.add_argument("--max-start-stems", type=int, default=5)
    parser.add_argument("--min-improvement", type=float, default=0.10)
    parser.add_argument(
        "--pool",
        type=int,
        default=10000,
        help="OpenSTA path pool size for fix-scope measurement.",
    )
    args = parser.parse_args()

    cfg = StudyConfig()
    design = resolve_design(args.design)

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    scope_root = design_scope_dir(design, batch_dir)
    scope_root.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {scope_root}")
    print(
        f"Scoping {design.benchmark}/{design.name}/{design.variant} "
        f"(top={design.top_module})"
    )

    # Pre-compute hierarchy + liberty once. yosys hierarchy reads the
    # original RTL sources from disk (not the snapshotted copies under
    # each iter's inputs/), so this is design-invariant across all iters.
    hier_json = scope_root / "hierarchy.json"
    include_dirs = [design.root / design.rtl_dir / d for d in design.include_dirs]
    dump_hierarchy(
        design.rtl_abs_paths,
        design.top_module,
        hier_json,
        include_dirs,
    )
    hier = load_hierarchy(hier_json)
    if design.top_module not in hier:
        raise RuntimeError(
            f"top module {design.top_module!r} not found in yosys hierarchy: "
            f"{sorted(hier)}"
        )
    libs = liberty_files(cfg.platform)
    budgets = Budgets(
        max_modules=args.max_modules,
        max_starts=args.max_start_stems,
        pool=args.pool,
    )
    print(
        f"Hierarchy: {len(hier)} modules. "
        f"Budgets: <={args.max_modules} modules, <={args.max_start_stems} starts."
    )

    history: list[dict] = []
    summary: dict[str, Any] = {
        "benchmark": design.benchmark,
        "name": design.name,
        "variant": design.variant,
        "top_module": design.top_module,
        "budgets": {
            "max_modules": args.max_modules,
            "max_start_stems": args.max_start_stems,
            "min_improvement": args.min_improvement,
        },
    }

    # Phase A
    t_baseline, last_ws, a_abort = phase_a(
        design,
        cfg,
        args,
        batch_dir,
        history,
    )
    if a_abort is not None:
        summary.update(
            {
                "T_baseline_ns": t_baseline,
                "T_sweet_ns": None,
                "improvement": None,
                "route_area_um2_at_sweet": None,
                "fix_scope_at_sweet": None,
                "bracket_complete": False,
                "suitable": False,
                "reason": a_abort,
                "history": history,
            }
        )
        _finalise(summary, scope_root)
        print_summary(t_baseline, None, None, None, None, False, a_abort)
        return

    assert t_baseline is not None

    # Phase B
    t_baseline, t_good, t_bad, sweet_run, b_abort = phase_b(
        design,
        cfg,
        args,
        batch_dir,
        history,
        t_baseline,
        hier,
        libs,
        budgets,
    )

    # Phase C
    bracket_complete = t_bad is not None
    if t_bad is not None:
        t_sweet, sweet_run = phase_c(
            design,
            cfg,
            args,
            batch_dir,
            history,
            t_good,
            t_bad,
            sweet_run,
            hier,
            libs,
            budgets,
        )
    else:
        t_sweet = t_good if sweet_run is not None else None

    # Verdict
    improvement: float | None = None
    area_um2: float | None = None
    sweet_scope: dict | None = None
    if t_sweet is not None and sweet_run is not None and t_sweet < t_baseline:
        improvement = (t_baseline - t_sweet) / t_baseline
        try:
            area_um2 = extract(sweet_run.output_dir, design.top_module)[
                "route_area_um2"
            ]
        except Exception:
            traceback.print_exc()
            area_um2 = None
        # Find the history entry for the sweet run to surface the scope.
        sweet_dir = str(sweet_run.output_dir)
        for h in reversed(history):
            if (
                h.get("output_dir") == sweet_dir
                and h.get("phase") in ("B", "C")
                and h.get("fix_ok")
            ):
                sweet_scope = {
                    "n_modules": h["n_modules"],
                    "n_starts": h["n_starts"],
                    "modules": h["modules"],
                }
                break

    if t_sweet is None or sweet_run is None or t_sweet >= t_baseline:
        suitable = False
        reason = b_abort or "no_local_headroom"
    elif improvement is not None and improvement >= args.min_improvement:
        suitable = True
        reason = None
    else:
        suitable = False
        reason = b_abort if b_abort else "insufficient_headroom"

    summary.update(
        {
            "T_baseline_ns": t_baseline,
            "T_sweet_ns": t_sweet,
            "improvement": improvement,
            "route_area_um2_at_sweet": area_um2,
            "fix_scope_at_sweet": sweet_scope,
            "bracket_complete": bracket_complete,
            "suitable": suitable,
            "reason": reason,
            "history": history,
        }
    )
    _finalise(summary, scope_root)
    print_summary(
        t_baseline,
        t_sweet,
        improvement,
        area_um2,
        sweet_scope,
        suitable,
        reason,
    )


def _finalise(summary: dict, scope_root: Path) -> None:
    """Atomic write of scope.json."""
    out = scope_root / "scope.json"
    tmp = out.with_suffix(".json.partial")
    tmp.write_text(json.dumps(summary, indent=2, default=str))
    tmp.replace(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
