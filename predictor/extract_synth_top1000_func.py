#!/usr/bin/env python3
"""Extract synth top-1000 functional logical_blocks per design, plus PnR slack.

Runs against each design's baseline cache (`_dummy_baseline` ORFS run):
  1. At stage 1_synth, call `find_worst_logical_blocks` with top_n=1000 and
     skip_async=1. This produces a rpt of 1000 functional logical groups --
     async/test endpoints (RN/SN/SI/SE/A1/A2/E) are quarantined during the
     worst-path search so the top-1000 contains only setup-arc data paths.
  2. Look up the post-route slack for each of those 1000 (start_full,
     end_full) pairs via `path_slack_lookup.tcl` at stage 6_final.
  3. Merge into a per-design TSV (`start, end, slack_synth_ns,
     slack_route_ns`) under predictor/data/extraction/synth_top1000_func/.

The hypothesis (from the predictor README): 1000 functional synth blocks
gives broader coverage of paths that actually become critical post-route,
so calibrating delta on this set should produce a more reliable beta than the
top-100 union and avoid the e203 degenerate corner.

Inputs:
  - Each target's baseline cache directory (cache_path on the baseline run)
  - eda_eval/tcl/worst_logical_paths.tcl (modified to accept skip_async)
  - eda_eval/tcl/path_slack_lookup.tcl

Outputs:
  - predictor/data/extraction/logical_blocks_top1000_func/<design>__1_synth.rpt
  - predictor/data/extraction/synth_top1000_func/<design>.tsv

Usage:
    uv run python predictor/extract_synth_top1000_func.py
    uv run python predictor/extract_synth_top1000_func.py --designs aes verilog_axi
    uv run python predictor/extract_synth_top1000_func.py --top-n 500 --force
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, ORFS_FLOW, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.analyse import liberty_files, locate_results  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

WLP_TCL = REPO / "eda_eval" / "tcl" / "worst_logical_paths.tcl"
PSL_TCL = REPO / "eda_eval" / "tcl" / "path_slack_lookup.tcl"

OUT_RPT_DIR = REPO / "predictor" / "data" / "extraction" / "logical_blocks_top1000_func"
OUT_TSV_DIR = REPO / "predictor" / "data" / "extraction" / "synth_top1000_func"

PLATFORM = "nangate45"
DEFAULT_TOP_N = 1000
DEFAULT_PARALLEL = 8

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    """Thread-safe stdout to keep parallel logs readable."""
    with _print_lock:
        print(msg, flush=True)


def _tcl_braces(s: str) -> str:
    return "{" + s + "}"


def _run_openroad(stdin: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """One-shot openroad invocation with a stdin script."""
    return subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input=stdin,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
    )


def _extract_synth(
    odb: Path,
    sdc: Path,
    set_rc: Path,
    libs: list[Path],
    top_module: str,
    top_n: int,
    out_rpt: Path,
) -> None:
    """Stage-1 synth extraction with skip_async=1, top_n functional blocks."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {WLP_TCL}"]
    # Synth has no SPEF -- pass an empty TCL list as the spef argument; the
    # setup_parasitics dispatch on stage `1_synth` doesn't read it anyway.
    # Positional TCL args: out, top_n, stage, spef, set_rc, top_module,
    # skip_async=1, include_full=1.
    lines.append(
        f"find_worst_logical_blocks "
        f"{_tcl_braces(str(out_rpt))} {top_n} {_tcl_braces('1_synth')} "
        f"{{}} {_tcl_braces(str(set_rc))} {_tcl_braces(top_module)} 1 1"
    )
    proc = _run_openroad("\n".join(lines))
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad synth extraction failed")


def _lookup_route_slack(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    set_rc: Path,
    libs: list[Path],
    input_pairs_tsv: Path,
    output_tsv: Path,
) -> None:
    """Stage-6_final slack lookup for the (start_full, end_full) pairs."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {PSL_TCL}"]
    env = {
        "PSL_INPUT_TSV": str(input_pairs_tsv),
        "PSL_OUTPUT_TSV": str(output_tsv),
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
    """Parse the rpt into (start, end, slack, start_full, end_full) tuples."""
    out: list[tuple[str, str, float, str, str]] = []
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


def _read_lookup_map(out_tsv: Path) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for line in out_tsv.read_text().splitlines():
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


def _process_design(design_name: str, top_n: int, force: bool) -> None:
    tsv_out = OUT_TSV_DIR / f"{design_name}.tsv"
    rpt_out = OUT_RPT_DIR / f"{design_name}__1_synth.rpt"
    if tsv_out.is_file() and rpt_out.is_file() and not force:
        _say(f"have {tsv_out.relative_to(REPO)}")
        return

    target = next((t for t in all_targets if t.design.name == design_name), None)
    if target is None:
        _say(f"skip {design_name}: no target")
        return
    design = target.design
    run = RunConfig(
        synth_target=target,
        output_dir=EDA_RUNS / "_dummy_baseline",
        flow_targets=ALL_FLOW_TARGETS,
        num_threads=1,
    )
    cp = cache_path(run)
    if not list(cp.glob(f"logs/{PLATFORM}/*/base/6_report.json")):
        _say(f"skip {design_name}: baseline cache missing")
        return

    set_rc = ORFS_FLOW / "platforms" / PLATFORM / "setRC.tcl"
    libs = liberty_files(PLATFORM)

    # Stage 1: synth extraction, functional only, top-N.
    try:
        odb_s, sdc_s, _ = locate_results(cp, design.top_module, PLATFORM, "1_synth")
    except Exception as exc:
        _say(f"FAIL {design_name} 1_synth locate: {exc}")
        return
    OUT_RPT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        _extract_synth(odb_s, sdc_s, set_rc, libs, design.top_module, top_n, rpt_out)
    except Exception as exc:
        _say(f"FAIL {design_name} synth extract: {exc}")
        return
    rows = _read_rpt_rows(rpt_out)
    dt = time.time() - t0
    _say(f"  {design_name} synth: {len(rows)} functional blocks ({dt:.1f}s)")
    if not rows:
        _say(f"FAIL {design_name}: empty synth rpt")
        return

    # Stage 2: route slack lookup for the same (start_full, end_full) pairs.
    try:
        odb_r, sdc_r, spef_r = locate_results(
            cp, design.top_module, PLATFORM, "6_final"
        )
    except Exception as exc:
        _say(f"FAIL {design_name} 6_final locate: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix=f"s1k_{design_name}_") as td:
        td_p = Path(td)
        pairs_in = td_p / "pairs.tsv"
        pairs_in.write_text("\n".join(f"{sf}\t{ef}" for _, _, _, sf, ef in rows) + "\n")
        route_out = td_p / "route_lookup.tsv"
        t0 = time.time()
        try:
            _lookup_route_slack(odb_r, sdc_r, spef_r, set_rc, libs, pairs_in, route_out)
        except Exception as exc:
            _say(f"FAIL {design_name} route lookup: {exc}")
            return
        route_map = _read_lookup_map(route_out)
        _say(
            f"  {design_name} route: {len(route_map)}/{len(rows)} matched "
            f"({time.time() - t0:.1f}s)"
        )

    OUT_TSV_DIR.mkdir(parents=True, exist_ok=True)
    n_written = 0
    with tsv_out.open("w") as fh:
        fh.write("# start\tend\tslack_synth_ns\tslack_route_ns\n")
        for s, e, ss, sf, ef in rows:
            sr = route_map.get((sf, ef))
            if sr is None:
                continue
            fh.write(f"{s}\t{e}\t{ss:.6f}\t{sr:.6f}\n")
            n_written += 1
    _say(f"wrote {tsv_out.relative_to(REPO)} ({n_written}/{len(rows)} pairs)")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--designs", nargs="*", default=None)
    ap.add_argument(
        "--parallel",
        type=int,
        default=DEFAULT_PARALLEL,
        help=(
            "max concurrent designs (each design owns 1 openroad process; "
            "8 fits comfortably on the 16-core host)"
        ),
    )
    args = ap.parse_args()

    if args.designs:
        names = args.designs
    else:
        names = sorted(t.design.name for t in all_targets)

    if args.parallel <= 1 or len(names) == 1:
        for name in names:
            _process_design(name, args.top_n, args.force)
        return

    _say(f"running {len(names)} designs with up to {args.parallel} in parallel")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {
            ex.submit(_process_design, n, args.top_n, args.force): n for n in names
        }
        for fut in cf.as_completed(futures):
            name = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                _say(f"FAIL {name}: {exc}")
    _say(f"all designs done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
