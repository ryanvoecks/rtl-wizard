#!/usr/bin/env python3
"""Drive the ORFS make-based flow over the corpus, or a single design folder.

Each design folder must contain exactly one `*.v` file; its stem is used as
DESIGN_NAME. The shared SDC template (`templates/constraint.sdc.template`)
and the platform/utilization knobs in `templates/Makefile.template` apply to
every design — only the design name, source file, DESIGN_DIR, clock period,
and floorplan dimensions vary per run.

Per-design clock period and die size are both derived from a single
*calibration* phase that runs the design at a loose period
(`--calibration-period-ns`, default 10 ns) on a large square die
(`--calibration-side-um`, default 1000 um). From that run we read:

  - the post-route worst setup slack -> tightened period for the final phase
    via `target_period = (cal_period - cal_ws) * --target-multiplier`
    (default 1.1)
  - the post-synth cell area -> floorplan side for the final phase via
    `side = sqrt(cell_area / --target-utilization) + 2*core_margin`,
    clamped to a minimum of `--minimum-side-um` (default 50 um) so small
    designs hit a fixed floor instead of an impractically tiny die.

Both phases use the same SDC shape, so the final WNS is interpretable
against the calibration's.

Each invocation is one *batch*: artifacts land under
`<repo>/eda_runs/<timestamp>/<rel-corpus-path>/{calibration,final}/`, where
`<rel-corpus-path>` mirrors the design's location inside `corpus/`. Each
phase dir is self-contained — its own `inputs/` snapshot (rtl + rendered
constraint.sdc + rendered Makefile) sits next to ORFS's
`logs/objects/reports/results/` trees and the make log. A
`calibration_summary.json` at the parent records the derivation.

The flow always runs through `do-finish` (final routed STA/area/power
report) rather than ORFS's `finish`, which additionally depends on GDS
generation via KLayout — not installed in every sandbox.

Usage:
    uv run eda_eval/run.py                  # every design under corpus/
    uv run eda_eval/run.py corpus/adder8    # full flow on one design
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EDA_RUNS = REPO_ROOT / "eda_runs"
CORPUS = REPO_ROOT / "corpus"

# nangate45 die-to-core margin. Small enough not to dominate floorplan side
# at the 50 um floor, large enough to leave room for IO pin placement and
# the routing track grid. ORFS doesn't expose a portable default for this,
# so we bake it in here and let users override via --core-margin-um.
DEFAULT_CORE_MARGIN_UM = 2.0

# Full-precision total stdcell area from yosys `stat`. The `cells` totals
# row uses %g formatting and flips to scientific notation (e.g.
# `1.32E+03`) for designs over ~1000 um^2, which is both fragile to parse
# and lossy. The `Chip area for module '<top>'` line yosys prints below
# the per-cell breakdown carries the same number to full precision.
_CHIP_AREA_RE = re.compile(
    r"^\s*Chip area for module\s+'[^']+'\s*:\s*([\d.eE+-]+)\s*$", re.MULTILINE,
)


def resolve_design(design_dir: Path) -> tuple[str, Path]:
    """Locate the single `*.v` file in `design_dir`. The corpus convention is
    one design per folder; if that ever stops holding we want to fail loudly
    rather than guess."""
    candidates = sorted(design_dir.glob("*.v"))
    if not candidates:
        raise SystemExit(f"error: no *.v file in {design_dir}")
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(f"error: multiple *.v files in {design_dir}: {names}")
    verilog = candidates[0]
    return verilog.stem, verilog


def list_corpus_designs(corpus_dir: Path) -> list[Path]:
    """All subfolders of `corpus_dir` that contain at least one `.v` file."""
    if not corpus_dir.is_dir():
        return []
    return [
        child for child in sorted(corpus_dir.iterdir())
        if child.is_dir() and any(child.glob("*.v"))
    ]


def design_out_dir(design_dir: Path, batch_dir: Path, corpus: Path) -> Path:
    """Per-design output dir under `batch_dir`, mirroring the design's path
    relative to `corpus/`. Falls back to just the folder name for designs
    passed from outside the corpus tree."""
    try:
        rel = design_dir.resolve().relative_to(corpus.resolve())
    except ValueError:
        rel = Path(design_dir.name)
    return batch_dir / rel


def render_floorplan(side_um: float, core_margin_um: float) -> tuple[str, str]:
    """ORFS DIE_AREA/CORE_AREA strings for a square `side_um` x `side_um`
    die with a `core_margin_um` boundary on each edge."""
    inner = side_um - core_margin_um
    die_area = f"0 0 {side_um:.3f} {side_um:.3f}"
    core_area = (
        f"{core_margin_um:.3f} {core_margin_um:.3f} "
        f"{inner:.3f} {inner:.3f}"
    )
    return die_area, core_area


def snapshot_inputs(
    design_dir: Path,
    phase_dir: Path,
    sdc_template_src: Path,
    makefile_template_src: Path,
    period_ns: float,
    side_um: float,
    core_margin_um: float,
) -> Path:
    """Materialize `<phase_dir>/inputs/` with an RTL copy, a rendered SDC at
    `period_ns`, and a rendered per-design Makefile pinned to a square
    `side_um` floorplan. Returns the path to the rendered Makefile."""
    inputs = phase_dir / "inputs"
    rtl_dst = inputs / "rtl"
    if rtl_dst.exists():
        shutil.rmtree(rtl_dst)
    shutil.copytree(design_dir, rtl_dst)
    sdc_dst = inputs / "constraint.sdc"
    sdc_dst.write_text(sdc_template_src.read_text().format(period_ns=period_ns))
    design_name, verilog_in_src = resolve_design(design_dir)
    verilog_dst = rtl_dst / verilog_in_src.name
    die_area, core_area = render_floorplan(side_um, core_margin_um)
    makefile_dst = inputs / "Makefile"
    makefile_dst.write_text(
        makefile_template_src.read_text().format(
            design_name=design_name,
            design_dir=verilog_dst.parent,
            verilog_files=verilog_dst,
            sdc_file=sdc_dst,
            work_home=phase_dir,
            die_area=die_area,
            core_area=core_area,
        )
    )
    return makefile_dst


def run_phase(
    design_dir: Path,
    phase_dir: Path,
    sdc_template_src: Path,
    makefile_template_src: Path,
    period_ns: float,
    side_um: float,
    core_margin_um: float,
    phase_label: str,
) -> int:
    """Invoke the rendered per-design Makefile for one phase. Output is
    captured to `<phase_dir>/flow_<design>_<phase_label>.log`."""
    design_name, _ = resolve_design(design_dir)
    phase_dir.mkdir(parents=True, exist_ok=True)
    makefile = snapshot_inputs(
        design_dir, phase_dir, sdc_template_src, makefile_template_src,
        period_ns, side_um, core_margin_um,
    )
    log_path = phase_dir / f"flow_{design_name}_{phase_label}.log"
    with log_path.open("w") as log:
        return subprocess.run(
            ["make", "-C", str(makefile.parent)],
            stdout=log, stderr=subprocess.STDOUT,
        ).returncode


def find_unique(phase_dir: Path, glob_pat: str, label: str) -> Path:
    """Return the single match for `glob_pat` under `phase_dir`. The
    platform/variant segments are glob-discovered rather than hardcoded so
    changing `PLATFORM` in Makefile.template doesn't require code edits."""
    matches = list(phase_dir.glob(glob_pat))
    if not matches:
        raise FileNotFoundError(
            f"no {label} under {phase_dir}/{glob_pat} — "
            "calibration flow likely failed before producing it"
        )
    if len(matches) > 1:
        joined = ", ".join(str(m) for m in matches)
        raise RuntimeError(f"multiple {label} matches: {joined}")
    return matches[0]


def read_worst_setup_slack_ns(phase_dir: Path, design_name: str) -> float:
    """Post-route worst setup slack in ns (positive when timing is met with
    margin; negative when violated)."""
    report = find_unique(
        phase_dir, f"logs/*/{design_name}/*/6_report.json", "6_report.json",
    )
    return float(json.loads(report.read_text())["finish__timing__setup__ws"])


def read_synth_cell_area_um2(phase_dir: Path, design_name: str) -> float:
    """Top-module stdcell area in um^2 from yosys's synth_stat.txt
    `Chip area for module '<top>'` line."""
    report = find_unique(
        phase_dir, f"reports/*/{design_name}/*/synth_stat.txt", "synth_stat.txt",
    )
    m = _CHIP_AREA_RE.search(report.read_text())
    if not m:
        raise ValueError(
            f"could not parse 'Chip area for module' line from {report}"
        )
    return float(m.group(1))


def derive_final_side_um(
    cell_area_um2: float,
    target_utilization: float,
    minimum_side_um: float,
    core_margin_um: float,
    area_multiplier: float,
) -> tuple[float, float, float, bool]:
    """Pick the final floorplan side.

    The calibration synth runs at a loose period, which tends to pick
    smaller drive strengths than the final tight-period synth — so the
    calibration cell area is multiplied by `area_multiplier` before sizing
    the die, padding the budget for the larger cells the final synth will
    likely pick.

    Returns `(side_um, natural_side_um, effective_cell_area_um2, at_floor)`:
      - `effective_cell_area_um2` is the inflated area used for sizing.
      - `natural_side_um` is what `target_utilization` alone would dictate
        against `effective_cell_area_um2` (core side derived from area, plus
        die margin on each edge).
      - `side_um` is the value actually used — `natural_side_um` unless that
        would fall below `minimum_side_um`, in which case the floor wins.
      - `at_floor` is True when the floor was applied.
    """
    effective_cell_area = cell_area_um2 * area_multiplier
    core_side = math.sqrt(effective_cell_area / target_utilization)
    natural_side = core_side + 2 * core_margin_um
    if natural_side < minimum_side_um:
        return minimum_side_um, natural_side, effective_cell_area, True
    return natural_side, natural_side, effective_cell_area, False


def write_calibration_summary(out_dir: Path, payload: dict) -> None:
    """Drop a tiny JSON next to the phase dirs so the derivation is easy to
    inspect after the fact."""
    (out_dir / "calibration_summary.json").write_text(
        json.dumps(payload, indent=2) + "\n"
    )


def run_flow(
    design_dir: Path,
    out_dir: Path,
    sdc_template_src: Path,
    makefile_template_src: Path,
    calibration_period_ns: float,
    calibration_side_um: float,
    target_multiplier: float,
    target_utilization: float,
    minimum_side_um: float,
    core_margin_um: float,
    area_multiplier: float,
) -> tuple[int, str]:
    """Two-phase flow: a calibration run at a loose period on a large die,
    then a final run at `(cal_period - cal_ws) * target_multiplier` with a
    floorplan sized to hit `target_utilization` against `cal_cell_area *
    area_multiplier` (subject to the `minimum_side_um` floor). Returns
    `(returncode, status_detail)`."""
    design_name, _ = resolve_design(design_dir)

    cal_dir = out_dir / "calibration"
    rc = run_phase(
        design_dir, cal_dir, sdc_template_src, makefile_template_src,
        calibration_period_ns, calibration_side_um, core_margin_um, "calibration",
    )
    if rc != 0:
        return rc, f"calibration FAIL (rc={rc})"

    try:
        cal_ws_ns = read_worst_setup_slack_ns(cal_dir, design_name)
        cal_cell_area_um2 = read_synth_cell_area_um2(cal_dir, design_name)
    except Exception as exc:
        return 1, f"calibration parse FAIL: {exc}"

    target_period_ns = (calibration_period_ns - cal_ws_ns) * target_multiplier
    (
        final_side_um, natural_side_um, effective_cell_area_um2, at_floor,
    ) = derive_final_side_um(
        cal_cell_area_um2, target_utilization, minimum_side_um, core_margin_um,
        area_multiplier,
    )

    write_calibration_summary(out_dir, {
        "calibration_period_ns": calibration_period_ns,
        "calibration_side_um": calibration_side_um,
        "calibration_ws_ns": cal_ws_ns,
        "calibration_cell_area_um2": cal_cell_area_um2,
        "target_multiplier": target_multiplier,
        "target_period_ns": target_period_ns,
        "target_utilization": target_utilization,
        "minimum_side_um": minimum_side_um,
        "core_margin_um": core_margin_um,
        "area_multiplier": area_multiplier,
        "effective_cell_area_um2": effective_cell_area_um2,
        "natural_side_um": natural_side_um,
        "final_side_um": final_side_um,
        "final_side_at_floor": at_floor,
    })

    final_dir = out_dir / "final"
    rc = run_phase(
        design_dir, final_dir, sdc_template_src, makefile_template_src,
        target_period_ns, final_side_um, core_margin_um, "final",
    )
    floor_tag = " (floor)" if at_floor else ""
    detail = (
        f"T={target_period_ns:.3f}ns side={final_side_um:.1f}um{floor_tag} "
        f"(cal_ws={cal_ws_ns:.3f}ns cal_area={cal_cell_area_um2:.1f}um2)"
    )
    if rc != 0:
        return rc, f"final FAIL (rc={rc}) [{detail}]"
    return 0, detail


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "design_dir", nargs="?", type=Path, default=None,
        help="Folder containing the design's .v file. "
             "Default: run every design under <repo>/corpus/.",
    )
    ap.add_argument(
        "--num-threads", type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Number of designs to run in parallel "
             "(default: half of host CPU count). Each design runs its "
             "calibration and final phases sequentially within its thread.",
    )
    ap.add_argument(
        "--calibration-period-ns", type=float, default=10.0,
        help="Clock period used for the calibration phase. Should be loose "
             "enough that even the slowest design in the corpus meets timing.",
    )
    ap.add_argument(
        "--calibration-side-um", type=float, default=1000.0,
        help="Side of the square die used for the calibration phase. Should "
             "be large enough that even the largest design in the corpus "
             "fits comfortably so synth area is measured unconstrained.",
    )
    ap.add_argument(
        "--target-multiplier", type=float, default=1.1,
        help="Safety factor on the calibration-derived minimum period. "
             "1.0 = run at the achievable minimum; >1 gives the final flow "
             "headroom to absorb optimization differences between phases.",
    )
    ap.add_argument(
        "--target-utilization", type=float, default=0.4,
        help="Target core utilization for the final phase. The floorplan "
             "side is derived to hit this density against the calibration "
             "cell area, unless --minimum-side-um is binding.",
    )
    ap.add_argument(
        "--area-multiplier", type=float, default=1.1,
        help="Safety factor on the calibration cell area before sizing. "
             "Tight-period synth tends to pick larger drive strengths than "
             "the loose calibration synth, so the floorplan is built for "
             "`cal_cell_area * --area-multiplier` rather than the raw value.",
    )
    ap.add_argument(
        "--minimum-side-um", type=float, default=50.0,
        help="Floor on the final floorplan side. Designs whose natural "
             "(utilization-derived) size would fall below this get padded "
             "out to a fixed minimum die instead.",
    )
    ap.add_argument(
        "--core-margin-um", type=float, default=DEFAULT_CORE_MARGIN_UM,
        help="Die-to-core boundary applied on each edge of the square die.",
    )
    args = ap.parse_args()

    if not 0.0 < args.target_utilization <= 1.0:
        sys.stderr.write(
            f"error: --target-utilization must be in (0, 1], got "
            f"{args.target_utilization}\n"
        )
        return 1
    if args.area_multiplier < 1.0:
        sys.stderr.write(
            f"error: --area-multiplier must be >= 1.0 (a safety factor), "
            f"got {args.area_multiplier}\n"
        )
        return 1

    # FLOW_HOME falls back to /OpenROAD-flow-scripts/flow inside the rendered
    # Makefile; check here too so we fail fast with a clear message rather
    # than deferring to a sub-make error.
    flow_home = Path(os.environ.get("FLOW_HOME", "/OpenROAD-flow-scripts/flow"))
    if not (flow_home / "Makefile").is_file():
        sys.stderr.write(
            f"error: ORFS flow Makefile not found at {flow_home / 'Makefile'}\n"
            "       set FLOW_HOME to your OpenROAD-flow-scripts/flow checkout.\n"
        )
        return 1

    sdc_template_src = HERE / "templates" / "constraint.sdc.template"
    makefile_template_src = HERE / "templates" / "Makefile.template"

    if args.design_dir is not None:
        design_dir = args.design_dir.resolve()
        if not design_dir.is_dir():
            sys.stderr.write(f"error: design dir not found: {design_dir}\n")
            return 1
        designs = [design_dir]
    else:
        designs = list_corpus_designs(CORPUS)
        if not designs:
            sys.stderr.write(f"error: no designs found under {CORPUS}\n")
            return 1

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Batch output: {batch_dir}")
    print(
        f"Calibration: period={args.calibration_period_ns} ns, "
        f"side={args.calibration_side_um} um"
    )
    print(
        f"Final: target_mult={args.target_multiplier}, "
        f"target_util={args.target_utilization}, "
        f"area_mult={args.area_multiplier}, "
        f"min_side={args.minimum_side_um} um, "
        f"core_margin={args.core_margin_um} um"
    )

    out_dirs = {d: design_out_dir(d, batch_dir, CORPUS) for d in designs}

    num_threads = max(1, min(args.num_threads, len(designs)))
    results: list[tuple[str, int, str]] = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(
                run_flow, d, out_dirs[d],
                sdc_template_src, makefile_template_src,
                args.calibration_period_ns, args.calibration_side_um,
                args.target_multiplier, args.target_utilization,
                args.minimum_side_um, args.core_margin_um,
                args.area_multiplier,
            ): d
            for d in designs
        }
        with tqdm(total=len(designs), desc="Designs", unit="design") as pbar:
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    rc, detail = fut.result()
                except Exception as exc:
                    rc, detail = 1, f"ERROR: {exc}"
                status = "OK" if rc == 0 else "FAIL"
                pbar.write(
                    f"  {d.name:<30} {status:<4} {detail}  "
                    f"(out: {out_dirs[d]})"
                )
                results.append((d.name, rc, detail))
                pbar.update(1)

    if len(results) > 1:
        print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
        for name, rc, detail in sorted(results):
            status = "OK" if rc == 0 else "FAIL"
            print(f"  {name:<30} {status:<4} {detail}")

    return 0 if all(rc == 0 for _, rc, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
