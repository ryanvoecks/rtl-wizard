#!/usr/bin/env python3
"""Top-1000 synth-functional + top-100 PnR extraction for every iter in the
__iterations sweeps under eda_results/.

Each `iter_*` symlink points at a cached ORFS run dir with the same layout
as a baseline run, so the per-iter extraction is the same shape as
predictor/extract_synth_top1000_func.py:
  - synth: find_worst_logical_blocks (top-1000, skip_async=1, include_full=1)
  - pnr:   path_slack_lookup for the (start_full, end_full) pairs at 6_final

Per-iter outputs:
  predictor/data/extraction/iterations/<id>__1_synth.rpt
  predictor/data/extraction/iterations/<id>.tsv

`<id>` is `<run>__<repo>__<design>__<epoch>__<iter>` so the source path is
recoverable from the filename alone.

Usage:
    uv run python predictor/extract_iterations.py             # all iters, 8 in parallel
    uv run python predictor/extract_iterations.py --parallel 4
    uv run python predictor/extract_iterations.py --only secworks/aes  # substring match
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import EDA_RUNS, ORFS_FLOW  # noqa: E402
from eda_eval.analyse import liberty_files, locate_results  # noqa: E402

WLP_TCL = REPO / "eda_eval" / "tcl" / "worst_logical_paths.tcl"
PSL_TCL = REPO / "eda_eval" / "tcl" / "path_slack_lookup.tcl"
ITER_ROOT = EDA_RUNS
OUT_DIR = REPO / "predictor" / "data" / "extraction" / "iterations"

PLATFORM = "nangate45"
DEFAULT_TOP_N = 1000
DEFAULT_PARALLEL = 8

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _tcl_braces(s: str) -> str:
    return "{" + s + "}"


def _run_openroad(stdin: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input=stdin,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
    )


def _extract_synth(odb, sdc, set_rc, libs, top_module, top_n, out_rpt) -> None:
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {WLP_TCL}"]
    lines.append(
        f"find_worst_logical_blocks "
        f"{_tcl_braces(str(out_rpt))} {top_n} {_tcl_braces('1_synth')} "
        f"{{}} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)} 1 1"
    )
    proc = _run_openroad("\n".join(lines))
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad synth extraction failed")


def _lookup_route(odb, sdc, spef, set_rc, libs, pairs_in, route_out) -> None:
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {PSL_TCL}"]
    env = {
        "PSL_INPUT_TSV": str(pairs_in),
        "PSL_OUTPUT_TSV": str(route_out),
        "PSL_STAGE": "6_final",
        "PSL_SET_RC": str(set_rc),
    }
    if spef is not None:
        env["PSL_SPEF"] = str(spef)
    proc = _run_openroad("\n".join(lines), env=env)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad route lookup failed")


def _read_rpt_rows(rpt: Path) -> list[tuple[str, str, float, str, str]]:
    out = []
    for line in rpt.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        try:
            slack = float(parts[1])
        except ValueError:
            continue
        out.append((parts[2], parts[3], slack, parts[4], parts[5]))
    return out


def _read_lookup_map(tsv: Path) -> dict[tuple[str, str], float]:
    out = {}
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3 or parts[2] == "NA":
            continue
        try:
            out[(parts[0], parts[1])] = float(parts[2])
        except ValueError:
            continue
    return out


def _iter_id(iter_dir: Path) -> str:
    """Encode the iter's path as <run>__<repo>__<design>__<epoch>__<iter>.

    Expected path:
      <eda_results>/<run>__iterations/<batch>/<repo>/<design>/<epoch>/<iter>.
    """
    parts = iter_dir.parts
    # Find the __iterations component and take what follows.
    for i, p in enumerate(parts):
        if p.endswith("__iterations"):
            tail = parts[i + 1 :]
            run = parts[i].removesuffix("__iterations")
            # tail = (batch, repo, design, epoch, iter)
            if len(tail) >= 5:
                return f"{run}__{tail[1]}__{tail[2]}__{tail[3]}__{tail[4]}"
            return f"{run}__" + "__".join(tail)
    return "_".join(iter_dir.parts[-5:])


def _process_iter(iter_dir: Path, top_n: int, force: bool) -> str:
    iter_id = _iter_id(iter_dir)
    tsv_out = OUT_DIR / f"{iter_id}.tsv"
    rpt_out = OUT_DIR / f"{iter_id}__1_synth.rpt"
    if tsv_out.is_file() and rpt_out.is_file() and not force:
        return f"have {iter_id}"

    cfg_path = iter_dir / "run_config.json"
    if not cfg_path.is_file():
        return f"skip {iter_id}: no run_config.json"
    cfg = json.loads(cfg_path.read_text())
    top_module = cfg["synth_target"]["design"]["top_module"]

    set_rc = ORFS_FLOW / "platforms" / PLATFORM / "setRC.tcl"
    libs = liberty_files(PLATFORM)

    try:
        odb_s, sdc_s, _ = locate_results(iter_dir, top_module, PLATFORM, "1_synth")
    except Exception as exc:
        return f"FAIL {iter_id} synth locate: {exc}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        _extract_synth(odb_s, sdc_s, set_rc, libs, top_module, top_n, rpt_out)
    except Exception as exc:
        return f"FAIL {iter_id} synth: {exc}"
    rows = _read_rpt_rows(rpt_out)
    if not rows:
        return f"FAIL {iter_id}: empty synth rpt"
    synth_dt = time.time() - t0

    try:
        odb_r, sdc_r, spef_r = locate_results(iter_dir, top_module, PLATFORM, "6_final")
    except Exception as exc:
        return f"FAIL {iter_id} route locate: {exc}"

    with tempfile.TemporaryDirectory(prefix=f"iter_{iter_id}_") as td:
        td_p = Path(td)
        pairs_in = td_p / "pairs.tsv"
        pairs_in.write_text("\n".join(f"{sf}\t{ef}" for _, _, _, sf, ef in rows) + "\n")
        route_out = td_p / "route.tsv"
        t1 = time.time()
        try:
            _lookup_route(odb_r, sdc_r, spef_r, set_rc, libs, pairs_in, route_out)
        except Exception as exc:
            return f"FAIL {iter_id} route: {exc}"
        route_map = _read_lookup_map(route_out)
        route_dt = time.time() - t1

    n_written = 0
    with tsv_out.open("w") as fh:
        fh.write("# start\tend\tslack_synth_ns\tslack_route_ns\n")
        for s, e, ss, sf, ef in rows:
            sr = route_map.get((sf, ef))
            if sr is None:
                continue
            fh.write(f"{s}\t{e}\t{ss:.6f}\t{sr:.6f}\n")
            n_written += 1
    return (
        f"wrote {iter_id}  synth={len(rows)} ({synth_dt:.0f}s)  "
        f"route={len(route_map)} ({route_dt:.0f}s)  pairs={n_written}"
    )


def _find_iters() -> list[Path]:
    iters = []
    for run_dir in sorted(EDA_RUNS.glob("*__iterations")):
        # Depth 5 from the run dir gets us batch/repo/design/epoch/iter_*.
        for p in run_dir.glob("*/*/*/*/iter_*"):
            if p.is_dir() or p.is_symlink():
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
