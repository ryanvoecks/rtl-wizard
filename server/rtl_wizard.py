from pathlib import Path

from mcp.server.fastmcp import FastMCP

from yosys_synth import yosys_synth as _yosys_synth
from reconstruct_from_path import emit, parse, run_yosys
from iverilog_sim import iverilog_sim as _iverilog_sim
from openroad_ppa import measure_ppa as _measure_ppa

mcp = FastMCP("rtl-wizard")

_RTL_GUIDANCE = (
    "Good RTL coding practice: keep combinational and sequential logic in "
    "separate always blocks — use non-blocking assignments (<=) inside "
    "clocked always_ff/always @(posedge clk) blocks and blocking assignments "
    "(=) inside always_comb/always @(*). Assign every output on every path of "
    "a combinational block (else/default branches) to prevent inferred latches. "
    "Give literals explicit widths and bases (e.g. 8'h00, 1'b0) and match "
    "operand widths on both sides of an assignment. Pick one reset style per "
    "module (synchronous or asynchronous, active-high or active-low) and apply "
    "it consistently. Drive each signal from exactly one always block, avoid "
    "mixing blocking and non-blocking in the same block, and prefer "
    "parameters/localparams over magic numbers. For FSMs, use a three-block "
    "style (state register, next-state combinational, output combinational) "
    "with a defined default next state."
)


@mcp.tool()
def rtl_helper() -> str:
    """Returns concise best-practice guidance for writing synthesizable Verilog/SystemVerilog RTL.

    Call this tool BEFORE generating any Verilog or SystemVerilog RTL snippet
    (modules, always blocks, continuous assignments, FSMs, pipelines, testbenches,
    or single-line edits to existing RTL). The guidance it returns helps avoid the
    most common correctness and lint issues (inferred latches, blocking/non-blocking
    misuse, width mismatches, incomplete sensitivity lists). Invoke it once at the
    start of an RTL task; no arguments are required.
    """
    return _RTL_GUIDANCE


@mcp.tool()
def yosys_synth(verilog_path: str, top: str | None = None) -> str:
    """Synthesize a Verilog/SystemVerilog file with yosys and return cell stats.

    Use this to check that RTL is synthesizable and to read the area/cell stats
    reported at the end. The cell count and breakdown are a direct proxy for
    area; fewer sequential cells and shallower logic usually mean better
    performance and power too. Call after simulation passes and iterate on the
    RTL to bring the cell/wire counts down while keeping the testbench green.

    Args:
        verilog_path: absolute or cwd-relative path to the .v / .sv file.
        top: top-module name. Defaults to the filename stem.

    Returns the final `stat` block (wire/cell counts). On synthesis failure,
    returns the tail of the yosys log so the error is visible.
    """
    return _yosys_synth(verilog_path, top)


@mcp.tool()
def reconstruct_critical_path(verilog_path: str) -> str:
    """Reconstruct the longest combinational path of a Verilog/SystemVerilog
    file as annotated RTL.

    Runs yosys's `ltp -noff` longest-path analysis on the post-techmap netlist,
    then maps each path step back to the original RTL line that produced it.
    Output lists one line per step in the form `<basename>:<lineno>: +<delta>
    [<op>] <source line>`, with a header showing the start/end wires and total
    depth. Use this AFTER `yosys_synth` confirms the design is synthesizable to
    find which RTL constructs dominate logic depth, then refactor those lines
    to flatten the path.

    Args:
        verilog_path: absolute or cwd-relative path to the .v / .sv file.

    Returns the annotated path. On yosys failure or a missing file, returns an
    error string instead of raising.
    """
    src = Path(verilog_path).expanduser()
    if not src.is_file():
        return f"error: file not found: {src}"
    try:
        return emit(*parse(run_yosys(str(src))))
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def openroad_ppa(verilog_paths: list[str], top: str | None = None) -> str:
    """Synthesize the design to nangate45 with yosys and run OpenSTA, returning
    delay (ns), area (µm²), power (µW), and ppa_score = 1 / (delay·area·power).

    This is the same pipeline the benchmark scorer uses to grade your design,
    so the numbers it returns are the numbers you are graded on. Use it as
    your primary feedback signal once your testbench passes — call it after
    every meaningful change and iterate to drive ppa_score up (equivalently,
    delay·area·power down). Higher ppa_score is better.

    The clock is the design's `clk`/`clock`/`i_clk`/`clk_i` port at 1 ns when
    one exists, else a virtual 1 ns clock. Absolute numbers are not physically
    meaningful (no placement, no wire RC), but they are stable and comparable
    across iterations on the same design.

    Args:
        verilog_paths: list of absolute or cwd-relative paths to .v / .sv files
            to synthesize together. Pass every source needed to elaborate the
            top module (e.g. `[<design>.v]` plus any submodule files).
        top: top-module name. Defaults to the first file's stem.

    Returns the formatted PPA block on success. On synth or STA failure,
    returns the tail of the tool log so the error is visible.
    """
    return _measure_ppa(verilog_paths, top)


@mcp.tool()
def simulate(verilog_paths: list[str]) -> str:
    """Compile and run a simulation with iverilog + vvp, returning the testbench output.

    Compiles the given Verilog/SystemVerilog files together with `iverilog -g2012`
    and runs the resulting binary with `vvp`. The list should include the
    design under test, its testbench, and any additional sources needed to
    elaborate the top module. The returned string contains everything the
    testbench printed to stdout/stderr (e.g. `$display` output and the usual
    `Passed` / `Failed` markers); on a compile failure it instead returns the
    iverilog error log so the issue is visible.

    Args:
        verilog_paths: list of absolute or cwd-relative paths to .v / .sv files
            to pass to iverilog. Order does not matter.

    Returns the simulation output (or compile-error log on failure).
    """
    return _iverilog_sim(verilog_paths)


if __name__ == "__main__":
    mcp.run()
