#!/usr/bin/env python3
"""Drive the ORFS make-based flow over the corpus, or a single design folder.

Each design folder must contain exactly one `*.v` file; its stem is used as
DESIGN_NAME. The shared SDC (`constraint.sdc`) and the platform/utilization
knobs in `config.mk` apply to every design — only the design name, source
file, and DESIGN_DIR vary per run.

Artifacts land under `<repo>/eda_runs/`; ORFS already namespaces by design
name internally, so multiple corpus designs can coexist there.

Default targets stop at `do-finish` (final routed STA/area/power report)
rather than ORFS's `finish`, which additionally depends on GDS generation
via KLayout — not installed in every sandbox. Pass `--targets finish` to
get GDS.

Usage:
    uv run test_flow_orfs/run.py                                # every design under corpus/
    uv run test_flow_orfs/run.py corpus/adder8                  # full flow on one design
    uv run test_flow_orfs/run.py corpus/adder8 --targets synth  # stop after synthesis
    uv run test_flow_orfs/run.py corpus/adder8 --targets clean  # ORFS clean
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EDA_RUNS = REPO_ROOT / "eda_runs"
CORPUS = REPO_ROOT / "corpus"

DEFAULT_TARGETS = (
    "synth",
    # Post-synth timing report (1_Post_synthesis.rpt). Not strictly required by
    # the PnR flow, but extract_metrics.py needs it for synth WNS/TNS.
    "synth-report",
    "floorplan",
    "place",
    "cts",
    "route",
    "do-finish",
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


def run_flow(
    design_dir: Path,
    targets: tuple[str, ...],
    flow_home: Path,
    out_dir: Path,
    sdc_path: Path,
    config_mk: Path,
) -> int:
    """Invoke ORFS make for one design and return its exit code. All output is
    redirected to a per-design log file so it doesn't fight with the progress
    bar."""
    design_name, verilog = resolve_design(design_dir)

    # Per-design values override anything in config.mk; only the platform and
    # utilization knobs come from the config file.
    cmd = [
        "make",
        "-C", str(flow_home),
        f"DESIGN_CONFIG={config_mk}",
        f"DESIGN_NAME={design_name}",
        f"DESIGN_DIR={design_dir}",
        f"VERILOG_FILES={verilog}",
        f"SDC_FILE={sdc_path}",
        f"WORK_HOME={out_dir}",
        *targets,
    ]

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
        "--targets", nargs="+", default=None, metavar="TARGET",
        help="ORFS make targets (default: full flow through do-finish).",
    )
    ap.add_argument(
        "--num-threads", type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Number of designs to run in parallel "
             "(default: half of host CPU count).",
    )
    args = ap.parse_args()

    flow_home = Path(os.environ.get("FLOW_HOME", "/OpenROAD-flow-scripts/flow"))
    if not (flow_home / "Makefile").is_file():
        sys.stderr.write(
            f"error: ORFS flow Makefile not found at {flow_home / 'Makefile'}\n"
            "       set FLOW_HOME to your OpenROAD-flow-scripts/flow checkout.\n"
        )
        return 1

    EDA_RUNS.mkdir(parents=True, exist_ok=True)
    targets = tuple(args.targets) if args.targets else DEFAULT_TARGETS
    sdc_path = HERE / "constraint.sdc"
    config_mk = HERE / "config.mk"

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

    num_threads = max(1, min(args.num_threads, len(designs)))
    results: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(
                run_flow, d, targets, flow_home, EDA_RUNS, sdc_path, config_mk,
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
                        f"(log: {EDA_RUNS}/flow_{d.name}.log)"
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
