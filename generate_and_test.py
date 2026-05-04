"""Generate a Verilog design with Gemini CLI and score it against the RTLLM testbench.

Run with:
    inspect eval generate_and_test.py -T design=adder_8bit
"""
import asyncio
import fnmatch
import re
import sys
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
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
from inspect_ai.tool import MCPServerConfigStdio, mcp_server_sandbox, web_search, bash, python, bash_session, text_editor, code_execution, skill, update_plan, memory, think, Tool, tool
from inspect_ai.tool._tools._execute import code_viewer
from inspect_ai.util import sandbox
from inspect_ai.agent import react
from inspect_swe import codex_cli, gemini_cli, mini_swe_agent

REPO_ROOT = Path(__file__).parent
RTLLM_ROOT = REPO_ROOT / "external" / "RTLLM"
SANDBOX_COMPOSE = REPO_ROOT / "sandbox" / "compose.yaml"
# Name of the sibling sandbox service in compose.yaml that the scorer uses
# to run the hidden golden testbench. The agent never has access to it.
SCORER_SANDBOX = "scorer"

# Files in the upstream RTLLM design folder that we never deliver to the
# agent: ground-truth solutions, the upstream VCS makefile, and crucially
# the golden testbench — the agent must write its own and only sees the
# natural-language spec. The golden testbench is run separately at scoring
# time in a fresh container.
SKIP_COPY_PATTERNS = (
    "verified_*.v",
    "makefile",
    "Makefile",
    "testbench.v",
)

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

SYSTEM_PROMPT_TEMPLATE = (
    "You are an expert Verilog designer working in a sandbox directory. "
    "The natural-language spec is in `design_description.txt`; that is the "
    "only input file you are given. Write your synthesizable module to "
    "`{design}.v` in the current directory, matching the module name and "
    "I/O signals from the spec.\n\n"
    "There is no testbench in your sandbox — you must write one yourself "
    "to verify correctness. Put it in a *separate* file (e.g. `tb.v`); its "
    "top-level module must be named `testbench`. Do NOT inline the "
    "testbench into `{design}.v` — at grading time, files containing a "
    "`module testbench` declaration are dropped, so an inlined testbench "
    "would take the design with it. Make your testbench thorough — "
    "exercise edge cases, randomized stimulus, and boundary conditions. "
    "After you submit, your design files (everything except the file with "
    "`module testbench`) are copied into a fresh container and a hidden "
    "golden testbench is run against them; that grade is final. A weak "
    "agent testbench that passes can still fail the golden one.\n\n"
    "The `rtl-wizard` MCP server exposes a simulation tool that compiles "
    "a list of Verilog files with iverilog and runs the resulting binary "
    "with vvp, returning the output. Pass it `[{design}.v, <your "
    "testbench>.v]` to iterate. Your testbench should print `Passed` on "
    "success.\n\n"
    "Then optimize the design — your primary objective is to minimize "
    "**combinational depth** (the longest topological path through the "
    "post-techmap netlist), since that sets the achievable clock period. "
    "Secondary PPA goals: prefer fewer sequential cells, narrower "
    "datapaths, and shared logic. The `rtl-wizard` MCP server also "
    "exposes a synthesis tool that runs yosys on `{design}.v` and returns "
    "the cell/wire stats, plus a tool that reconstructs the longest "
    "combinational path as annotated RTL — call them (use whatever exact "
    "names appear in your tool list), use the reconstructed critical path "
    "to identify the depth bottleneck, and iterate to bring combinational "
    "depth down (while also watching cell count as the area metric) and "
    "keeping your testbench green. Do not change the module interface."
)

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

def find_design(name: str) -> Path:
    for path in RTLLM_ROOT.rglob(name):
        if path.is_dir() and (path / "design_description.txt").exists():
            return path
    sys.exit(f"Design not found: {name}")


def design_files(folder: Path, design: str) -> dict[str, str]:
    """Map sandbox-relative path → host path for files the agent should see.

    The agent only receives `design_description.txt`. The golden testbench,
    ground-truth solutions, and the upstream VCS makefile are all withheld;
    grading happens in a sibling sandbox at score time (see `rtllm_make_passes`).
    """
    skip_exact = {f"{design}.v"}
    out: dict[str, str] = {}
    for src in folder.iterdir():
        if not src.is_file() or src.name in skip_exact:
            continue
        if any(fnmatch.fnmatch(src.name, pat) for pat in SKIP_COPY_PATTERNS):
            continue
        out[src.name] = str(src.resolve())
    if "design_description.txt" not in out:
        sys.exit("Design folder missing required file: design_description.txt")
    return out


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


_CELL_COUNT_RE = re.compile(r"Number of cells:\s+(\d+)")
_LTP_LENGTH_RE = re.compile(r"longest topological path.*?\(length=(\d+)\)", re.IGNORECASE)


@scorer(metrics=[mean(), stderr()])
def yosys_cell_count(design: str) -> Scorer:
    """Synthesize the agent's design with yosys and report the total cell count.

    Returns NaN for samples whose testbench did not pass — Inspect filters NaN
    scores out of metric computation, so the mean is taken only over correct
    runs and isn't polluted by broken designs.
    """
    script = (
        f"read_verilog -sv {design}.v; "
        f"hierarchy -check -top {design}; "
        "proc; opt; fsm; opt; memory; opt; "
        "techmap; opt; "
        "stat"
    )
    nan = float("nan")

    async def score(state: TaskState, target: Target) -> Score:
        # Skip synth stats for failed runs — depends on rtllm_make_passes
        # running first (Inspect runs scorers in the order passed to Task).
        pass_score = (state.scores or {}).get("rtllm_make_passes")
        if pass_score is None or pass_score.value != CORRECT:
            return Score(value=nan, explanation="testbench did not pass — cell count omitted")

        sbox = sandbox("scorer")
        result = await sbox.exec(["yosys", "-p", script])
        if not result.success:
            return Score(
                value=nan,
                explanation=f"yosys failed (rc={result.returncode}):\n{result.stderr[-2000:]}",
            )
        matches = _CELL_COUNT_RE.findall(result.stdout)
        if not matches:
            return Score(value=nan, explanation="could not parse cell count from yosys output")
        cells = int(matches[-1])
        return Score(value=cells, answer=str(cells), explanation=f"yosys reports {cells} cells")

    return score


@scorer(metrics=[mean(), stderr()])
def yosys_gate_depth(design: str) -> Scorer:
    """Synthesize the agent's design with yosys and report the longest
    combinational path length (gate depth) via `ltp -noff`.

    Gated on the testbench passing — returns NaN otherwise so the mean is
    computed only over correct runs.
    """
    script = (
        f"read_verilog -sv {design}.v; "
        f"hierarchy -check -top {design}; "
        "proc; flatten; opt; fsm; opt; memory; opt; "
        "techmap; opt; "
        "ltp -noff"
    )
    nan = float("nan")

    async def score(state: TaskState, target: Target) -> Score:
        pass_score = (state.scores or {}).get("rtllm_make_passes")
        if pass_score is None or pass_score.value != CORRECT:
            return Score(value=nan, explanation="testbench did not pass — gate depth omitted")

        sbox = sandbox("scorer")
        result = await sbox.exec(["yosys", "-p", script])
        if not result.success:
            return Score(
                value=nan,
                explanation=f"yosys failed (rc={result.returncode}):\n{result.stderr[-2000:]}",
            )
        matches = _LTP_LENGTH_RE.findall(result.stdout)
        if not matches:
            return Score(value=nan, explanation="could not parse gate depth from yosys ltp output")
        depth = int(matches[-1])
        return Score(value=depth, answer=str(depth), explanation=f"yosys reports gate depth of {depth}")

    return score


@task
def rtllm_generate_and_test(design: str, message_limit: int = 40) -> Task:
    folder = find_design(design)
    description = (folder / "design_description.txt").read_text()
    golden_testbench = folder / "testbench.v"
    if not golden_testbench.is_file():
        sys.exit(f"Golden testbench not found: {golden_testbench}")

    sample = Sample(
        id=design,
        input=description,
        target="testbench prints 'Passed'",
        files=design_files(folder, design),
    )

    # Codex (broken for all tool-calling with gpt-oss-120b)
    # return Task(
    #     dataset=[sample],
    #     solver=codex_cli(
    #         system_prompt=SYSTEM_PROMPT_TEMPLATE.format(design=design),
    #         env={"GEMINI_CLI_TRUST_WORKSPACE": "true", "LD_LIBRARY_PATH": ""},
    #         mcp_servers=[RTL_WIZARD_MCP],
    #         version="0.110.0",
    #     ),
    #     scorer=rtllm_make_passes(),
    #     sandbox=("docker", str(SANDBOX_COMPOSE)),
    # )

    # ReACT
    return Task(
        dataset=[sample],
        solver=react(
            prompt=SYSTEM_PROMPT_TEMPLATE.format(design=design),
            tools=[rtl_wizard_mcp, web_search(), bash(), python(), bash_session(), text_editor(), code_execution(), update_plan(), memory(), think()],
            on_continue="Please proceed to the next step using your best judgement. If you believe you are done, please call the `submit()` tool."
        ),
        scorer=[rtllm_make_passes(design, golden_testbench), yosys_cell_count(design), yosys_gate_depth(design)],
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        message_limit=message_limit,
    )
