from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rtl-helper")


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
    return (
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


if __name__ == "__main__":
    mcp.run()
