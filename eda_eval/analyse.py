#!/usr/bin/env python3
"""Analyse a single ORFS variant phase dir for optimisation opportunities.

Queries the post-route OpenROAD database for the worst-slack setup paths,
groups them by logical register-to-register relationship using the original
RTL hierarchy, and writes ranked reports next to the EDA tool's own output.

Reports land in `<phase_dir>/reports/<platform>/<design>/<variant>/`:
    critical_paths.rpt        -- top M worst-slack individual paths
    critical_paths_raw.tsv    -- the full sampled pool, with per-path cell
                                 chain detail (auditable + reusable)
    logical_paths.rpt         -- top N logical register-to-register groups,
                                 with worst/best slack, path count, the union
                                 of containing modules, and their total LOC

Usage:
    uv run eda_eval/analyse.py eda_runs/<batch>/<benchmark>/<name>/<variant>/
    uv run eda_eval/analyse.py <phase_dir> --top-paths 20 --top-logical 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from config import HERE, ORFS_HOME, RUN_CONFIG_FILENAME
from extract_metrics import find_unique

EXTRACT_TCL = HERE / "tcl" / "extract_critical_paths.tcl"


def load_run_config(phase_dir: Path) -> dict:
    """Read the `run_config.json` written by run.py at the top of the phase
    dir. Raises if absent."""
    return json.loads((phase_dir / RUN_CONFIG_FILENAME).read_text())


def locate_post_route(
    phase_dir: Path, design: str, platform: str,
) -> tuple[Path, Path, Path | None]:
    """Return (odb, sdc, spef_or_none) for this phase dir."""
    base = f"results/{platform}/{design}/*"
    odb = find_unique(phase_dir, f"{base}/6_final.odb", "6_final.odb")
    sdc = find_unique(phase_dir, f"{base}/6_final.sdc", "6_final.sdc")
    spef_matches = list(phase_dir.glob(f"{base}/6_final.spef"))
    spef = spef_matches[0] if spef_matches else None
    return odb, sdc, spef


def liberty_files(platform: str) -> list[Path]:
    """Resolve every .lib for `platform` under the ORFS platforms dir."""
    lib_dir = ORFS_HOME / "platforms" / platform / "lib"
    if not lib_dir.is_dir():
        raise FileNotFoundError(f"platform lib dir not found: {lib_dir}")
    libs = sorted(lib_dir.glob("*.lib"))
    if not libs:
        raise FileNotFoundError(f"no .lib files under {lib_dir}")
    return libs


def run_openroad_extract(
    odb: Path, sdc: Path, spef: Path | None, libs: list[Path],
    pool: int, tsv_out: Path,
) -> None:
    """Pipe an STA driver script into openroad to populate `tsv_out`."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines.append(f"read_db {odb}")
    lines.append(f"read_sdc {sdc}")
    if spef is not None:
        lines.append(f"read_spef {spef}")
    lines.append(f"source {EXTRACT_TCL}")
    script = "\n".join(lines)

    env = {**os.environ,
           "ANALYSE_OUT_TSV": str(tsv_out),
           "ANALYSE_POOL": str(pool)}
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input=script, env=env, text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"openroad path extraction failed (rc={proc.returncode}). "
            f"See output above."
        )
    if not tsv_out.is_file():
        raise RuntimeError(f"openroad did not write {tsv_out}")


# Hierarchy via yosys ----------------------------------------------------------

_INDEX_RE = re.compile(r"\[\d+\]")


def dump_hierarchy(
    rtl_files: list[Path], top_module: str, json_out: Path,
) -> None:
    """Have yosys read the RTL, elaborate the hierarchy, and dump it as JSON.

    `proc` is the minimum yosys needs before `write_json` accepts the design
    (it lowers always-blocks to structured logic). We deliberately do NOT
    run `flatten` or `synth` -- we want the original module structure with
    one cells entry per submodule instantiation.
    """
    rtl_args = " ".join(str(f) for f in rtl_files)
    script = (
        f"read_verilog -sv {rtl_args}\n"
        f"hierarchy -top {top_module}\n"
        "proc\n"
        f"write_json {json_out}\n"
    )
    proc = subprocess.run(
        ["yosys", "-q", "-p", script],
        text=True, capture_output=True,
    )
    if proc.returncode != 0 or not json_out.is_file():
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"yosys hierarchy dump failed (rc={proc.returncode})."
        )


def _module_loc(src_attr: str | None) -> int:
    """Yosys emits `attributes.src` like `"path/to/file.v:41.1-269.10"`,
    occasionally `"...|..."` for spans across multiple ranges. Take the
    first range and count inclusive lines. Missing/unparseable -> 0."""
    if not src_attr:
        return 0
    first = src_attr.split("|", 1)[0]
    try:
        _, span = first.rsplit(":", 1)
        start_s, end_s = span.split("-")
        start = int(start_s.split(".")[0])
        end = int(end_s.split(".")[0])
        return max(0, end - start + 1)
    except (ValueError, IndexError):
        return 0


def load_hierarchy(json_path: Path) -> dict[str, dict]:
    """Reshape yosys's write_json output into:
        module_name -> {line_count: int,
                        instances: {inst_name: child_module_name}}

    Only cells whose `type` is itself a module get recorded as instances --
    leaf primitives (`$_DFFE_`, gate cells, etc.) are skipped because the
    walk in `cell_modules` only cares about module-to-module hops.
    """
    raw = json.loads(json_path.read_text())
    modules_raw = raw.get("modules", {})
    valid = set(modules_raw.keys())
    hierarchy: dict[str, dict] = {}
    for name, info in modules_raw.items():
        instances: dict[str, str] = {}
        for inst, cell in info.get("cells", {}).items():
            t = cell.get("type")
            if t in valid:
                instances[inst] = t
        src = info.get("attributes", {}).get("src")
        hierarchy[name] = {
            "line_count": _module_loc(src),
            "instances": instances,
        }
    return hierarchy


def _clean_instance(name: str) -> str:
    """Strip `[N]` indices and trailing yosys `$_CELLTYPE_` tag from a path
    component, leaving the user-visible Verilog instance name."""
    name = name.split("$", 1)[0]
    return _INDEX_RE.sub("", name)


def cell_modules(
    inst_path: str, top_module: str, hierarchy: dict[str, dict],
) -> set[str]:
    """Walk a hierarchical instance path and return every module type the
    cell touches along the way -- top + each submodule type traversed +
    the leaf-containing module."""
    mods = {top_module}
    parts = inst_path.split(".")
    current = top_module
    for inst in parts[:-1]:
        clean = _clean_instance(inst)
        instances = hierarchy.get(current, {}).get("instances", {})
        if clean not in instances:
            break
        current = instances[clean]
        mods.add(current)
    return mods


def logical_stem(full_name: str) -> str:
    """Drop /<pin>, drop yosys $_CELLTYPE_, strip [N] -- mirrors the Tcl
    cell-name handling so start/end stems compare cleanly."""
    inst = full_name.split("/", 1)[0]
    inst = inst.split("$", 1)[0]
    return _INDEX_RE.sub("", inst)


# Report writers ---------------------------------------------------------------

def read_pool_tsv(tsv: Path) -> list[tuple[float, str, str, list[str]]]:
    """Return [(slack_ns, startpoint, endpoint, cells_list), ...]."""
    records: list[tuple[float, str, str, list[str]]] = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        slack_s, sp, ep, cells = line.split("\t")
        records.append((
            float(slack_s), sp, ep,
            cells.split("|") if cells else [],
        ))
    records.sort(key=lambda r: r[0])
    return records


def write_critical_paths(records: list, out_path: Path, top_m: int) -> int:
    with out_path.open("w") as fh:
        fh.write("# rank\tslack_ns\tstartpoint\tendpoint\n")
        written = 0
        for rank, (slack, sp, ep, _) in enumerate(records[:top_m], 1):
            fh.write(f"{rank}\t{slack:.4f}\t{sp}\t{ep}\n")
            written += 1
    return written


def write_logical_paths(
    records: list, out_path: Path, top_n: int,
    top_module: str, hierarchy: dict[str, dict], pool_size: int,
) -> int:
    groups: dict[tuple[str, str], dict] = {}
    for slack, sp, ep, cells in records:
        ss, es = logical_stem(sp), logical_stem(ep)
        mods: set[str] = set()
        for cell in cells:
            mods |= cell_modules(cell, top_module, hierarchy)
        key = (ss, es)
        g = groups.get(key)
        if g is None:
            g = {"count": 0, "worst": slack, "best": slack, "modules": set()}
            groups[key] = g
        g["count"] += 1
        g["worst"] = min(g["worst"], slack)
        g["best"] = max(g["best"], slack)
        g["modules"] |= mods

    ranked = sorted(
        groups.items(),
        key=lambda kv: (kv[1]["worst"], -kv[1]["count"]),
    )

    with out_path.open("w") as fh:
        fh.write(f"# pool_size\t{pool_size}\n")
        fh.write(f"# top_module\t{top_module}\n")
        fh.write(
            "# rank\tworst_slack_ns\tbest_slack_ns\tcount"
            "\ttotal_loc\tstart_stem\tend_stem\tmodules\n"
        )
        written = 0
        for rank, ((ss, es), g) in enumerate(ranked[:top_n], 1):
            mods = sorted(g["modules"])
            total_loc = sum(
                hierarchy.get(m, {}).get("line_count", 0) for m in mods
            )
            fh.write(
                f"{rank}\t{g['worst']:.4f}\t{g['best']:.4f}\t{g['count']}"
                f"\t{total_loc}\t{ss}\t{es}\t{','.join(mods)}\n"
            )
            written += 1
    return written


# Entry point ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "phase_dir", type=Path,
        help="Variant phase dir (contains inputs/, results/, logs/, reports/).",
    )
    ap.add_argument("--top-paths", type=int, default=50, metavar="M",
                    help="How many worst-slack individual paths to report "
                         "(default 50).")
    ap.add_argument("--top-logical", type=int, default=50, metavar="N",
                    help="How many logical register-to-register groups to "
                         "report (default 50).")
    ap.add_argument("--pool", type=int, default=1000, metavar="P",
                    help="Path pool size for find_timing_paths (default 1000). "
                         "Larger pools give better logical-group statistics.")
    args = ap.parse_args(argv)

    phase_dir = args.phase_dir.resolve()
    if not phase_dir.is_dir():
        ap.error(f"not a directory: {phase_dir}")
    if phase_dir.name == "__calibration__":
        ap.error(f"refusing to analyse calibration phase dir: {phase_dir}")

    run_cfg = load_run_config(phase_dir)
    top_module = run_cfg["design"]["top_module"]
    platform = run_cfg["cfg"]["platform"]
    rtl_files = [Path(p) for p in run_cfg["design"]["rtl_files"]]
    missing = [p for p in rtl_files if not p.is_file()]
    if missing:
        ap.error(f"missing RTL files referenced from {RUN_CONFIG_FILENAME}: {missing}")

    odb, sdc, spef = locate_post_route(phase_dir, top_module, platform)
    libs = liberty_files(platform)

    reports_dir = phase_dir / "reports" / platform / top_module / "base"
    reports_dir.mkdir(parents=True, exist_ok=True)
    raw_tsv = reports_dir / "critical_paths_raw.tsv"

    print(f"==> Phase dir:   {phase_dir}")
    print(f"==> Platform:    {platform}")
    print(f"==> Design:      {top_module}")
    print(f"==> Routed DB:   {odb}")
    print(f"==> SDC:         {sdc}")
    print(f"==> SPEF:        {spef if spef else '(absent; STA estimates parasitics)'}")
    print(f"==> Reports:     {reports_dir}")
    print(f"==> Pool / paths / logical: {args.pool} / {args.top_paths} / {args.top_logical}")

    # Stage the OpenSTA output to a sibling of the final report so the
    # atomic rename stays on one filesystem (workspace and /tmp can be
    # separate mounts in the devcontainer).
    staging = raw_tsv.with_suffix(".tsv.partial")
    run_openroad_extract(odb, sdc, spef, libs, args.pool, staging)
    staging.replace(raw_tsv)

    records = read_pool_tsv(raw_tsv)
    if not records:
        sys.stderr.write(
            "warning: OpenSTA returned zero paths -- is the design purely "
            "combinational with no constrained outputs?\n"
        )
        return 1

    hier_json = reports_dir / "hierarchy.json"
    dump_hierarchy(rtl_files, top_module, hier_json)
    hierarchy = load_hierarchy(hier_json)
    if top_module not in hierarchy:
        ap.error(
            f"top module {top_module!r} not found in yosys-dumped hierarchy: "
            f"{sorted(hierarchy)}"
        )

    crit_path = reports_dir / "critical_paths.rpt"
    n_crit = write_critical_paths(records, crit_path, args.top_paths)

    logical_path = reports_dir / "logical_paths.rpt"
    n_log = write_logical_paths(
        records, logical_path, args.top_logical,
        top_module, hierarchy, pool_size=len(records),
    )

    print(f"==> Wrote {n_crit} worst paths to {crit_path}")
    print(f"==> Wrote {n_log} logical groups to {logical_path}")
    print(f"==> Raw pool ({len(records)} paths): {raw_tsv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
