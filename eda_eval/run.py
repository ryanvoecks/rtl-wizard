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
    RunJob,
    StudyConfig,
)
from extract_metrics import extract
from loader import CorpusLoader, DesignTree, RTLLMLoader

SDC_TEMPLATE = HERE / "templates" / "constraint.sdc.template"
MAKEFILE_TEMPLATE = HERE / "templates" / "Makefile.template"


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
    """Output dir for this variant's run."""
    return batch_dir / design.benchmark / design.name / design.variant


def design_calibration_dir(design: DesignConfig, batch_dir: Path) -> Path:
    """Output dir for the calibration step."""
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
    captured to `<output_dir>/flow.log`."""
    run.output_dir.mkdir(parents=True, exist_ok=True)
    makefile = snapshot_inputs(run, cfg)
    log_path = run.output_dir / "flow.log"
    with log_path.open("w") as log:
        return subprocess.run(
            ["make", "-C", str(makefile.parent)],
            stdout=log, stderr=subprocess.STDOUT,
        ).returncode


def derive_final_side_um(cell_area_um2: float, cfg: StudyConfig) -> float:
    """Pick the final floorplan side. The calibration synth runs at a
    loose period, which tends to pick smaller drive strengths than the
    final tight-period synth — so the calibration cell area is multiplied
    by `cfg.area_multiplier` before sizing the die, padding the budget for
    the larger cells the final synth will likely pick. The natural side
    dictated by `cfg.target_utilization` is clamped up to
    `cfg.minimum_side_um` so small designs hit a fixed floor."""
    effective_cell_area = cell_area_um2 * cfg.area_multiplier
    core_side = math.sqrt(effective_cell_area / cfg.target_utilization)
    natural_side = core_side + 2 * cfg.core_margin_um
    return max(natural_side, cfg.minimum_side_um)


def run_phases(
    runs: list[RunConfig],
    cfg: StudyConfig,
    num_threads: int,
    desc: str,
) -> dict[RunConfig, int]:
    """Drive each `RunConfig` through `run_phase` on a thread pool, with a
    progress bar whose postfix tracks running pass/fail counts. Returns
    `{run: returncode}`; a non-zero rc is a make failure, and any
    unexpected exception is mapped to rc=1."""
    if not runs:
        return {}
    workers = max(1, min(num_threads, len(runs)))
    rcs: dict[RunConfig, int] = {}
    passed = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_phase, r, cfg): r for r in runs}
        with tqdm(total=len(runs), desc=desc, unit="run") as pbar:
            pbar.set_postfix(passed=passed, failed=failed)
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    rc = fut.result()
                except Exception:
                    rc = 1
                if rc == 0:
                    passed += 1
                else:
                    failed += 1
                rcs[r] = rc
                pbar.set_postfix(passed=passed, failed=failed)
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
    print(f"Output dir: {batch_dir}")

    # Phase 1: run every group's calibration (on the reference variant).
    cal_runs: dict[tuple[str, str], RunConfig] = {
        key: RunConfig(
            design=references[key],
            output_dir=design_calibration_dir(references[key], batch_dir),
            period_ns=cfg.calibration_period_ns,
            side_um=cfg.calibration_side_um,
        )
        for key in groups
    }
    cal_rcs = run_phases(
        list(cal_runs.values()), cfg, args.num_threads, "Calibration",
    )

    # Build one RunJob per variant, and record each group's calibration as
    # its own synthetic `__calibration__` row. Calibration failures (rc != 0
    # or unparseable outputs) attach as `error` on every variant in the
    # group and skip execution, but the RunJob still carries the design so
    # the row shows in runs.json.
    final_runs: dict[DesignConfig, RunJob] = {}
    runs_report: list[dict] = []
    for key, variants in groups.items():
        b, n = key
        cal_run = cal_runs[key]
        cal_rc = cal_rcs[cal_run]
        error: str | None = None
        period_ns = cfg.calibration_period_ns
        side_um = cfg.calibration_side_um
        if cal_rc != 0:
            error = f"calibration FAIL (rc={cal_rc})"
        else:
            try:
                metrics = extract(cal_run.output_dir, cal_run.design.top_module)
                ws_ns = metrics["route_ws_ns"]
                cell_area_um2 = metrics["synth_area_um2"]
                period_ns = (cfg.calibration_period_ns - ws_ns) * cfg.target_multiplier
                side_um = derive_final_side_um(cell_area_um2, cfg)
            except Exception as exc:
                error = f"calibration parse FAIL: {exc}"
        runs_report.append({
            "benchmark": b,
            "name": n,
            "variant": "__calibration__",
            "error": error,
        })
        for d in variants:
            final_runs[d] = RunJob(
                run=RunConfig(
                    design=d,
                    output_dir=design_variant_dir(d, batch_dir),
                    period_ns=period_ns,
                    side_um=side_um,
                ),
                error=error,
            )

    runnable = [job.run for job in final_runs.values() if job.error is None]
    skipped = len(final_runs) - len(runnable)
    final_rcs = run_phases(runnable, cfg, args.num_threads, "Eval")

    for d, job in final_runs.items():
        if job.error is not None:
            error = job.error
        else:
            rc = final_rcs[job.run]
            error = f"final FAIL (rc={rc})" if rc != 0 else None
        runs_report.append({
            "benchmark": d.benchmark,
            "name": d.name,
            "variant": d.variant,
            "error": error,
        })
    runs_report.sort(key=lambda e: (e["benchmark"], e["name"], e["variant"]))
    (batch_dir / "runs.json").write_text(json.dumps(runs_report, indent=2))


if __name__ == "__main__":
    main()
