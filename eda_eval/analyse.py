#!/usr/bin/env python3
"""Dump per-path and logical-group setup-timing reports for an ORFS phase dir."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from common.config import (
    EDA_EVAL,
    ORFS_FLOW,
    PNR_FLOW_TARGETS,
    YOSYS_BIN,
    DesignConfig,
    RunConfig,
)
from eda_eval.extract_metrics import find_unique, parse_period_ps

# Default config
TOP_PATHS = 50
TOP_LOGICAL = 50
POOL_SIZE = 5000

# tcl paths
EXTRACT_TCL = EDA_EVAL / "tcl" / "extract_critical_paths.tcl"

# Regexes for lexing
INDEX_RE = re.compile(r"\[\d+\]")
HIER_SEP_RE = re.compile(r"[/.]")


# Helpers


def locate_results(
    phase_dir: Path,
    design: str,
    platform: str,
    stage: str,
) -> tuple[Path, Path, Path | None]:
    """Return (odb, sdc, spef_or_none) for `stage` (`1_synth` or `6_final`)."""
    base = f"results/{platform}/{design}/*"
    odb = find_unique(phase_dir, f"{base}/{stage}.odb", f"{stage}.odb")
    sdc = find_unique(phase_dir, f"{base}/{stage}.sdc", f"{stage}.sdc")
    spef = next(iter(phase_dir.glob(f"{base}/{stage}.spef")), None)
    return odb, sdc, spef


def liberty_files(platform: str) -> list[Path]:
    """All .lib files for `platform` under the ORFS platforms dir."""
    libs = sorted((ORFS_FLOW / "platforms" / platform / "lib").glob("*.lib"))
    if not libs:
        raise FileNotFoundError(f"no .lib files for platform {platform!r}")
    return libs


def run_openroad_extract(
    odb: Path,
    sdc: Path,
    spef: Path | None,
    libs: list[Path],
    pool: int,
    tsv_out: Path,
) -> None:
    """Drive openroad to dump the worst-slack timing pool to tsv_out."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines += [f"read_db {odb}", f"read_sdc {sdc}"]
    if spef is not None:
        lines.append(f"read_spef {spef}")
    lines.append(f"source {EXTRACT_TCL}")
    env = {**os.environ, "ANALYSE_OUT_TSV": str(tsv_out), "ANALYSE_POOL": str(pool)}
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input="\n".join(lines),
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("openroad extraction failed")


def resolve_rtl_inputs(
    design: DesignConfig, phase_dir: Path
) -> tuple[list[Path], list[Path]]:
    """Return (rtl_paths, include_dirs) for the hierarchy dump. Prefers
    `design.rtl_abs_paths`, falls back to `<phase_dir>/inputs/rtl/<basename>`."""
    abs_paths = list(design.rtl_abs_paths)
    rtl_src = design.root / design.rtl_dir
    include_dirs = [rtl_src / d for d in design.include_dirs]
    if all(p.is_file() for p in abs_paths) and all(d.is_dir() for d in include_dirs):
        return abs_paths, include_dirs
    staged = phase_dir / "inputs" / "rtl"
    return [staged / Path(rel).name for rel in design.rtl_files], []


def dump_hierarchy(
    rtl_files: list[Path],
    top_module: str,
    json_out: Path,
    include_dirs: list[Path] | None = None,
) -> None:
    """Have yosys elaborate the module hierarchy and dump it as JSON."""
    inc_args = "".join(f" -I {d}" for d in (include_dirs or []))
    rtl_args = " ".join(str(f) for f in rtl_files)
    script = (
        f"read_verilog -sv{inc_args} {rtl_args}\n"
        f"hierarchy -top {top_module}\n"
        "proc\n"
        f"write_json {json_out}\n"
    )
    proc = subprocess.run(
        [YOSYS_BIN, "-q", "-p", script],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise RuntimeError("yosys hierarchy dump failed")


def _clean_instance(name: str) -> str:
    """Strip yosys escape syntax, `[N]` indices, and trailing `$_CELLTYPE_`."""
    name = name.lstrip("\\").rstrip().split("$", 1)[0]
    return INDEX_RE.sub("", name)


def load_hierarchy(json_path: Path) -> dict[str, dict[str, str]]:
    """Module -> {clean_inst_name: child_module} from yosys write_json output."""
    modules_raw = json.loads(json_path.read_text()).get("modules", {})
    valid = set(modules_raw)
    return {
        name: {
            _clean_instance(inst): cell["type"]
            for inst, cell in info.get("cells", {}).items()
            if cell.get("type") in valid
        }
        for name, info in modules_raw.items()
    }


def cell_modules(
    inst_path: str,
    top_module: str,
    hierarchy: dict[str, dict[str, str]],
) -> tuple[set[str], bool]:
    """Walk an instance path; return (modules touched, walk truncated early)."""
    mods = {top_module}
    current = top_module
    parts = HIER_SEP_RE.split(inst_path)
    for inst in parts[:-1]:
        child = hierarchy.get(current, {}).get(_clean_instance(inst))
        if child is None:
            return mods, True
        current = child
        mods.add(current)
    return mods, False


def logical_stem(full_name: str) -> str:
    """Pin name -> register-array stem (drop /<pin>, $-tag, and [N] indices)."""
    return INDEX_RE.sub("", full_name.rsplit("/", 1)[0].split("$", 1)[0])


# Report writers


def read_pool_tsv(tsv: Path) -> list[tuple[float, str, str, list[str]]]:
    """Parse the timing-pool TSV into (slack_ns, sp, ep, cells) records."""
    records = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        slack, sp, ep, cells = line.split("\t")
        records.append((float(slack), sp, ep, cells.split("|") if cells else []))
    return records


def write_critical_paths(
    records: list,
    out_path: Path,
    top_m: int,
    stage_label: str,
) -> int:
    """Write the top_m worst-slack paths as a ranked TSV."""
    rows = records[:top_m]
    with out_path.open("w") as fh:
        fh.write(f"# stage\t{stage_label}\n")
        fh.write("# rank\tslack_ns\tstartpoint\tendpoint\n")
        for rank, (slack, sp, ep, _) in enumerate(rows, 1):
            fh.write(f"{rank}\t{slack:.4f}\t{sp}\t{ep}\n")
    return len(rows)


def write_logical_paths(
    records: list,
    out_path: Path,
    top_n: int,
    top_module: str,
    hierarchy: dict[str, dict[str, str]],
    clock_period_ns: float,
    stage_label: str,
) -> int:
    """Group records by (start_stem, end_stem); write the ranked top_n."""
    groups: dict[tuple[str, str], dict] = {}
    walks_total = 0
    walks_truncated = 0
    for slack, sp, ep, cells in records:
        mods: set[str] = set()
        for cell in cells:
            cell_mods, truncated = cell_modules(cell, top_module, hierarchy)
            mods |= cell_mods
            walks_total += 1
            walks_truncated += truncated
        g = groups.setdefault(
            (logical_stem(sp), logical_stem(ep)),
            {"count": 0, "worst": slack, "best": slack, "modules": set()},
        )
        g["count"] += 1
        g["worst"] = min(g["worst"], slack)
        g["best"] = max(g["best"], slack)
        g["modules"] |= mods

    ranked = sorted(groups.items(), key=lambda kv: (kv[1]["worst"], -kv[1]["count"]))
    top = ranked[:top_n]

    with out_path.open("w") as fh:
        fh.write(f"# stage\t{stage_label}\n")
        fh.write(f"# target_period_ns\t{clock_period_ns:.4f}\n")
        fh.write(f"# pool_size\t{len(records)}\n")
        fh.write(f"# top_module\t{top_module}\n")
        fh.write(f"# truncated_walks\t{walks_truncated}/{walks_total}\n")
        fh.write(
            "# rank\tworst_slack_ns\tbest_slack_ns\tcount"
            "\tstart_stem\tend_stem\tmodules\n"
        )
        for rank, ((ss, es), g) in enumerate(top, 1):
            mods_str = ",".join(sorted(g["modules"]))
            fh.write(
                f"{rank}\t{g['worst']:.4f}\t{g['best']:.4f}\t{g['count']}"
                f"\t{ss}\t{es}\t{mods_str}\n"
            )
    return len(top)


# Entry point


def analyse(
    run: RunConfig,
    *,
    top_paths: int = TOP_PATHS,
    top_logical: int = TOP_LOGICAL,
    pool: int = POOL_SIZE,
) -> str:
    """Run STA on the phase dir and write critical/logical-paths reports.
    Determines stage (synth/route) based on RunConfig."""
    design = run.synth_target.design
    top_module = design.top_module
    platform = run.synth_target.cfg.platform
    phase_dir = run.output_dir
    rtl_paths, include_dirs = resolve_rtl_inputs(design, phase_dir)

    if set(run.flow_targets) & set(PNR_FLOW_TARGETS):
        stage, stage_label = "6_final", "post-route"
    else:
        stage, stage_label = "1_synth", "post-synth"

    odb, sdc, spef = locate_results(phase_dir, top_module, platform, stage)
    libs = liberty_files(platform)
    reports_dir = phase_dir / "reports" / platform / top_module / "base"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Stage the tsv next to the final report so the rename is same-filesystem.
    raw_tsv = reports_dir / "critical_paths_raw.tsv"
    staging = raw_tsv.with_suffix(".tsv.partial")
    run_openroad_extract(odb, sdc, spef, libs, pool, staging)
    staging.replace(raw_tsv)

    records = read_pool_tsv(raw_tsv)
    if not records:
        raise RuntimeError("OpenSTA returned zero paths")

    hier_json = reports_dir / "hierarchy.json"
    dump_hierarchy(rtl_paths, top_module, hier_json, include_dirs)
    hierarchy = load_hierarchy(hier_json)

    crit_path = reports_dir / "critical_paths.rpt"
    logical_path = reports_dir / "logical_paths.rpt"
    write_critical_paths(records, crit_path, top_paths, stage_label)
    write_logical_paths(
        records,
        logical_path,
        top_logical,
        top_module,
        hierarchy,
        clock_period_ns=parse_period_ps(sdc) / 1000,
        stage_label=stage_label,
    )
    return logical_path.read_text()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase_dir", type=Path)
    parser.add_argument("--top-paths", type=int, default=TOP_PATHS, metavar="M")
    parser.add_argument("--top-logical", type=int, default=TOP_LOGICAL, metavar="N")
    parser.add_argument("--pool", type=int, default=POOL_SIZE, metavar="P")
    args = parser.parse_args()

    phase_dir = args.phase_dir.resolve()
    run = replace(RunConfig.load(phase_dir / RunConfig.FILENAME), output_dir=phase_dir)
    analyse(
        run,
        top_paths=args.top_paths,
        top_logical=args.top_logical,
        pool=args.pool,
    )


if __name__ == "__main__":
    main()
