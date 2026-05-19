#!/usr/bin/env python3
"""Analyse a single ORFS variant phase dir for optimisation opportunities.

Queries the post-route OpenROAD database for the worst-slack setup paths
**and** the worst routing-congestion tiles, groups each by logical RTL
relationship using the original module hierarchy, and writes ranked
reports next to the EDA tool's own output.

Reports land in `<phase_dir>/reports/<platform>/<design>/<variant>/`:
    critical_paths.rpt          -- top M worst-slack individual paths
    critical_paths_raw.tsv      -- the full sampled timing pool, with
                                   per-path cell chain detail
    logical_paths.rpt           -- top N logical register-to-register
                                   groups, with worst/best slack, path
                                   count, the union of containing
                                   modules, and their total LOC
    congestion_hotspots.rpt     -- top M overflowing GR tiles by overflow
                                   magnitude (empty stub if the design
                                   routed clean)
    congestion_hotspots_raw.tsv -- every overflow tile, enriched with the
                                   instances + IO pins driving the
                                   contributing nets
    logical_congestion.rpt      -- top N RTL-module groups by aggregate
                                   overflow exposure

Usage:
    uv run eda_eval/analyse.py eda-results/<batch>/<benchmark>/<name>/<variant>/
    uv run eda_eval/analyse.py <phase_dir> --top-paths 20 --top-logical 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from extract_metrics import find_unique

from common.config import ORFS_HOME, RUN_CONFIG_FILENAME, YOSYS_BIN

HERE = Path(__file__).resolve().parent
EXTRACT_TCL = HERE / "tcl" / "extract_critical_paths.tcl"
EXTRACT_CONGEST_TCL = HERE / "tcl" / "extract_congestion.tcl"

# ORFS emits up to four congestion reports across the GR pass; the later
# the stage, the closer to the final routed state. Pick the latest that
# exists (per-iteration `-N.rpt` snapshots are intentionally ignored).
_CONGESTION_RPT_PRIORITY = (
    "congestion_post_recover_power.rpt",
    "congestion_post_repair_timing.rpt",
    "congestion_post_repair_design.rpt",
    "congestion.rpt",
)


def load_run_config(phase_dir: Path) -> dict:
    """Read the `run_config.json` written by run.py at the top of the phase
    dir. Raises if absent."""
    return json.loads((phase_dir / RUN_CONFIG_FILENAME).read_text())


def locate_post_route(
    phase_dir: Path,
    design: str,
    platform: str,
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
    odb: Path,
    sdc: Path,
    spef: Path | None,
    libs: list[Path],
    pool: int,
    tsv_out: Path,
    congest_rpt: Path | None,
    congest_tsv_out: Path,
) -> None:
    """Pipe an STA + congestion driver script into openroad. One openroad
    invocation produces both `tsv_out` (timing pool) and
    `congest_tsv_out` (enriched congestion). When `congest_rpt` is
    None the congestion tcl short-circuits and `congest_tsv_out` is
    written with just its header (the clean-design case)."""
    lines = [f"read_liberty {lib}" for lib in libs]
    lines.append(f"read_db {odb}")
    lines.append(f"read_sdc {sdc}")
    if spef is not None:
        lines.append(f"read_spef {spef}")
    lines.append(f"source {EXTRACT_TCL}")
    lines.append(f"source {EXTRACT_CONGEST_TCL}")
    script = "\n".join(lines)

    env = {
        **os.environ,
        "ANALYSE_OUT_TSV": str(tsv_out),
        "ANALYSE_POOL": str(pool),
        "ANALYSE_CONGEST_TSV": str(congest_tsv_out),
    }
    if congest_rpt is not None:
        env["ANALYSE_CONGEST_RPT"] = str(congest_rpt)
    proc = subprocess.run(
        ["openroad", "-no_init", "-exit"],
        input=script,
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"openroad extraction failed (rc={proc.returncode}). " f"See output above."
        )
    if not tsv_out.is_file():
        raise RuntimeError(f"openroad did not write {tsv_out}")
    if not congest_tsv_out.is_file():
        raise RuntimeError(f"openroad did not write {congest_tsv_out}")


def locate_congestion_rpt(reports_dir: Path) -> Path | None:
    """Return the most-final GR-emitted congestion rpt that exists, or
    None. ORFS writes these only when overflowing tiles exist (see
    `flow/scripts/global_route.tcl`), so a missing file is the
    clean-design case."""
    for name in _CONGESTION_RPT_PRIORITY:
        candidate = reports_dir / name
        if candidate.is_file():
            return candidate
    return None


# Hierarchy via yosys ----------------------------------------------------------

_INDEX_RE = re.compile(r"\[\d+\]")


def dump_hierarchy(
    rtl_files: list[Path],
    top_module: str,
    json_out: Path,
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
        [YOSYS_BIN, "-q", "-p", script],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0 or not json_out.is_file():
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"yosys hierarchy dump failed (rc={proc.returncode}).")


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
    inst_path: str,
    top_module: str,
    hierarchy: dict[str, dict],
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
        records.append(
            (
                float(slack_s),
                sp,
                ep,
                cells.split("|") if cells else [],
            )
        )
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
    records: list,
    out_path: Path,
    top_n: int,
    top_module: str,
    hierarchy: dict[str, dict],
    pool_size: int,
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
            total_loc = sum(hierarchy.get(m, {}).get("line_count", 0) for m in mods)
            fh.write(
                f"{rank}\t{g['worst']:.4f}\t{g['best']:.4f}\t{g['count']}"
                f"\t{total_loc}\t{ss}\t{es}\t{','.join(mods)}\n"
            )
            written += 1
    return written


# Congestion ------------------------------------------------------------------


@dataclass(frozen=True)
class CongestionTile:
    """One overflowing GR tile, post-enrichment by extract_congestion.tcl."""

    direction: str  # "H" / "V" (or "?" if GR labelled unusually)
    overflow: int  # usage - capacity, positive
    capacity: int
    usage: int
    layer: str  # GR commonly writes "-" for per-direction aggregate
    bbox_um: tuple[float, float, float, float]  # xL, yL, xH, yH
    nets: tuple[str, ...]  # contributing nets per GR's `srcs:`
    insts: tuple[str, ...]  # instance names connected to those nets
    io_pins: tuple[str, ...]  # "IO:<bterm_name>" markers


def read_congestion_tsv(tsv: Path) -> list[CongestionTile]:
    """Parse the enriched congestion TSV. Ranked by overflow descending so
    the first row is the worst tile."""
    tiles: list[CongestionTile] = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 12:
            continue
        d, ovfl, cap, use, lyr, xL, yL, xH, yH, nets_s, insts_s, io_s = parts
        tiles.append(
            CongestionTile(
                direction=d,
                overflow=int(ovfl),
                capacity=int(cap),
                usage=int(use),
                layer=lyr,
                bbox_um=(float(xL), float(yL), float(xH), float(yH)),
                nets=tuple(nets_s.split("|")) if nets_s else (),
                insts=tuple(insts_s.split("|")) if insts_s else (),
                io_pins=tuple(io_s.split("|")) if io_s else (),
            )
        )
    tiles.sort(key=lambda t: (-t.overflow, -t.usage))
    return tiles


def write_congestion_hotspots(
    tiles: list[CongestionTile],
    out_path: Path,
    top_m: int,
) -> int:
    with out_path.open("w") as fh:
        fh.write(f"# total_overflow_tiles\t{len(tiles)}\n")
        fh.write(
            "# rank\toverflow\tcapacity\tusage\tdir\tlayer"
            "\txL\tyL\txH\tyH\tnet_count\tinst_count\ttop_net\n"
        )
        if not tiles:
            fh.write("# no overflowing tiles\n")
            return 0
        written = 0
        for rank, t in enumerate(tiles[:top_m], 1):
            xL, yL, xH, yH = t.bbox_um
            top_net = t.nets[0] if t.nets else "-"
            fh.write(
                f"{rank}\t{t.overflow}\t{t.capacity}\t{t.usage}"
                f"\t{t.direction}\t{t.layer}"
                f"\t{xL:.4f}\t{yL:.4f}\t{xH:.4f}\t{yH:.4f}"
                f"\t{len(t.nets)}\t{len(t.insts)}\t{top_net}\n"
            )
            written += 1
    return written


def write_logical_congestion(
    tiles: list[CongestionTile],
    out_path: Path,
    top_n: int,
    top_module: str,
    hierarchy: dict[str, dict],
) -> int:
    """Group overflow tiles by the frozenset of RTL modules their
    contributing instances touch (via `cell_modules`). IO-pin contributions
    are recorded separately as an `io_driven` flag so primary-port-fed
    congestion is visible without diluting the module-set key."""
    groups: dict[frozenset[str], dict] = {}
    for t in tiles:
        mods: set[str] = set()
        for inst in t.insts:
            mods |= cell_modules(inst, top_module, hierarchy)
        key = frozenset(mods)
        g = groups.get(key)
        if g is None:
            g = {
                "tile_count": 0,
                "max_overflow": t.overflow,
                "sum_overflow": 0,
                "insts": set(),
                "io_driven": False,
            }
            groups[key] = g
        g["tile_count"] += 1
        g["max_overflow"] = max(g["max_overflow"], t.overflow)
        g["sum_overflow"] += t.overflow
        g["insts"] |= set(t.insts)
        if t.io_pins:
            g["io_driven"] = True

    ranked = sorted(
        groups.items(),
        key=lambda kv: (-kv[1]["sum_overflow"], -kv[1]["tile_count"]),
    )

    with out_path.open("w") as fh:
        fh.write(f"# overflow_tiles\t{len(tiles)}\n")
        fh.write(f"# top_module\t{top_module}\n")
        fh.write(
            "# rank\tsum_overflow\tmax_overflow\ttile_count\tunique_insts"
            "\tio_driven\ttotal_loc\tmodules\n"
        )
        if not tiles:
            fh.write("# no overflowing tiles\n")
            return 0
        written = 0
        for rank, (mods_key, g) in enumerate(ranked[:top_n], 1):
            mods = sorted(mods_key)
            total_loc = sum(hierarchy.get(m, {}).get("line_count", 0) for m in mods)
            mods_str = ",".join(mods) if mods else "(no instances)"
            fh.write(
                f"{rank}\t{g['sum_overflow']}\t{g['max_overflow']}"
                f"\t{g['tile_count']}\t{len(g['insts'])}"
                f"\t{int(g['io_driven'])}\t{total_loc}\t{mods_str}\n"
            )
            written += 1
    return written


# Entry point ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "phase_dir",
        type=Path,
        help="Variant phase dir (contains inputs/, results/, logs/, reports/).",
    )
    ap.add_argument(
        "--top-paths",
        type=int,
        default=50,
        metavar="M",
        help="How many worst-slack individual paths to report " "(default 50).",
    )
    ap.add_argument(
        "--top-logical",
        type=int,
        default=50,
        metavar="N",
        help="How many logical register-to-register groups to " "report (default 50).",
    )
    ap.add_argument(
        "--pool",
        type=int,
        default=1000,
        metavar="P",
        help="Path pool size for find_timing_paths (default 1000). "
        "Larger pools give better logical-group statistics.",
    )
    ap.add_argument(
        "--top-tiles",
        type=int,
        default=50,
        metavar="M",
        help="How many worst-overflow congestion tiles to " "report (default 50).",
    )
    ap.add_argument(
        "--top-modules-congest",
        type=int,
        default=50,
        metavar="N",
        help="How many RTL-module groups to report in the "
        "logical congestion rollup (default 50).",
    )
    args = ap.parse_args(argv)

    phase_dir = args.phase_dir.resolve()
    if not phase_dir.is_dir():
        ap.error(f"not a directory: {phase_dir}")

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
    congest_raw_tsv = reports_dir / "congestion_hotspots_raw.tsv"
    congest_rpt = locate_congestion_rpt(reports_dir)

    print(f"==> Phase dir:   {phase_dir}")
    print(f"==> Platform:    {platform}")
    print(f"==> Design:      {top_module}")
    print(f"==> Routed DB:   {odb}")
    print(f"==> SDC:         {sdc}")
    print(f"==> SPEF:        {spef if spef else '(absent; STA estimates parasitics)'}")
    print(
        f"==> GR congest:  {congest_rpt if congest_rpt else '(absent; design routed clean)'}"
    )
    print(f"==> Reports:     {reports_dir}")
    print(
        f"==> Pool / paths / logical: {args.pool} / {args.top_paths} / {args.top_logical}"
    )
    print(f"==> Tiles / module groups:  {args.top_tiles} / {args.top_modules_congest}")

    # Stage both tsv outputs to siblings of the final reports so the
    # atomic rename stays on one filesystem (workspace and /tmp can be
    # separate mounts in the devcontainer).
    staging = raw_tsv.with_suffix(".tsv.partial")
    congest_staging = congest_raw_tsv.with_suffix(".tsv.partial")
    run_openroad_extract(
        odb,
        sdc,
        spef,
        libs,
        args.pool,
        staging,
        congest_rpt,
        congest_staging,
    )
    staging.replace(raw_tsv)
    congest_staging.replace(congest_raw_tsv)

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
        records,
        logical_path,
        args.top_logical,
        top_module,
        hierarchy,
        pool_size=len(records),
    )

    tiles = read_congestion_tsv(congest_raw_tsv)
    congest_path = reports_dir / "congestion_hotspots.rpt"
    n_tiles = write_congestion_hotspots(tiles, congest_path, args.top_tiles)
    logical_congest = reports_dir / "logical_congestion.rpt"
    n_lcong = write_logical_congestion(
        tiles,
        logical_congest,
        args.top_modules_congest,
        top_module,
        hierarchy,
    )

    print(f"==> Wrote {n_crit} worst paths to {crit_path}")
    print(f"==> Wrote {n_log} logical groups to {logical_path}")
    print(f"==> Raw timing pool ({len(records)} paths): {raw_tsv}")
    print(f"==> Wrote {n_tiles} congestion tiles to {congest_path}")
    print(f"==> Wrote {n_lcong} logical congestion groups to {logical_congest}")
    print(f"==> Overflow tiles total: {len(tiles)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
