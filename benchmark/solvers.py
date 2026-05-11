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
    think,
    update_plan,
    web_search,
)

from benchmark.text_editor import text_editor

SYSTEM_PROMPT = (
    "You are an expert Verilog designer working in a sandbox directory. "
    "You are only given a natural-language specification, in `design_description.txt`"
    "There is no testbench in your sandbox — you must write one yourself "
    "to verify correctness. Put it in a *separate* file; its "
    "top-level module must be named `testbench`. Do NOT inline the "
    "testbench into `<design>.v`. Make your testbench thorough — "
    "exercise edge cases, randomized stimulus, and boundary conditions. "
    "Do not use `ref` as a variable name. It is invalid syntax. "
    "After you submit, your design files (everything except the file with "
    "`module testbench`) are copied into a fresh container and a hidden "
    "golden testbench is run against them; that grade is final.\n\n"
    "The `rtl-wizard` MCP server exposes a simulation tool that compiles and runs "
    "a list of Verilog files. Pass it `[<design>.v, <your "
    "testbench>.v]` to iterate. Your testbench should print `Passed` on "
    "success.\n\n"
    "You need to optimize your design. Your primary objective is to **maximize "
    "ppa_score = 1 / (delay × area × power)** — equivalently, minimize "
    "delay × area × power. This is exactly what the "
    "benchmark grades on, so it is the only objective that ultimately "
    "matters. The `rtl-wizard` MCP server exposes an `openroad_ppa` tool "
    "that runs that "
    "same pipeline on your sources — call it after every meaningful change to "
    "see your true score and iterate to drive it up.\n\n"
    "The server also exposes a `yosys_synth` tool (cell/wire stats) "
    "and a `reconstruct_critical_path` tool (annotates the longest "
    "combinational path back to its RTL lines). These are diagnostic — "
    "depth correlates with delay and cell count correlates with area, so "
    "they are useful for finding *where* to optimize, but they are not "
    "the objective. When `yosys_synth` or path depth disagrees with "
    "`openroad_ppa`, trust `openroad_ppa`. Keep your testbench green "
    "throughout, and do not change the module interface.\n\n"
    "Your primary objective is to create a design with as high a PPA score "
    "as possible. Any Verilog refactoring which does not break functionality is allowed.\n\n"
    "Remember that you are an agent in a sandbox - you must use the bash and text editor tools "
    "to write your solutions to files before evaluating."
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
