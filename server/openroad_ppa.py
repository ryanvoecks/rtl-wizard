"""Shared yosys + OpenSTA PPA pipeline.

Used in two contexts:

  * The benchmark scorer (`benchmark/scorers.py`) drives this against a
    sibling `scorer` sandbox via `await sbox.exec(...)`, importing only the
    pure helpers (constants, script builders, parsers).
  * The rtl-wizard MCP server (`server/rtl_wizard.py`) exposes
    `measure_ppa()` as a tool the agent can call inside its own sandbox to
    see the same delay/area/power/ppa_score the scorer measures.

Keep the synth recipe + STA TCL + report parsing in one place so the agent
and the scorer never disagree on what "PPA" means.

Top-level imports are stdlib-only so this module can be loaded from the
host (where `util.eda_env` is not on `sys.path`). `eda_env` is imported
lazily inside `measure_ppa`, which is only called inside the sandbox where
the flat `from util import ...` resolves.
"""

import re
import shutil
import subprocess

# nangate45 ships with the OpenROAD-flow-scripts build (see sandbox/Dockerfile).
# Single-corner Liberty + matching tech/cell LEFs -- OpenSTA needs all three
# before `report_power` can model cells. ORFS bundles a TAPCELL with no LEF
# master, which surfaces as a benign WARNING ORD-2056 we ignore.
_NANGATE_DIR = "/OpenROAD-flow-scripts/flow/platforms/nangate45"
NANGATE_LIB = f"{_NANGATE_DIR}/lib/NangateOpenCellLibrary_typical.lib"
NANGATE_TECH_LEF = f"{_NANGATE_DIR}/lef/NangateOpenCellLibrary.tech.lef"
NANGATE_CELL_LEF = f"{_NANGATE_DIR}/lef/NangateOpenCellLibrary.macro.lef"

# Sentinels printed between OpenSTA reports so each parser's regex is scoped
# to its own section -- report-format drift in one command can't bleed into
# another.
PPA_DELAY_TAG = "===PPA_DELAY==="
PPA_AREA_TAG = "===PPA_AREA==="
PPA_POWER_TAG = "===PPA_POWER==="
PPA_END_TAG = "===PPA_END==="

# Critical-path delay in ns from a `report_checks -path_delay max` block.
# The path-summary "data arrival time" line is just whitespace + the
# cumulative arrival time + the label, e.g. `           0.36   data arrival
# time`. The slack-section line below has the negated arrival, e.g.
# `          -0.36   data arrival time`; the unsigned `[\d.]+` won't match
# that, and `.search()` finds the positive (path-summary) line first anyway.
_DELAY_ARRIVAL_RE = re.compile(r"^\s+([\d.]+)\s+data arrival time\s*$", re.MULTILINE)
# Total cell area in um^2 from `report_design_area`, e.g.
# `Design area 60 um^2 100% utilization.` (utilization is meaningless without
# a floorplan but report_design_area still prints it).
_AREA_RE = re.compile(r"Design area\s+([\d.eE+-]+)\s+um\^2")
# Last column of OpenSTA's `report_power` summary `Total` row -- total
# power in Watts, e.g. `Total  8.01e-05  8.81e-06  1.78e-06  9.07e-05 100.0%`.
_POWER_TOTAL_RE = re.compile(r"^\s*Total\s+\S+\s+\S+\s+\S+\s+(\S+)", re.MULTILINE)


def build_yosys_script(sources: list[str], top: str, netlist_v: str) -> str:
    """Yosys script that synthesizes `sources` against nangate45 and writes a
    linked netlist to `netlist_v` for OpenSTA to consume."""
    sources_arg = " ".join(sources)
    return (
        f"read_verilog -sv {sources_arg}; "
        f"hierarchy -check -top {top}; "
        "proc; opt; fsm; opt; memory; opt; "
        "techmap; opt; "
        f"dfflibmap -liberty {NANGATE_LIB}; "
        f"abc -liberty {NANGATE_LIB}; "
        "clean; "
        f"write_verilog -noattr -noexpr -nohex -nodec {netlist_v}"
    )


def build_openroad_tcl(top: str, netlist_v: str) -> str:
    """OpenSTA TCL that links `netlist_v` against nangate45 and prints the
    delay / area / power reports between section sentinels."""
    return f"""\
read_lef {NANGATE_TECH_LEF}
read_lef {NANGATE_CELL_LEF}
read_liberty {NANGATE_LIB}
read_verilog {netlist_v}
link_design {top}
set clk_ports [get_ports -quiet {{clk clock i_clk clk_i}}]
if {{[llength $clk_ports] > 0}} {{
    create_clock -name clk -period 1.0 $clk_ports
    set non_clk_inputs {{}}
    foreach p [all_inputs] {{
        if {{[lsearch -exact $clk_ports $p] < 0}} {{
            lappend non_clk_inputs $p
        }}
    }}
    if {{[llength $non_clk_inputs] > 0}} {{
        set_input_delay -clock clk 0 $non_clk_inputs
    }}
    set_output_delay -clock clk 0 [all_outputs]
}} else {{
    create_clock -name virtual -period 1.0
    set_input_delay -clock virtual 0 [all_inputs]
    set_output_delay -clock virtual 0 [all_outputs]
}}
puts "{PPA_DELAY_TAG}"
report_checks -path_delay max
puts "{PPA_AREA_TAG}"
report_design_area
puts "{PPA_POWER_TAG}"
report_power
puts "{PPA_END_TAG}"
exit
"""


def _section(text: str, start_tag: str, end_tag: str) -> str:
    """Slice OpenROAD stdout to the chunk between two sentinel tags.

    Falls back to the empty string if either tag is missing -- callers detect
    the parse failure when their regex finds nothing.
    """
    s = text.find(start_tag)
    if s < 0:
        return ""
    e = text.find(end_tag, s)
    return text[s:e] if e >= 0 else text[s:]


def parse_ppa_report(stdout: str) -> tuple[dict[str, float] | None, str | None]:
    """Parse `delay` (ns), `area` (um^2), `power` (uW) out of OpenSTA stdout.

    Returns ({delay, area, power}, None) on success or
    (None, error_msg) when a section is missing or non-numeric.
    """
    delay_m = _DELAY_ARRIVAL_RE.search(_section(stdout, PPA_DELAY_TAG, PPA_AREA_TAG))
    area_m = _AREA_RE.search(_section(stdout, PPA_AREA_TAG, PPA_POWER_TAG))
    power_m = _POWER_TOTAL_RE.search(_section(stdout, PPA_POWER_TAG, PPA_END_TAG))
    missing = [
        n
        for n, m in (("delay", delay_m), ("area", area_m), ("power", power_m))
        if m is None
    ]
    if missing:
        return None, (
            f"could not parse {', '.join(missing)} from openroad output:\n"
            f"{stdout[-2000:]}"
        )

    try:
        return {
            "delay": float(delay_m.group(1)),
            "area": float(area_m.group(1)),
            "power": float(power_m.group(1)) * 1e6,
        }, None
    except ValueError as e:
        return None, f"non-numeric value in openroad report: {e}"


YOSYS_BIN = shutil.which("yosys") or "yosys"
OPENROAD_BIN = shutil.which("openroad") or "openroad"
OUTPUT_LIMIT = 20_000
SYNTH_TIMEOUT = 120
STA_TIMEOUT = 120


def measure_ppa(verilog_paths: list[str], top: str | None = None) -> str:
    """Synthesize `verilog_paths` to nangate45 with yosys and run OpenSTA,
    returning a formatted block with delay (ns), area (um^2), power (uW), and
    `ppa_score = 1 / (delay*area*power)`.

    Mirrors the benchmark scorer's PPA pipeline exactly so the agent sees the
    same numbers it will be graded on. On synth or STA failure, returns the
    tail of the tool log so the error is visible.

    Args:
        verilog_paths: list of paths to .v / .sv files to synthesize together.
            The first file's stem is used as the top module name unless `top`
            is given.
        top: top-module name. Defaults to the first file's stem.
    """
    from pathlib import Path

    from util import eda_env  # lazy: only resolves inside the sandbox

    if not verilog_paths:
        return "error: no verilog files provided"

    resolved: list[str] = []
    for p in verilog_paths:
        src = Path(p).expanduser()
        if not src.is_file():
            return f"error: file not found: {src}"
        resolved.append(str(src.resolve()))

    top_name = top or Path(resolved[0]).stem
    netlist_v = f"/tmp/openroad_ppa_{top_name}_netlist.v"
    tcl_path = f"/tmp/openroad_ppa_{top_name}.tcl"

    synth = subprocess.run(
        [YOSYS_BIN, "-q", "-p", build_yosys_script(resolved, top_name, netlist_v)],
        env=eda_env(),
        capture_output=True,
        text=True,
        timeout=SYNTH_TIMEOUT,
    )
    if synth.returncode != 0:
        log = (synth.stdout + synth.stderr).strip()
        if len(log) > OUTPUT_LIMIT:
            log = log[-OUTPUT_LIMIT:]
        return f"[openroad_ppa yosys rc={synth.returncode} top={top_name}]\n{log}"

    Path(tcl_path).write_text(build_openroad_tcl(top_name, netlist_v))

    try:
        sta = subprocess.run(
            [OPENROAD_BIN, "-no_init", "-exit", tcl_path],
            env=eda_env(),
            capture_output=True,
            text=True,
            timeout=STA_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"[openroad_ppa openroad timeout] top={top_name} (>{STA_TIMEOUT}s)"

    if sta.returncode != 0:
        log = (sta.stdout + sta.stderr).strip()
        if len(log) > OUTPUT_LIMIT:
            log = log[-OUTPUT_LIMIT:]
        return f"[openroad_ppa openroad rc={sta.returncode} top={top_name}]\n{log}"

    metrics, err = parse_ppa_report(sta.stdout)
    if err:
        return f"[openroad_ppa parse-error top={top_name}]\n{err}"

    delay_ns = metrics["delay"]
    area_um2 = metrics["area"]
    power_uw = metrics["power"]
    product = delay_ns * area_um2 * power_uw
    ppa_score = 1.0 / product if product > 0 else float("nan")

    return (
        f"[openroad_ppa top={top_name}] (nangate45, 1 ns clock)\n"
        f"delay     = {delay_ns:.4f} ns\n"
        f"area      = {area_um2:.2f} um^2\n"
        f"power     = {power_uw:.4f} uW\n"
        f"ppa_score = 1 / (delay * area * power) = {ppa_score:.4e}  "
        "(higher is better)"
    )
