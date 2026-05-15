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

from config import ORFS_HOME, RUN_CONFIG_FILENAME
from extract_metrics import find_unique


VERILOG_KEYWORDS = {
    "always", "and", "assign", "begin", "buf", "bufif0", "bufif1", "case",
    "casex", "casez", "cmos", "deassign", "default", "defparam", "disable",
    "edge", "else", "end", "endcase", "endfunction", "endgenerate",
    "endmodule", "endprimitive", "endspecify", "endtable", "endtask", "event",
    "for", "force", "forever", "fork", "function", "generate", "genvar",
    "highz0", "highz1", "if", "ifnone", "initial", "inout", "input",
    "integer", "join", "large", "localparam", "macromodule", "medium",
    "module", "nand", "negedge", "nmos", "nor", "not", "notif0", "notif1",
    "or", "output", "parameter", "pmos", "posedge", "primitive", "pull0",
    "pull1", "pulldown", "pullup", "rcmos", "real", "realtime", "reg",
    "release", "repeat", "rnmos", "rpmos", "rtran", "rtranif0", "rtranif1",
    "scalared", "signed", "small", "specify", "specparam", "strong0",
    "strong1", "supply0", "supply1", "table", "task", "time", "tran",
    "tranif0", "tranif1", "tri", "tri0", "tri1", "triand", "trior", "trireg",
    "unsigned", "vectored", "wait", "wand", "weak0", "weak1", "while",
    "wire", "wor", "xnor", "xor",
}


# Embedded OpenSTA Tcl. `find_timing_paths` returns opaque PathEnd handles;
# we project each to a tab-separated `slack<TAB>start<TAB>end<TAB>cells` row.
# `-endpoint_path_count 1 -unique_paths_to_endpoint` caps the pool at one
# path per endpoint so a wide fanout doesn't drown out the rest of the
# design. The pool size and output path are wired in via env vars so the
# Python side controls them without textual templating.
_EXTRACT_TCL = r"""
if {![info exists env(ANALYSE_OUT_TSV)]} {
    error "ANALYSE_OUT_TSV env var not set"
}
set out_path $env(ANALYSE_OUT_TSV)
set pool 1000
if {[info exists env(ANALYSE_POOL)]} {
    set pool $env(ANALYSE_POOL)
}
set paths [find_timing_paths -path_delay max \
                             -group_path_count $pool \
                             -endpoint_path_count 1 \
                             -unique_paths_to_endpoint]
set fh [open $out_path w]
puts $fh "# slack_ns\tstartpoint\tendpoint\tcells"
foreach p $paths {
    set slack [sta::get_property $p slack]
    set sp [sta::get_property [sta::get_property $p startpoint] full_name]
    set ep [sta::get_property [sta::get_property $p endpoint]   full_name]
    set cells [list]
    foreach pt [sta::get_property $p points] {
        set pin [sta::get_property $pt pin]
        set fn  [sta::get_property $pin full_name]
        set slash [string last "/" $fn]
        if {$slash < 0} { continue }
        lappend cells [string range $fn 0 [expr {$slash - 1}]]
    }
    set cells [lsort -unique $cells]
    puts $fh "[format %.6f $slack]\t$sp\t$ep\t[join $cells |]"
}
close $fh
puts "ANALYSE: wrote [llength $paths] paths to $out_path"
"""


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
    lines.append(_EXTRACT_TCL)
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


# RTL parsing & hierarchical lookup --------------------------------------------

_MOD_START_RE = re.compile(r"^\s*module\s+(\w+)\b")
_END_RE = re.compile(r"^\s*endmodule\b")
_INST_RE = re.compile(
    r"^\s*([a-zA-Z_]\w*)"           # 1: module type
    r"(?:\s*#\s*\([^)]*\))?"         # optional #(params)
    r"\s+([a-zA-Z_]\w*)"            # 2: instance name
    r"\s*\("                         # opening port-connection paren
)
_INDEX_RE = re.compile(r"\[\d+\]")


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def parse_rtl(rtl_files: list[Path]) -> dict[str, dict]:
    """Return module_name -> {file, start_line, end_line, line_count,
    instances: {inst_name: child_module_name}}.

    Two passes: first locate module spans (so we know the universe of valid
    module names), then re-scan each body to find child instantiations whose
    type resolves to a known module.
    """
    modules: dict[str, dict] = {}
    for f in rtl_files:
        clean = strip_comments(f.read_text()).splitlines()
        current = None
        for i, line in enumerate(clean, 1):
            if current is None:
                m = _MOD_START_RE.match(line)
                if m:
                    current = {"name": m.group(1), "start": i, "instances": {}}
            else:
                if _END_RE.match(line):
                    modules[current["name"]] = {
                        "file": f,
                        "start_line": current["start"],
                        "end_line": i,
                        "line_count": i - current["start"] + 1,
                        "instances": current["instances"],
                    }
                    current = None

    valid_types = set(modules.keys())
    for info in modules.values():
        clean = strip_comments(info["file"].read_text()).splitlines()
        body = clean[info["start_line"] - 1 : info["end_line"]]
        for line in body:
            m = _INST_RE.match(line)
            if not m:
                continue
            mod_type, inst_name = m.group(1), m.group(2)
            if mod_type not in valid_types or inst_name in VERILOG_KEYWORDS:
                continue
            info["instances"][inst_name] = mod_type
    return modules


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

    hierarchy = parse_rtl(rtl_files)
    if top_module not in hierarchy:
        ap.error(
            f"top module {top_module!r} not found among parsed RTL modules: "
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
