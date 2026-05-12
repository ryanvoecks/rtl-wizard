#!/usr/bin/env python3
"""Drive the ORFS make-based flow over every discovered design, or a subset
selected by glob filters.

Designs come from `loader.CorpusLoader` (each corpus subfolder yields one
`DesignConfig` whose `rtl_files` are its `*.v` sources). The CLI's
`--benchmark`/`--name`/`--variant` flags filter that catalog: each is
repeatable and accepts globs; a design matches a flag when *any* of its
patterns hits, and must match every flag that's present. The shared SDC
template (`templates/constraint.sdc.template`) and the rendered per-design
Makefile (`templates/Makefile.template`) apply to every design — only the
design name, source files, DESIGN_DIR, clock period, and floorplan
dimensions vary per run.

All study parameters (platform, calibration setup, target utilization,
floor, safety factors) live in `config.StudyConfig`, not on the CLI.

Per-design clock period and die size are both derived from a single
*calibration* phase that runs the design at a loose period
(`StudyConfig.calibration_period_ns`) on a large square die
(`StudyConfig.calibration_side_um`). From that run we read:

  - the post-route worst setup slack -> tightened period for the final phase
    via `target_period = (cal_period - cal_ws) * target_multiplier`
  - the post-synth cell area -> floorplan side for the final phase via
    `side = sqrt(cell_area / target_utilization) + 2*core_margin`,
    clamped to a minimum of `minimum_side_um` so small designs hit a fixed
    floor instead of an impractically tiny die.

Both phases use the same SDC shape, so the final WNS is interpretable
against the calibration's.

Each (benchmark, name) group has one shared calibration that's always run
on the `reference` variant; the derived period/floorplan are then used for
the final routed run of every variant of that design (including the
reference itself). Filtering out the reference still runs calibration on
it under the hood — calibration is a hidden dependency, not a selectable
unit.

Each invocation is one *batch*: artifacts land under

    <repo>/eda_runs/<timestamp>/<benchmark>/<name>/
        __calibration__/   # shared calibration phase, fed by the reference
        <variant>/         # one subdir per variant -- the final routed run

Each phase dir is self-contained — its own `inputs/` snapshot (rtl +
rendered constraint.sdc + rendered Makefile) sits next to ORFS's
`logs/objects/reports/results/` trees and the make log.

The flow always runs through `do-finish` (final routed STA/area/power
report) rather than ORFS's `finish`, which additionally depends on GDS
generation via KLayout — not installed in every sandbox.

Usage:
    uv run eda_eval/run.py                                # every discovered design
    uv run eda_eval/run.py --name adder8                  # one design by name
    uv run eda_eval/run.py --name 'adder*' --name 'mux*'  # multiple name globs
    uv run eda_eval/run.py --benchmark corpus             # entire benchmark
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from config import (
    EDA_RUNS,
    HERE,
    ORFS_HOME,
    DesignConfig,
    RunConfig,
    StudyConfig,
)
from loader import CorpusLoader, DesignTree, RTLLMLoader

SDC_TEMPLATE = HERE / "templates" / "constraint.sdc.template"
MAKEFILE_TEMPLATE = HERE / "templates" / "Makefile.template"

# Full-precision total stdcell area from yosys `stat`. The `cells` totals
# row uses %g formatting and flips to scientific notation (e.g.
# `1.32E+03`) for designs over ~1000 um^2, which is both fragile to parse
# and lossy. The `Chip area for module '<top>'` line yosys prints below
# the per-cell breakdown carries the same number to full precision.
_CHIP_AREA_RE = re.compile(
    r"^\s*Chip area for module\s+'[^']+'\s*:\s*([\d.eE+-]+)\s*$", re.MULTILINE,
)


def filter_designs(
    designs: DesignTree,
    benchmark_globs: list[str] | None,
    name_globs: list[str] | None,
    variant_globs: list[str] | None,
) -> DesignTree:
    """Keep designs whose benchmark/name/variant match at least one glob in
    each non-empty filter list. A `None` filter means that field is
    unconstrained, so an unfiltered call returns `designs` unchanged."""
    def matches(value: str, patterns: list[str] | None) -> bool:
        return patterns is None or any(fnmatch.fnmatchcase(value, p) for p in patterns)
    out: DesignTree = {}
    for b, names in designs.items():
        if not matches(b, benchmark_globs):
            continue
        for n, variants in names.items():
            if not matches(n, name_globs):
                continue
            for v, d in variants.items():
                if matches(v, variant_globs):
                    out.setdefault(b, {}).setdefault(n, {})[v] = d
    return out


def design_variant_dir(design: DesignConfig, batch_dir: Path) -> Path:
    """Output dir for this variant's final (target-period) run."""
    return batch_dir / design.benchmark / design.name / design.variant


def design_calibration_dir(design: DesignConfig, batch_dir: Path) -> Path:
    """Output dir for the calibration phase shared by every variant of this
    (benchmark, name). Named `__calibration__` so it can't collide with a
    real variant name."""
    return batch_dir / design.benchmark / design.name / "__calibration__"


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


def snapshot_inputs(run: RunConfig, cfg: StudyConfig) -> Path:
    """Materialize `<run.output_dir>/inputs/` with an RTL copy, a rendered SDC
    at `run.period_ns`, and a rendered per-design Makefile pinned to a square
    `run.side_um` floorplan. Returns the path to the rendered Makefile."""
    inputs = run.output_dir / "inputs"
    rtl_dst = inputs / "rtl"
    if rtl_dst.exists():
        shutil.rmtree(rtl_dst)
    rtl_dst.mkdir(parents=True)
    verilog_dsts: list[Path] = []
    for src in run.design.rtl_files:
        dst = rtl_dst / src.name
        shutil.copy2(src, dst)
        verilog_dsts.append(dst)
    sdc_dst = inputs / "constraint.sdc"
    sdc_dst.write_text(SDC_TEMPLATE.read_text().format(
        period_ns=run.period_ns, io_delay_ns=cfg.io_delay_ns,
    ))
    die_area, core_area = render_floorplan(run.side_um, cfg.core_margin_um)
    makefile_dst = inputs / "Makefile"
    makefile_dst.write_text(
        MAKEFILE_TEMPLATE.read_text().format(
            top_module=run.design.top_module,
            design_dir=rtl_dst,
            verilog_files=" ".join(str(v) for v in verilog_dsts),
            sdc_file=sdc_dst,
            work_home=run.output_dir,
            orfs_home=ORFS_HOME,
            platform=cfg.platform,
            place_density=cfg.place_density,
            die_area=die_area,
            core_area=core_area,
        )
    )
    return makefile_dst


def run_phase(run: RunConfig, cfg: StudyConfig) -> int:
    """Invoke the rendered per-design Makefile for one phase. Output is
    captured to `<output_dir>/flow_<design>_<phase_label>.log`."""
    run.output_dir.mkdir(parents=True, exist_ok=True)
    makefile = snapshot_inputs(run, cfg)
    log_path = run.output_dir / f"flow_{run.design.name}_{run.phase_label}.log"
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


def read_worst_setup_slack_ns(phase_dir: Path, top_module: str) -> float:
    """Post-route worst setup slack in ns (positive when timing is met with
    margin; negative when violated). ORFS keys output paths off DESIGN_NAME,
    which we set to the design's `top_module`."""
    report = find_unique(
        phase_dir, f"logs/*/{top_module}/*/6_report.json", "6_report.json",
    )
    return float(json.loads(report.read_text())["finish__timing__setup__ws"])


def read_synth_cell_area_um2(phase_dir: Path, top_module: str) -> float:
    """Top-module stdcell area in um^2 from yosys's synth_stat.txt
    `Chip area for module '<top>'` line. ORFS keys output paths off
    DESIGN_NAME, which we set to the design's `top_module`."""
    report = find_unique(
        phase_dir, f"reports/*/{top_module}/*/synth_stat.txt", "synth_stat.txt",
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


def run_phases(
    runs: list[RunConfig],
    cfg: StudyConfig,
    num_threads: int,
    desc: str,
) -> dict[RunConfig, int]:
    """Drive each `RunConfig` through `run_phase` on a thread pool, showing
    a progress bar labeled `desc`. Returns `{run: returncode}`; a non-zero
    rc is a make failure, and any unexpected exception is mapped to rc=1
    with an error line written above the bar."""
    if not runs:
        return {}
    workers = max(1, min(num_threads, len(runs)))
    rcs: dict[RunConfig, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_phase, r, cfg): r for r in runs}
        with tqdm(total=len(runs), desc=desc, unit="run") as pbar:
            for fut in as_completed(futures):
                r = futures[fut]
                d = r.design
                label = f"{d.benchmark}/{d.name}/{d.variant} {r.phase_label}"
                try:
                    rc = fut.result()
                except Exception as exc:
                    rc = 1
                    pbar.write(f"  {label:<50} ERROR: {exc}")
                else:
                    status = "OK" if rc == 0 else f"FAIL (rc={rc})"
                    pbar.write(f"  {label:<50} {status}")
                rcs[r] = rc
                pbar.update(1)
    return rcs


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--benchmark", action="append", default=None, metavar="GLOB",
        help="Restrict to designs whose benchmark matches one of these globs. "
             "Repeatable; a design matches if *any* given glob hits.",
    )
    parser.add_argument(
        "--name", action="append", default=None, metavar="GLOB",
        help="Restrict to designs whose name matches one of these globs. "
             "Repeatable.",
    )
    parser.add_argument(
        "--variant", action="append", default=None, metavar="GLOB",
        help="Restrict to designs whose variant matches one of these globs. "
             "Repeatable.",
    )
    parser.add_argument(
        "--num-threads", type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Worker count for each parallel phase (default: half of host "
             "CPU count). All calibrations run as one batch, then all finals "
             "run as a second batch.",
    )
    args = parser.parse_args()

    cfg = StudyConfig()

    if not (ORFS_HOME / "Makefile").is_file():
        raise FileNotFoundError("ORFS flow not found - set config.ORFS_HOME")

    all_designs = CorpusLoader().designs() | RTLLMLoader().designs()
    designs = filter_designs(
        all_designs, args.benchmark, args.name, args.variant,
    )
    if not designs:
        raise ValueError("No designs matched specified filters")

    # Group filtered designs and check for valid references
    references: dict[tuple[str, str], DesignConfig] = {}
    groups: dict[tuple[str, str], list[DesignConfig]] = {}
    for b, names in designs.items():
        for n, variants in names.items():
            if "reference" not in all_designs[b][n]:
                raise ValueError(f"no 'reference' variant available for {b}/{n}")
            groups[(b, n)] = list(variants.values())
            references[(b, n)] = all_designs[b][n]["reference"]

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / batch_ts
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"Batch output: {batch_dir}")
    print(
        f"Platform: {cfg.platform}  "
        f"Calibration: period={cfg.calibration_period_ns} ns, "
        f"side={cfg.calibration_side_um} um"
    )
    print(
        f"Final: target_mult={cfg.target_multiplier}, "
        f"target_util={cfg.target_utilization}, "
        f"area_mult={cfg.area_multiplier}, "
        f"min_side={cfg.minimum_side_um} um, "
        f"core_margin={cfg.core_margin_um} um"
    )

    # Phase 1: run every group's calibration (on the reference variant).
    cal_runs: dict[tuple[str, str], RunConfig] = {
        key: RunConfig(
            design=references[key],
            output_dir=design_calibration_dir(references[key], batch_dir),
            period_ns=cfg.calibration_period_ns,
            side_um=cfg.calibration_side_um,
            phase_label="calibration",
        )
        for key in groups
    }
    print(f"\nPhase 1: {len(cal_runs)} calibration runs")
    cal_rcs = run_phases(
        list(cal_runs.values()), cfg, args.num_threads, "Calibrations",
    )

    # Parse each successful calibration into a target period/side. Failures
    # propagate as a per-group reason that gets attached to each variant.
    targets: dict[tuple[str, str], tuple[float, float, str]] = {}
    cal_failures: dict[tuple[str, str], str] = {}
    for key, cal_run in cal_runs.items():
        rc = cal_rcs[cal_run]
        if rc != 0:
            cal_failures[key] = f"calibration FAIL (rc={rc})"
            continue
        try:
            ws_ns = read_worst_setup_slack_ns(
                cal_run.output_dir, cal_run.design.top_module,
            )
            cell_area_um2 = read_synth_cell_area_um2(
                cal_run.output_dir, cal_run.design.top_module,
            )
        except Exception as exc:
            cal_failures[key] = f"calibration parse FAIL: {exc}"
            continue
        target_period_ns = (cfg.calibration_period_ns - ws_ns) * cfg.target_multiplier
        final_side_um, _natural, _eff_area, at_floor = derive_final_side_um(
            cell_area_um2, cfg.target_utilization, cfg.minimum_side_um,
            cfg.core_margin_um, cfg.area_multiplier,
        )
        floor_tag = " (floor)" if at_floor else ""
        detail = (
            f"T={target_period_ns:.3f}ns side={final_side_um:.1f}um{floor_tag} "
            f"(cal_ws={ws_ns:.3f}ns cal_area={cell_area_um2:.1f}um2)"
        )
        targets[key] = (target_period_ns, final_side_um, detail)

    # Phase 2: run every variant whose group's calibration produced a target.
    final_runs: dict[DesignConfig, RunConfig] = {}
    for key, variants in groups.items():
        if key not in targets:
            continue
        period_ns, side_um, _detail = targets[key]
        for d in variants:
            final_runs[d] = RunConfig(
                design=d,
                output_dir=design_variant_dir(d, batch_dir),
                period_ns=period_ns,
                side_um=side_um,
                phase_label="final",
            )
    print(f"\nPhase 2: {len(final_runs)} final runs")
    final_rcs = run_phases(
        list(final_runs.values()), cfg, args.num_threads, "Finals",
    )

    # Stitch each variant's outcome together from the calibration and final
    # phases — calibration failures cascade to every variant in the group.
    results: list[tuple[str, int, str]] = []
    for key, variants in groups.items():
        for d in variants:
            label = f"{d.benchmark}/{d.name}/{d.variant}"
            if key in cal_failures:
                results.append((label, 1, cal_failures[key]))
                continue
            _, _, detail = targets[key]
            rc = final_rcs[final_runs[d]]
            if rc != 0:
                results.append((label, rc, f"final FAIL (rc={rc}) [{detail}]"))
            else:
                results.append((label, 0, detail))

    if len(results) > 1:
        print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
        for label, rc, detail in sorted(results):
            status = "OK" if rc == 0 else "FAIL"
            print(f"  {label:<40} {status:<4} {detail}")

    return 0 if all(rc == 0 for _, rc, _ in results) else 1


if __name__ == "__main__":
    main()
