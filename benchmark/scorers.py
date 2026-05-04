"""Scorers for the RTLLM generate-and-test benchmark.

Two scorer factories run in this order (the second is gated on the first
via `state.scores["rtllm_make_passes"]`):

  1. `rtllm_make_passes` — correctness, runs hidden golden testbench
  2. `openroad_ppa`      — delay (ns), area (µm²), power (µW), composite
                           ppa_score = 1 / (delay·area·power), and
                           relative_ppa_score = ppa_score(agent) /
                           ppa_score(golden). Two yosys→nangate45 +
                           OpenSTA passes per sample (agent + golden),
                           same setup. Despite the "post-P&R" framing,
                           this is STA on the linked netlist — no real
                           placement or routing.
"""
import asyncio
import re
from pathlib import Path

from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    mean,
    scorer,
    stderr,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox

# Name of the sibling sandbox service in compose.yaml that the scorer uses
# to run the hidden golden testbench. The agent never has access to it.
SCORER_SANDBOX = "scorer"

# Glob-based makefile used only at scoring time, after we've staged the
# agent's design files alongside the golden testbench in the sibling
# 'scorer' sandbox. Globbing keeps it agnostic to how the agent split the
# design across files.
IVERILOG_MAKEFILE = """\
.PHONY: vcs sim clean

vcs:
\tiverilog -g2012 -o simv $(wildcard *.v)

sim:
\tvvp simv | tee run.log

clean:
\trm -rf *.log simv simv.dSYM output.txt
"""

# Top-level `module testbench` declaration — used to tell the agent's own
# testbench file apart from the design files when copying out for scoring.
_TESTBENCH_MODULE_RE = re.compile(r"^\s*module\s+testbench\b", re.MULTILINE)

_SCORING_TIMEOUT_S = 300


async def _collect_dut_files(design: str) -> tuple[dict[str, str], str | None]:
    """Read the agent's Verilog sources, dropping any file that defines a
    `module testbench` (the agent's own testbench — replaced at scoring time
    by the hidden golden testbench).

    Returns (files, error). On success error is None; on failure files is
    empty and error explains what went wrong (with extra detail when the
    design file was dropped because the agent inlined the testbench).
    """
    sbox = sandbox("solver")
    ls = await sbox.exec(["sh", "-c", "ls *.v *.sv 2>/dev/null"])
    names = [n for n in ls.stdout.split() if n]
    if not names:
        return {}, "agent produced no Verilog files in sandbox cwd"

    files: dict[str, str] = {}
    dropped_as_testbench: list[str] = []
    for name in names:
        content = await sbox.read_file(name)
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if _TESTBENCH_MODULE_RE.search(content):
            dropped_as_testbench.append(name)
            continue
        files[name] = content

    if f"{design}.v" not in files:
        if f"{design}.v" in dropped_as_testbench:
            return {}, (
                f"{design}.v was dropped because it contains a `module testbench` "
                "declaration — keep the design and the testbench in separate files"
            )
        return {}, (
            f"agent did not produce {design}.v "
            f"(found design files: {sorted(files)}, "
            f"dropped as testbenches: {sorted(dropped_as_testbench)})"
        )
    return files, None


async def _run_golden_in_sibling(
    files: dict[str, str], golden_testbench_text: str
) -> tuple[int, str, str]:
    """Stage the agent's design files + golden testbench + makefile into the
    sibling 'scorer' sandbox container (defined in compose.yaml — Inspect
    spawns it per-sample and the agent never has access to it) and run the
    grading make recipe there.

    Returns (rc, stdout, stderr). Raises asyncio.TimeoutError on overrun.
    """
    sbox = sandbox(SCORER_SANDBOX)

    # Defensive cleanup — Inspect provisions a fresh container per sample,
    # but if a future change reuses the sibling we don't want stale files.
    await sbox.exec(["sh", "-c", "rm -rf /workspace/* /workspace/.[!.]*"])

    for name, content in files.items():
        await sbox.write_file(f"/workspace/{name}", content)
    await sbox.write_file("/workspace/testbench.v", golden_testbench_text)
    await sbox.write_file("/workspace/makefile", IVERILOG_MAKEFILE)

    result = await asyncio.wait_for(
        sbox.exec(
            ["sh", "-c", "make clean >/dev/null 2>&1 || true; make vcs && make sim"],
        ),
        timeout=_SCORING_TIMEOUT_S,
    )
    return result.returncode, result.stdout, result.stderr


@scorer(metrics=[accuracy(), stderr()])
def rtllm_make_passes(design: str, golden_testbench: Path) -> Scorer:
    """Score by running the hidden golden testbench against the agent's design
    in a sibling sandbox the agent never had access to."""
    golden_text = golden_testbench.read_text()

    async def score(state: TaskState, target: Target) -> Score:
        files, err = await _collect_dut_files(design)
        if err:
            return Score(value=INCORRECT, explanation=err)

        try:
            rc, stdout, stderr_out = await _run_golden_in_sibling(files, golden_text)
        except asyncio.TimeoutError:
            return Score(
                value=INCORRECT,
                explanation=f"golden testbench run timed out after {_SCORING_TIMEOUT_S}s",
            )

        if rc != 0:
            return Score(
                value=INCORRECT,
                answer=stdout,
                explanation=f"golden testbench run failed (rc={rc}):\n{stderr_out[-2000:]}",
            )
        if "Passed" not in stdout:
            return Score(
                value=INCORRECT,
                answer=stdout,
                explanation="golden testbench did not print 'Passed'",
            )
        return Score(value=CORRECT, answer=stdout)

    return score


# nangate45 ships with the OpenROAD-flow-scripts build (see sandbox/Dockerfile).
# Single-corner Liberty + matching tech/cell LEFs — OpenSTA needs all three
# before `report_power` can model cells. ORFS bundles a TAPCELL with no LEF
# master, which surfaces as a benign WARNING ORD-2056 we ignore.
_NANGATE_DIR = "/OpenROAD-flow-scripts/flow/platforms/nangate45"
NANGATE_LIB = f"{_NANGATE_DIR}/lib/NangateOpenCellLibrary_typical.lib"
NANGATE_TECH_LEF = f"{_NANGATE_DIR}/lef/NangateOpenCellLibrary.tech.lef"
NANGATE_CELL_LEF = f"{_NANGATE_DIR}/lef/NangateOpenCellLibrary.macro.lef"

# Sentinels printed between OpenSTA reports so each parser's regex is scoped
# to its own section — report-format drift in one command can't bleed into
# another.
_PPA_DELAY_TAG = "===PPA_DELAY==="
_PPA_AREA_TAG = "===PPA_AREA==="
_PPA_POWER_TAG = "===PPA_POWER==="
_PPA_END_TAG = "===PPA_END==="

# Critical-path delay in ns from a `report_checks -path_delay max` block.
# The path-summary "data arrival time" line is just whitespace + the
# cumulative arrival time + the label, e.g. `           0.36   data arrival
# time`. The slack-section line below has the negated arrival, e.g.
# `          -0.36   data arrival time`; the unsigned `[\d.]+` won't match
# that, and `.search()` finds the positive (path-summary) line first anyway.
_DELAY_ARRIVAL_RE = re.compile(
    r"^\s+([\d.]+)\s+data arrival time\s*$", re.MULTILINE
)
# Total cell area in µm² from `report_design_area`, e.g.
# `Design area 60 um^2 100% utilization.` (utilization is meaningless without
# a floorplan but report_design_area still prints it).
_AREA_RE = re.compile(r"Design area\s+([\d.eE+-]+)\s+um\^2")
# Last column of OpenSTA's `report_power` summary `Total` row — total
# power in Watts, e.g. `Total  8.01e-05  8.81e-06  1.78e-06  9.07e-05 100.0%`.
_POWER_TOTAL_RE = re.compile(r"^\s*Total\s+\S+\s+\S+\s+\S+\s+(\S+)", re.MULTILINE)


def _section(text: str, start_tag: str, end_tag: str) -> str:
    """Slice OpenROAD stdout to the chunk between two sentinel tags.

    Falls back to the empty string if either tag is missing — callers detect
    the parse failure when their regex finds nothing.
    """
    s = text.find(start_tag)
    if s < 0:
        return ""
    e = text.find(end_tag, s)
    return text[s:e] if e >= 0 else text[s:]


_PPA_KEYS = ("delay", "area", "power", "ppa_score", "relative_ppa_score")
_NAN_PPA = {k: float("nan") for k in _PPA_KEYS}


def _detect_golden_top(text: str, design: str) -> str | None:
    """Return the top module name in a `verified_<design>.v` reference.

    Naming in RTLLM golden references is inconsistent — some files use
    `module <design>` and others `module verified_<design>`. We try both
    in order and pick whichever the file actually declares.
    """
    for candidate in (design, f"verified_{design}"):
        if re.search(rf"\bmodule\s+{re.escape(candidate)}\b", text):
            return candidate
    return None


def _yosys_script(source_v: str, top: str, netlist_v: str) -> str:
    return (
        f"read_verilog -sv {source_v}; "
        f"hierarchy -check -top {top}; "
        "proc; opt; fsm; opt; memory; opt; "
        "techmap; opt; "
        f"dfflibmap -liberty {NANGATE_LIB}; "
        f"abc -liberty {NANGATE_LIB}; "
        "clean; "
        f"write_verilog -noattr -noexpr -nohex -nodec {netlist_v}"
    )


def _openroad_tcl(top: str, netlist_v: str) -> str:
    return f"""\
read_lef {NANGATE_TECH_LEF}
read_lef {NANGATE_CELL_LEF}
read_liberty {NANGATE_LIB}
read_verilog {netlist_v}
link_design {top}
set clk_ports [get_ports -quiet {{clk clock i_clk clk_i}}]
if {{[llength $clk_ports] > 0}} {{
    create_clock -name clk -period 1.0 $clk_ports
    set_input_delay -clock clk 0 [remove_from_collection [all_inputs] $clk_ports]
    set_output_delay -clock clk 0 [all_outputs]
}} else {{
    create_clock -name virtual -period 1.0
    set_input_delay -clock virtual 0 [all_inputs]
    set_output_delay -clock virtual 0 [all_outputs]
}}
puts "{_PPA_DELAY_TAG}"
report_checks -path_delay max
puts "{_PPA_AREA_TAG}"
report_design_area
puts "{_PPA_POWER_TAG}"
report_power
puts "{_PPA_END_TAG}"
exit
"""


async def _measure_ppa(
    sbox, source_v: str, top: str, label: str
) -> tuple[dict[str, float] | None, str | None]:
    """Synthesize source_v to nangate45 and run OpenSTA. Returns
    ({delay_ns, area_um2, power_uw}, None) on success or (None, error_msg)
    on any failure. `label` ("agent"/"golden") namespaces tmp files and
    error messages so the two passes don't trip over each other.
    """
    netlist_v = f"/tmp/{label}_netlist.v"
    tcl_path = f"/tmp/openroad_ppa_{label}.tcl"

    synth = await sbox.exec(["yosys", "-q", "-p", _yosys_script(source_v, top, netlist_v)])
    if not synth.success:
        return None, f"{label} yosys failed (rc={synth.returncode}):\n{synth.stderr[-2000:]}"

    await sbox.write_file(tcl_path, _openroad_tcl(top, netlist_v))
    try:
        ord_run = await asyncio.wait_for(
            sbox.exec(["openroad", "-no_init", "-exit", tcl_path]),
            timeout=120,
        )
    except asyncio.TimeoutError:
        return None, f"{label} openroad timed out after 120s"

    if not ord_run.success:
        return None, f"{label} openroad failed (rc={ord_run.returncode}):\n{ord_run.stdout[-2000:]}"

    out = ord_run.stdout
    delay_m = _DELAY_ARRIVAL_RE.search(_section(out, _PPA_DELAY_TAG, _PPA_AREA_TAG))
    area_m = _AREA_RE.search(_section(out, _PPA_AREA_TAG, _PPA_POWER_TAG))
    power_m = _POWER_TOTAL_RE.search(_section(out, _PPA_POWER_TAG, _PPA_END_TAG))
    missing = [n for n, m in (("delay", delay_m), ("area", area_m), ("power", power_m)) if m is None]
    if missing:
        return None, f"{label}: could not parse {', '.join(missing)} from openroad output:\n{out[-2000:]}"

    try:
        return {
            "delay": float(delay_m.group(1)),
            "area": float(area_m.group(1)),
            "power": float(power_m.group(1)) * 1e6,
        }, None
    except ValueError as e:
        return None, f"{label}: non-numeric value: {e}"


@scorer(metrics={k: [mean(), stderr()] for k in _PPA_KEYS})
def openroad_ppa(design: str, golden_reference: Path) -> Scorer:
    """Synthesize the agent's design AND the golden reference to nangate45,
    run OpenSTA on each, and report delay (ns), area (µm²), power (µW),
    `ppa_score = 1 / (delay·area·power)` (agent only — higher is better),
    and `relative_ppa_score = ppa_score(agent) / ppa_score(golden)` — i.e.
    `(delay·area·power)_golden / (delay·area·power)_agent`. >1 means the
    agent beat the reference on the composite metric.

    The clock is the design's `clk`/`clock`/`i_clk`/`clk_i` port at 1 ns when
    one exists, else a virtual 1 ns clock. Input/output delays are pinned to
    zero so STA finds combinational paths in flop-less designs. Absolute
    numbers aren't physically meaningful (no placement, no wire RC), but the
    relative score cancels the methodology and is comparable across designs.

    Gated on the testbench passing — returns all-NaN otherwise so per-key
    means are computed only over correct runs (Inspect filters NaN per-key
    for dict-valued scores).
    """
    golden_text = golden_reference.read_text()
    golden_top = _detect_golden_top(golden_text, design)
    if golden_top is None:
        # Configuration error — fail at task-creation time, not silently per-sample.
        raise ValueError(
            f"Could not find module `{design}` or `verified_{design}` in {golden_reference}"
        )

    async def score(state: TaskState, target: Target) -> Score:
        pass_score = (state.scores or {}).get("rtllm_make_passes")
        if pass_score is None or pass_score.value != CORRECT:
            return Score(value=_NAN_PPA, explanation="testbench did not pass — PPA omitted")

        sbox = sandbox(SCORER_SANDBOX)

        # Stage golden reference into sandbox /tmp (agent's design is already
        # in cwd as `{design}.v`).
        golden_v_path = f"/tmp/golden_{design}.v"
        await sbox.write_file(golden_v_path, golden_text)

        agent_m, err = await _measure_ppa(sbox, f"{design}.v", design, "agent")
        if err:
            return Score(value=_NAN_PPA, explanation=err)
        golden_m, err = await _measure_ppa(sbox, golden_v_path, golden_top, "golden")
        if err:
            return Score(value=_NAN_PPA, explanation=err)

        delay_ns, area_um2, power_uw = agent_m["delay"], agent_m["area"], agent_m["power"]
        agent_product = delay_ns * area_um2 * power_uw
        golden_product = golden_m["delay"] * golden_m["area"] * golden_m["power"]
        ppa_score = 1.0 / agent_product if agent_product > 0 else float("nan")
        relative = (
            golden_product / agent_product
            if agent_product > 0 and golden_product > 0
            else float("nan")
        )

        return Score(
            value={
                "delay": delay_ns,
                "area": area_um2,
                "power": power_uw,
                "ppa_score": ppa_score,
                "relative_ppa_score": relative,
            },
            answer=(
                f"delay={delay_ns:.3f}ns area={area_um2:.1f}um^2 "
                f"power={power_uw:.3f}uW ppa_score={ppa_score:.3e} "
                f"relative_ppa_score={relative:.3f}"
            ),
            explanation=(
                f"agent: delay={delay_ns:.3f} ns, area={area_um2:.1f} µm², "
                f"power={power_uw:.3f} µW. golden: delay={golden_m['delay']:.3f} ns, "
                f"area={golden_m['area']:.1f} µm², power={golden_m['power']:.3f} µW. "
                f"ppa_score={ppa_score:.3e}, relative_ppa_score={relative:.3f} "
                "(nangate45, 1 ns clock)"
            ),
        )

    return score
