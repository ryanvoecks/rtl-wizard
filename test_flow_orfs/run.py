#!/usr/bin/env python3
"""Drive the ORFS make-based flow over the corpus, or a single design folder.

Each design folder must contain exactly one `*.v` file; its stem is used as
DESIGN_NAME. The shared SDC template (`constraint.sdc.template`) and the
platform/utilization knobs in `Makefile.template` apply to every design — only
the design name, source file, DESIGN_DIR, and clock period vary per run.

Per-design clock period is found by running the flow twice. The *calibration*
phase runs at a generous period (`--calibration-period-ns`, default 10 ns) and
the post-route worst slack from that run is fed into
`target = (cal_period - cal_ws) * --target-multiplier` (default 1.1) to derive
a per-design target the *final* phase runs at. Both phases use the same SDC
shape — only the period changes — so the final WNS is interpretable against
the calibration's.

Each invocation is one *batch*: artifacts land under
`<repo>/eda_runs/<timestamp>/<rel-corpus-path>/{calibration,final}/`, where
`<rel-corpus-path>` mirrors the design's location inside `corpus/`. Each
phase dir is self-contained — its own `inputs/` snapshot (rtl + rendered
constraint.sdc + rendered Makefile) sits next to ORFS's
`logs/objects/reports/results/` trees and the make log.

The flow always runs through `do-finish` (final routed STA/area/power
report) rather than ORFS's `finish`, which additionally depends on GDS
generation via KLayout — not installed in every sandbox.

Usage:
    uv run test_flow_orfs/run.py                  # every design under corpus/
    uv run test_flow_orfs/run.py corpus/adder8    # full flow on one design
"""
from __future__ import annotations

import argparse
import json
import os
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


def snapshot_inputs(
    design_dir: Path,
    phase_dir: Path,
    sdc_template_src: Path,
    makefile_template_src: Path,
    period_ns: float,
) -> Path:
    """Materialize `<phase_dir>/inputs/` with an RTL copy, a rendered SDC at
    `period_ns`, and a rendered per-design Makefile. Returns the path to the
    Makefile so the caller can invoke it directly."""
    inputs = phase_dir / "inputs"
    rtl_dst = inputs / "rtl"
    if rtl_dst.exists():
        shutil.rmtree(rtl_dst)
    shutil.copytree(design_dir, rtl_dst)
    sdc_dst = inputs / "constraint.sdc"
    sdc_dst.write_text(sdc_template_src.read_text().format(period_ns=period_ns))
    design_name, verilog_in_src = resolve_design(design_dir)
    verilog_dst = rtl_dst / verilog_in_src.name
    makefile_dst = inputs / "Makefile"
    makefile_dst.write_text(
        makefile_template_src.read_text().format(
            design_name=design_name,
            design_dir=verilog_dst.parent,
            verilog_files=verilog_dst,
            sdc_file=sdc_dst,
            work_home=phase_dir,
        )
    )
    return makefile_dst


def run_phase(
    design_dir: Path,
    phase_dir: Path,
    sdc_template_src: Path,
    makefile_template_src: Path,
    period_ns: float,
    phase_label: str,
) -> int:
    """Invoke the rendered per-design Makefile for one phase. Output is
    captured to `<phase_dir>/flow_<design>_<phase_label>.log`."""
    design_name, _ = resolve_design(design_dir)
    phase_dir.mkdir(parents=True, exist_ok=True)
    makefile = snapshot_inputs(
        design_dir, phase_dir, sdc_template_src, makefile_template_src, period_ns,
    )
    log_path = phase_dir / f"flow_{design_name}_{phase_label}.log"
    with log_path.open("w") as log:
        return subprocess.run(
            ["make", "-C", str(makefile.parent)],
            stdout=log, stderr=subprocess.STDOUT,
        ).returncode


def find_finish_report(phase_dir: Path, design_name: str) -> Path:
    """Locate the post-route ORFS JSON report. The platform segment is
    glob-discovered rather than hardcoded so changing `PLATFORM` in
    Makefile.template doesn't require updating this script."""
    matches = list(phase_dir.glob(f"logs/*/{design_name}/*/6_report.json"))
    if not matches:
        raise FileNotFoundError(
            f"no 6_report.json under {phase_dir}/logs/*/{design_name}/*/ — "
            "calibration flow likely failed before do-finish"
        )
    if len(matches) > 1:
        joined = ", ".join(str(m) for m in matches)
        raise RuntimeError(f"multiple finish reports for {design_name}: {joined}")
    return matches[0]


def read_worst_setup_slack_ns(report_path: Path) -> float:
    """Post-route worst setup slack in ns (positive when timing is met with
    margin; negative when violated)."""
    data = json.loads(report_path.read_text())
    return float(data["finish__timing__setup__ws"])


def write_calibration_summary(
    out_dir: Path,
    calibration_period_ns: float,
    calibration_ws_ns: float,
    target_multiplier: float,
    target_period_ns: float,
) -> None:
    """Drop a tiny JSON next to the phase dirs so the derivation is easy to
    inspect after the fact (and consumable by extract_metrics.py)."""
    (out_dir / "calibration_summary.json").write_text(
        json.dumps(
            {
                "calibration_period_ns": calibration_period_ns,
                "calibration_ws_ns": calibration_ws_ns,
                "target_multiplier": target_multiplier,
                "target_period_ns": target_period_ns,
            },
            indent=2,
        )
        + "\n"
    )


def run_flow(
    design_dir: Path,
    out_dir: Path,
    sdc_template_src: Path,
    makefile_template_src: Path,
    calibration_period_ns: float,
    target_multiplier: float,
) -> tuple[int, str]:
    """Two-phase flow: calibration at `calibration_period_ns`, then a final
    run at `(cal_period - cal_ws) * target_multiplier`. Returns
    `(returncode, status_detail)` — the detail describes the phase that
    failed, or summarizes the derived target on success."""
    design_name, _ = resolve_design(design_dir)

    cal_dir = out_dir / "calibration"
    rc = run_phase(
        design_dir, cal_dir, sdc_template_src, makefile_template_src,
        calibration_period_ns, "calibration",
    )
    if rc != 0:
        return rc, f"calibration FAIL (rc={rc})"

    try:
        report_path = find_finish_report(cal_dir, design_name)
        cal_ws_ns = read_worst_setup_slack_ns(report_path)
    except Exception as exc:
        return 1, f"calibration parse FAIL: {exc}"

    target_period_ns = (calibration_period_ns - cal_ws_ns) * target_multiplier
    write_calibration_summary(
        out_dir, calibration_period_ns, cal_ws_ns, target_multiplier, target_period_ns,
    )

    final_dir = out_dir / "final"
    rc = run_phase(
        design_dir, final_dir, sdc_template_src, makefile_template_src,
        target_period_ns, "final",
    )
    detail = f"cal_ws={cal_ws_ns:.3f}ns target={target_period_ns:.3f}ns"
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
        "--target-multiplier", type=float, default=1.1,
        help="Safety factor on the calibration-derived minimum period. "
             "1.0 = run at the achievable minimum; >1 gives the final flow "
             "headroom to absorb optimization differences between phases.",
    )
    args = ap.parse_args()

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

    sdc_template_src = HERE / "constraint.sdc.template"
    makefile_template_src = HERE / "Makefile.template"

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
        f"Calibration period: {args.calibration_period_ns} ns, "
        f"target multiplier: {args.target_multiplier}"
    )

    out_dirs = {d: design_out_dir(d, batch_dir, CORPUS) for d in designs}

    num_threads = max(1, min(args.num_threads, len(designs)))
    results: list[tuple[str, int, str]] = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(
                run_flow, d, out_dirs[d], sdc_template_src, makefile_template_src,
                args.calibration_period_ns, args.target_multiplier,
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
