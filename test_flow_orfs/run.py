#!/usr/bin/env python3
"""Drive the ORFS make-based flow over the corpus, or a single design folder.

Each design folder must contain exactly one `*.v` file; its stem is used as
DESIGN_NAME. The shared SDC (`constraint.sdc`) and the platform/utilization
knobs in `Makefile.template` apply to every design — only the design name,
source file, and DESIGN_DIR vary per run.

Each invocation is one *batch*: artifacts land under
`<repo>/eda_runs/<timestamp>/<rel-corpus-path>/`, where `<rel-corpus-path>`
mirrors the design's location inside `corpus/`. The per-design directory
contains a snapshot of the `inputs/` actually fed to ORFS (rtl + config +
constraints) so the run is self-contained, plus ORFS's own
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
    design_dir: Path, out_dir: Path, sdc_src: Path, template_src: Path,
) -> Path:
    """Copy the design RTL tree and SDC into `<out_dir>/inputs/`, then render
    the per-design Makefile from `template_src` into the same `inputs/` dir.
    Returns the path to the rendered Makefile (also serves as DESIGN_CONFIG
    when ORFS includes it)."""
    inputs = out_dir / "inputs"
    rtl_dst = inputs / "rtl"
    if rtl_dst.exists():
        shutil.rmtree(rtl_dst)
    shutil.copytree(design_dir, rtl_dst)
    sdc_dst = inputs / "constraint.sdc"
    shutil.copy2(sdc_src, sdc_dst)
    design_name, verilog_in_src = resolve_design(design_dir)
    verilog_dst = rtl_dst / verilog_in_src.name
    makefile_dst = inputs / "Makefile"
    makefile_dst.write_text(
        template_src.read_text().format(
            design_name=design_name,
            design_dir=verilog_dst.parent,
            verilog_files=verilog_dst,
            sdc_file=sdc_dst,
            work_home=out_dir,
        )
    )
    return makefile_dst


def run_flow(
    design_dir: Path,
    out_dir: Path,
    sdc_src: Path,
    template_src: Path,
) -> int:
    """Invoke the rendered per-design Makefile and return its exit code. All
    output is redirected to a per-design log file so it doesn't fight with
    the progress bar."""
    design_name, _ = resolve_design(design_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    makefile = snapshot_inputs(design_dir, out_dir, sdc_src, template_src)

    cmd = ["make", "-C", str(makefile.parent)]

    log_path = out_dir / f"flow_{design_name}.log"
    with log_path.open("w") as log:
        return subprocess.run(
            cmd, stdout=log, stderr=subprocess.STDOUT
        ).returncode


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
             "(default: half of host CPU count).",
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

    sdc_src = HERE / "constraint.sdc"
    template_src = HERE / "Makefile.template"

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

    out_dirs = {d: design_out_dir(d, batch_dir, CORPUS) for d in designs}

    num_threads = max(1, min(args.num_threads, len(designs)))
    results: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(
                run_flow, d, out_dirs[d], sdc_src, template_src,
            ): d
            for d in designs
        }
        with tqdm(total=len(designs), desc="Designs", unit="design") as pbar:
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    rc = fut.result()
                except Exception as exc:
                    rc = 1
                    pbar.write(f"  {d.name:<30} ERROR: {exc}")
                else:
                    status = "OK" if rc == 0 else f"FAIL (rc={rc})"
                    pbar.write(
                        f"  {d.name:<30} {status}  "
                        f"(log: {out_dirs[d]}/flow_{d.name}.log)"
                    )
                results.append((d.name, rc))
                pbar.update(1)

    if len(results) > 1:
        print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
        for name, rc in sorted(results):
            status = "OK" if rc == 0 else f"FAIL (rc={rc})"
            print(f"  {name:<30} {status}")

    return 0 if all(rc == 0 for _, rc in results) else 1


if __name__ == "__main__":
    sys.exit(main())
