"""Solvers (agents) for the RTLLM generate-and-test benchmark.

Currently exposes a single factory, `rtllm_react_solver`, which wires up
inspect_ai's `react` agent with the rtl-wizard MCP server and a standard
tool set. A commented-out `codex_cli` variant is preserved below — it has
been broken for tool-calling on gpt-oss-120b, but documents the alternative
solver shape and should be revived if/when codex_cli stabilizes.
"""
from inspect_ai.agent import react
from inspect_ai.tool import (
    MCPServerConfigStdio,
    bash,
    bash_session,
    code_execution,
    mcp_server_sandbox,
    memory,
    python,
    text_editor,
    think,
    update_plan,
    web_search,
)

SYSTEM_PROMPT = (
    "You are an expert Verilog designer working in a sandbox directory. "
    "The natural-language spec is in `design_description.txt`; that is the "
    "only input file you are given. The user message names the design — "
    "call it `<design>` below. Write your synthesizable module to "
    "`<design>.v` in the current directory, matching the module name and "
    "I/O signals from the spec.\n\n"
    "There is no testbench in your sandbox — you must write one yourself "
    "to verify correctness. Put it in a *separate* file (e.g. `tb.v`); its "
    "top-level module must be named `testbench`. Do NOT inline the "
    "testbench into `<design>.v` — at grading time, files containing a "
    "`module testbench` declaration are dropped, so an inlined testbench "
    "would take the design with it. Make your testbench thorough — "
    "exercise edge cases, randomized stimulus, and boundary conditions. "
    "After you submit, your design files (everything except the file with "
    "`module testbench`) are copied into a fresh container and a hidden "
    "golden testbench is run against them; that grade is final. A weak "
    "agent testbench that passes can still fail the golden one.\n\n"
    "The `rtl-wizard` MCP server exposes a simulation tool that compiles "
    "a list of Verilog files with iverilog and runs the resulting binary "
    "with vvp, returning the output. Pass it `[<design>.v, <your "
    "testbench>.v]` to iterate. Your testbench should print `Passed` on "
    "success.\n\n"
    "Then optimize the design. Your primary objective is to **maximize "
    "ppa_score = 1 / (delay × area × power)** — equivalently, minimize "
    "delay × area × power — measured by synthesizing your design to "
    "nangate45 with yosys and running OpenSTA. This is exactly what the "
    "benchmark grades on, so it is the only objective that ultimately "
    "matters. The `rtl-wizard` MCP server exposes an `openroad_ppa` tool "
    "(use whatever exact name appears in your tool list) that runs that "
    "same pipeline on your sources and returns delay (ns), area (µm²), "
    "power (µW), and ppa_score — call it after every meaningful change to "
    "see your true score and iterate to drive it up.\n\n"
    "The server also exposes a fast `yosys_synth` tool (cell/wire stats) "
    "and a `reconstruct_critical_path` tool (annotates the longest "
    "combinational path back to its RTL lines). These are diagnostic — "
    "depth correlates with delay and cell count correlates with area, so "
    "they are useful for finding *where* to optimize, but they are not "
    "the objective. When `yosys_synth` or path depth disagrees with "
    "`openroad_ppa`, trust `openroad_ppa`. Keep your testbench green "
    "throughout, and do not change the module interface."
)

# Stdio variant — kept for parity with the codex_cli alternative below,
# which expects an MCPServerConfigStdio. Not used by the active react solver.
RTL_WIZARD_MCP = MCPServerConfigStdio(
    name="rtl-wizard",
    command="python3",
    args=["/opt/rtl_wizard.py"],
)

rtl_wizard_mcp = mcp_server_sandbox(
    name="rtl-wizard",
    command="python3",
    args=["/opt/rtl_wizard.py"],
)


def rtllm_react_solver():
    """Build the active react agent.

    Wires the rtl-wizard MCP server alongside the standard inspect_ai tool
    set. The system prompt is design-agnostic — the per-sample design name
    is delivered in the user message (see `_build_sample` in tasks.py).
    """
    return react(
        prompt=SYSTEM_PROMPT,
        tools=[
            rtl_wizard_mcp,
            web_search(),
            bash(),
            python(),
            bash_session(),
            text_editor(),
            code_execution(),
            update_plan(),
            memory(),
            think(),
        ],
        on_continue=(
            "Please proceed to the next step using your best judgement. "
            "If you believe you are done, please call the `submit()` tool."
        ),
    )


# Codex (broken for all tool-calling with gpt-oss-120b)
# from inspect_swe import codex_cli
# def rtllm_codex_solver():
#     return codex_cli(
#         system_prompt=SYSTEM_PROMPT,
#         env={"GEMINI_CLI_TRUST_WORKSPACE": "true", "LD_LIBRARY_PATH": ""},
#         mcp_servers=[RTL_WIZARD_MCP],
#         version="0.110.0",
#     )
