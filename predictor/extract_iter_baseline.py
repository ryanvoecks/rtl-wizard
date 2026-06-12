#!/usr/bin/env python3
"""Raw (un-filtered) top-100 synth + top-100 route extraction per iter.

Complement to extract_iterations.py, which runs with skip_async=1 (functional
D-pin paths only) so the predictor's training/scoring pool excludes async.
For a proper "before any of our pipeline" baseline, we need the un-filtered
top-100 at both stages.

Per iter:
  predictor/data/extraction/iter_baseline/<id>__1_synth_raw.rpt
  predictor/data/extraction/iter_baseline/<id>__6_final_raw.rpt

Usage:
    uv run python predictor/extract_iter_baseline.py             # 4-wide
    uv run python predictor/extract_iter_baseline.py --parallel 2
    uv run python predictor/extract_iter_baseline.py --only secworks/aes
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import EDA_RUNS, ORFS_FLOW  # noqa: E402
from eda_eval.analyse import liberty_files, locate_results  # noqa: E402
from predictor.calibrate_iters import (  # noqa: E402
    _invalid_iter_ids,
    _outdated_iter_ids,
)
from predictor.extract_iterations import _iter_id  # noqa: E402

WLP_TCL = REPO / "eda_eval" / "tcl" / "worst_logical_paths.tcl"
OUT_DIR = REPO / "predictor" / "data" / "extraction" / "iter_baseline"
EXISTING_TSV_DIR = REPO / "predictor" / "data" / "extraction" / "iterations"

PLATFORM = "nangate45"
DEFAULT_TOP_N = 100
DEFAULT_PARALLEL = 4

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _tcl_braces(s: str) -> str:
    return "{" + s + "}"


def _run_openroad(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input=stdin,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
    )


def _extract(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    set_rc: Path,
    libs: list[Path],
    top_module: str,
    stage: str,
    top_n: int,
    out_rpt: Path,
) -> None:
    spef_arg = _tcl_braces(str(spef)) if spef is not None else "{}"
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {WLP_TCL}"]
    # skip_async=0, include_full=1 -> raw top-N with start_full / end_full.
    lines.append(
        f"find_worst_logical_blocks "
        f"{_tcl_braces(str(out_rpt))} {top_n} {_tcl_braces(stage)} "
        f"{spef_arg} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)} 0 1"
    )
    proc = _run_openroad("\n".join(lines))
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError(f"openroad {stage} extraction failed for {out_rpt}")


def _process_iter(iter_dir: Path, top_n: int, force: bool) -> str:
    iter_id = _iter_id(iter_dir)
    rpt_synth = OUT_DIR / f"{iter_id}__1_synth_raw.rpt"
    rpt_route = OUT_DIR / f"{iter_id}__6_final_raw.rpt"
    if rpt_synth.is_file() and rpt_route.is_file() and not force:
        return f"have {iter_id}"

    cfg_path = iter_dir / "run_config.json"
    if not cfg_path.is_file():
        return f"skip {iter_id}: no run_config.json"
    cfg = json.loads(cfg_path.read_text())
    top_module = cfg["synth_target"]["design"]["top_module"]

    set_rc = ORFS_FLOW / "platforms" / PLATFORM / "setRC.tcl"
    libs = liberty_files(PLATFORM)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        odb_s, sdc_s, _ = locate_results(iter_dir, top_module, PLATFORM, "1_synth")
    except Exception as exc:
        return f"FAIL {iter_id} synth locate: {exc}"
    try:
        odb_r, sdc_r, spef_r = locate_results(iter_dir, top_module, PLATFORM, "6_final")
    except Exception as exc:
        return f"FAIL {iter_id} route locate: {exc}"

    t0 = time.time()
    if not rpt_synth.is_file() or force:
        try:
            _extract(
                odb_s,
                sdc_s,
                None,
                set_rc,
                libs,
                top_module,
                "1_synth",
                top_n,
                rpt_synth,
            )
        except Exception as exc:
            return f"FAIL {iter_id} synth: {exc}"
    synth_dt = time.time() - t0

    t1 = time.time()
    if not rpt_route.is_file() or force:
        try:
            _extract(
                odb_r,
                sdc_r,
                spef_r,
                set_rc,
                libs,
                top_module,
                "6_final",
                top_n,
                rpt_route,
            )
        except Exception as exc:
            return f"FAIL {iter_id} route: {exc}"
    route_dt = time.time() - t1

    return f"wrote {iter_id}  synth={synth_dt:.0f}s  route={route_dt:.0f}s"


def _find_iters() -> list[Path]:
    """The 140 iter_ids that survived load_all()'s outdated+invalid drop.

    Any iter dir without a corresponding TSV in extraction/iterations/ was
    never extracted in the first round and is not in the training set, so
    we skip it here too.
    """
    all_iter_ids = [t.stem for t in EXISTING_TSV_DIR.glob("*.tsv")]
    dropped = _outdated_iter_ids(all_iter_ids) | _invalid_iter_ids(all_iter_ids)
    keep_ids = set(all_iter_ids) - dropped

    iters = []
    for run_dir in sorted(EDA_RUNS.glob("*__iterations")):
        for p in run_dir.glob("*/*/*/*/iter_*"):
            if (p.is_dir() or p.is_symlink()) and _iter_id(p) in keep_ids:
                iters.append(p)
    return iters


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--only",
        action="append",
        default=None,
        help="substring filter on iter path (repeatable; OR-matched)",
    )
    args = ap.parse_args()

    iters = _find_iters()
    if args.only:
        iters = [p for p in iters if any(s in str(p) for s in args.only)]
    if not iters:
        _say("no iter dirs matched")
        return

    _say(f"running {len(iters)} iters, up to {args.parallel} in parallel")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {
            ex.submit(_process_iter, p, args.top_n, args.force): p for p in iters
        }
        for fut in cf.as_completed(futures):
            try:
                _say(fut.result())
            except Exception as exc:
                _say(f"FAIL {_iter_id(futures[fut])}: {exc}")
    _say(f"all iters done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
