from pathlib import Path

from mcp.server.fastmcp import FastMCP

from yosys_synth import yosys_synth as _yosys_synth
from reconstruct_from_path import emit, parse, run_yosys

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


if __name__ == "__main__":
    mcp.run()
