"""Generate a Verilog design with Gemini CLI and score it against the RTLLM testbench.

Run with:
    inspect eval generate_and_test.py -T design=adder_8bit
"""
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
MAKEFILE_DIR = REPO_ROOT / "tmp" / "makefiles"

# RTLLM ships VCS-based makefiles; we run with iverilog instead, so we ignore
# the upstream makefile and inject our own (see IVERILOG_MAKEFILE).
SKIP_COPY_PATTERNS = (
    "verified_*.v",
    "makefile",
    "Makefile",
)

IVERILOG_MAKEFILE = """\
.PHONY: vcs sim clean

TEST_DESIGN = {design}

vcs:
\tiverilog -g2012 -o simv $(TEST_DESIGN).v testbench.v

sim:
\tvvp simv | tee run.log

clean:
\trm -rf *.log simv simv.dSYM output.txt
"""


def iverilog_makefile_path(design: str) -> str:
    MAKEFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = MAKEFILE_DIR / f"{design}.makefile"
    path.write_text(IVERILOG_MAKEFILE.format(design=design))
    return str(path.resolve())

SYSTEM_PROMPT_TEMPLATE = (
    "You are an expert Verilog designer working in a sandbox directory. "
    "The natural-language spec is in `design_description.txt` and the testbench "
    "is in `testbench.v`. A `makefile` provides `vcs`, `sim`, and `clean` targets "
    "(iverilog + vvp). Write your synthesizable module to `{design}.v` in the "
    "current directory, matching the module name and I/O signals from the spec. "
    "You may run `make clean && make vcs && make sim` to compile and simulate; "
    "the simulation prints `Passed` on success. Iterate until simulation passes. "
    "Then optimize the design for PPA (power, performance, area): prefer fewer "
    "sequential cells, narrower datapaths, shared logic, and shallower "
    "combinational depth. The `yosys-runner` MCP server exposes a synthesis tool "
    "that runs yosys on `{design}.v` and returns the cell/wire stats — call it "
    "(use whatever exact name appears in your tool list) and treat the cell "
    "count as the area metric, iterating to bring it down while keeping the "
    "testbench green. Do not change the module interface or alter the testbench."
)

YOSYS_MCP = MCPServerConfigStdio(
    name="yosys-runner",
    command="python3",
    args=["/opt/yosys_runner.py"],
)

yosys_mcp = mcp_server_sandbox(
    name="yosys-runner",
    command="python3",
    args=["/opt/yosys_runner.py"],
)

def find_design(name: str) -> Path:
    for path in RTLLM_ROOT.rglob(name):
        if path.is_dir() and (path / "design_description.txt").exists():
            return path
    sys.exit(f"Design not found: {name}")


def design_files(folder: Path, design: str) -> dict[str, str]:
    """Map sandbox-relative path → host path for files the agent should see.

    Copies `design_description.txt` and `testbench.v` from the upstream RTLLM
    submodule (which is kept pristine), skips ground-truth solutions and the
    upstream VCS makefile, and substitutes our own iverilog-based makefile.
    """
    skip_exact = {f"{design}.v"}
    out: dict[str, str] = {}
    for src in folder.iterdir():
        if not src.is_file() or src.name in skip_exact:
            continue
        if any(fnmatch.fnmatch(src.name, pat) for pat in SKIP_COPY_PATTERNS):
            continue
        out[src.name] = str(src.resolve())
    for required in ("design_description.txt", "testbench.v"):
        if required not in out:
            sys.exit(f"Design folder missing required file: {required}")
    out["makefile"] = iverilog_makefile_path(design)
    return out


@scorer(metrics=[accuracy(), stderr()])
def rtllm_make_passes() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        sbox = sandbox()
        await sbox.exec(["make", "clean"])
        vcs = await sbox.exec(["make", "vcs"])
        if not vcs.success:
            return Score(
                value=INCORRECT,
                explanation=f"make vcs failed (rc={vcs.returncode}):\n{vcs.stderr}",
            )
        sim = await sbox.exec(["make", "sim"])
        if not sim.success:
            return Score(
                value=INCORRECT,
                answer=sim.stdout,
                explanation=f"make sim failed (rc={sim.returncode})",
            )
        if "Passed" not in sim.stdout:
            return Score(
                value=INCORRECT,
                answer=sim.stdout,
                explanation="testbench did not print 'Passed'",
            )
        return Score(value=CORRECT, answer=sim.stdout)

    return score


_CELL_COUNT_RE = re.compile(r"Number of cells:\s+(\d+)")


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

        sbox = sandbox()
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


@task
def rtllm_generate_and_test(design: str) -> Task:
    folder = find_design(design)
    description = (folder / "design_description.txt").read_text()

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
    #         mcp_servers=[YOSYS_MCP],
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
            tools=[yosys_mcp, web_search(), bash(), python(), bash_session(), text_editor(), code_execution(), update_plan(), memory(), think()],
            on_continue="Please proceed to the next step using your best judgement. If you believe you are done, please call the `submit()` tool."
        ),
        scorer=[rtllm_make_passes(), yosys_cell_count(design)],
        sandbox=("docker", str(SANDBOX_COMPOSE)),
    )
