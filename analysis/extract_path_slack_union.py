#!/usr/bin/env python3
"""Per-design slack table over the union of baseline logical_blocks paths.

The `logical_blocks` rpts under `analysis/data/logical_blocks/` each list the
top-N worst-slack logical groups at one stage (1_synth or 6_final). Paths
near the synth-stage knife edge often migrate after CTS/routing, so a path
present in the 1_synth top-N may fall off the 6_final top-N (or vice versa)
without disappearing -- its slack just shifts. To compare post-synth vs
post-route slack on the same set, we need slack at BOTH stages for every
path in the union of the two top-N lists.

For each design:
  1. Read `<design>__1_synth.rpt` and `<design>__6_final.rpt` and union the
     logical groups by their collapsed (start, end) pair, keeping one
     representative raw (start_full, end_full) per group.
  2. Drive openroad once per stage with `path_slack_lookup.tcl`, which uses
     `block_selector` to expand each raw name into the cells/ports of all
     replicated bits, then reports the worst slack of that pair.
  3. Merge into a single per-design TSV at
     `analysis/data/path_slack_union/<design>.tsv` with columns
     `start  end  slack_synth_ns  slack_route_ns` (start/end are the
     collapsed names from the rpt, kept for downstream port classification).

Outputs are cached -- re-running skips designs whose TSV already exists
(use `--force` to rebuild). Each openroad invocation takes a similar amount
of time to the logical-paths extraction itself.

Usage:
    uv run python analysis/extract_path_slack_union.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import ALL_FLOW_TARGETS, EDA_RUNS, ORFS_FLOW, RunConfig  # noqa: E402
from common.targets import all_targets  # noqa: E402
from eda_eval.analyse import liberty_files, locate_results  # noqa: E402
from eda_eval.cache import cache_path  # noqa: E402

RPT_DIR = REPO / "analysis" / "data" / "logical_blocks"
OUT_DIR = REPO / "analysis" / "data" / "path_slack_union"
TCL = REPO / "eda_eval" / "tcl" / "path_slack_lookup.tcl"
PLATFORM = "nangate45"
STAGES = ("1_synth", "6_final")


def _read_rows(rpt: Path) -> list[tuple[str, str, str, str]]:
    """Pull (start, end, start_full, end_full) per row from a logical_blocks rpt.

    Headers and rows without the full-name columns (older 4-col extracts)
    are skipped silently.
    """
    out: list[tuple[str, str, str, str]] = []
    for line in rpt.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        out.append((parts[2], parts[3], parts[4], parts[5]))
    return out


def _read_slack_map(out_tsv: Path) -> dict[tuple[str, str], float]:
    """Parse the TCL output TSV into {(start_full, end_full): slack_ns}.

    Rows whose lookup failed ("NA") are dropped so the caller can detect
    misses via membership.
    """
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


def _drive_openroad(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    set_rc: Path,
    libs: list[Path],
    stage: str,
    input_tsv: Path,
    output_tsv: Path,
) -> None:
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}", f"source {TCL}"]
    env = {
        **os.environ,
        "PSL_INPUT_TSV": str(input_tsv),
        "PSL_OUTPUT_TSV": str(output_tsv),
        "PSL_STAGE": stage,
        "PSL_SET_RC": str(set_rc),
    }
    if spef is not None:
        env["PSL_SPEF"] = str(spef)
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input="\n".join(lines),
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError(f"openroad path-slack lookup failed at {stage}")


def _process_design(design_name: str, force: bool) -> None:
    out_tsv = OUT_DIR / f"{design_name}.tsv"
    if out_tsv.is_file() and not force:
        print(f"have {out_tsv.relative_to(REPO)}")
        return

    rpt_synth = RPT_DIR / f"{design_name}__1_synth.rpt"
    rpt_route = RPT_DIR / f"{design_name}__6_final.rpt"
    if not rpt_synth.is_file() or not rpt_route.is_file():
        print(f"skip {design_name}: missing logical_blocks rpt", file=sys.stderr)
        return

    target = next((t for t in all_targets if t.design.name == design_name), None)
    if target is None:
        print(f"skip {design_name}: no target", file=sys.stderr)
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
        print(f"skip {design_name}: baseline cache missing", file=sys.stderr)
        return

    # Union by collapsed-name pair (the level downstream port classification
    # sees), keeping one representative raw (start_full, end_full) per
    # group. The TCL will block_glob the raw name so the lookup expands to
    # every replicated bit, not just the representative pin.
    seen: set[tuple[str, str]] = set()
    union: list[tuple[str, str, str, str]] = []
    for rpt in (rpt_synth, rpt_route):
        for sp, ep, sp_full, ep_full in _read_rows(rpt):
            key = (sp, ep)
            if key in seen:
                continue
            seen.add(key)
            union.append((sp, ep, sp_full, ep_full))

    if not union:
        print(f"skip {design_name}: empty union", file=sys.stderr)
        return

    set_rc = ORFS_FLOW / "platforms" / PLATFORM / "setRC.tcl"
    libs = liberty_files(PLATFORM)

    with tempfile.TemporaryDirectory(prefix=f"psl_{design_name}_") as tdir:
        td = Path(tdir)
        in_tsv = td / "pairs.tsv"
        in_tsv.write_text("\n".join(f"{spf}\t{epf}" for _, _, spf, epf in union) + "\n")

        stage_slack: dict[str, dict[tuple[str, str], float]] = {}
        for stage in STAGES:
            try:
                odb, sdc, spef = locate_results(cp, design.top_module, PLATFORM, stage)
            except Exception as exc:
                print(f"FAIL {design_name} {stage}: {exc}", file=sys.stderr)
                return
            stage_out = td / f"out_{stage}.tsv"
            t0 = time.time()
            try:
                _drive_openroad(odb, sdc, spef, set_rc, libs, stage, in_tsv, stage_out)
            except Exception as exc:
                print(f"FAIL {design_name} {stage}: {exc}", file=sys.stderr)
                return
            stage_slack[stage] = _read_slack_map(stage_out)
            print(
                f"  {design_name} {stage}: "
                f"{len(stage_slack[stage])}/{len(union)} matched "
                f"({time.time() - t0:.1f}s)"
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n_written = 0
    with out_tsv.open("w") as fh:
        fh.write("# start\tend\tslack_synth_ns\tslack_route_ns\n")
        for sp, ep, sp_full, ep_full in union:
            key = (sp_full, ep_full)
            s = stage_slack["1_synth"].get(key)
            r = stage_slack["6_final"].get(key)
            if s is None or r is None:
                continue
            fh.write(f"{sp}\t{ep}\t{s:.6f}\t{r:.6f}\n")
            n_written += 1
    print(f"wrote {out_tsv.relative_to(REPO)} ({n_written}/{len(union)} pairs)")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--designs",
        nargs="*",
        default=None,
        help="restrict to these design names (default: every rpt under "
        "analysis/data/logical_blocks)",
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.designs:
        names = args.designs
    else:
        names = sorted({p.name.split("__")[0] for p in RPT_DIR.glob("*__1_synth.rpt")})

    for name in names:
        _process_design(name, args.force)


if __name__ == "__main__":
    main()
