"""Drive the OAUTH-token Claude Code agent against RTL optimisation tasks.

Entry point is `llm_eval/run.py` -- it owns the per-run output dir AND
the shared container lifecycle. Each sample's solver creates its own
ClaudeEnv (fresh unix user + workdir inside the shared container) and
stages RTL into it, so samples are fully independent.
"""

from pathlib import Path

from claude_env import SANDBOX_RTL_ROOT
from container import Container
from inspect_ai import Task, task
from inspect_ai.dataset import Sample

from common.config import TargetConfig
from common.targets import all_targets

from .scorers import synthesis, testbench
from .solvers import claude_code_solver


def _build_sample(target: TargetConfig) -> Sample:
    design = target.design
    rel_files = list(design.rtl_files)
    if not rel_files:
        raise FileNotFoundError(
            f"design {design.benchmark}/{design.name}/{design.variant} has no "
            "RTL files -- run `git submodule update --init` if the upstream "
            "repo is a submodule"
        )
    top_file = next((p for p in rel_files if p.stem == design.top_module), rel_files[0])
    top_sandbox = f"{SANDBOX_RTL_ROOT}/{top_file}"
    return Sample(
        id=f"{design.benchmark}/{design.name}/{design.variant}",
        input=(
            f"There is an RTL design in `{SANDBOX_RTL_ROOT}/` (top module: "
            f"`{design.top_module}`, in `{top_sandbox}`). Your job is to "
            "reduce the **longest combinational path** through the design "
            "-- the path that gates the achievable clock period.\n\n"
            "Constraints:\n"
            "- Preserve functional behaviour. You cannot read the "
            "testbench, but you can call the `run_testbench` MCP tool to "
            "run it against your current RTL -- it returns the testbench's "
            "exit code and stdout so you can sanity-check edits before "
            "finishing.\n"
            "- The design must remain synthesisable by yosys (the scorer "
            f"runs yosys over `{SANDBOX_RTL_ROOT}/` after you finish).\n"
            f"- Edit the files in `{SANDBOX_RTL_ROOT}/` in place; do not "
            "rename them.\n\n"
            "To measure your progress, call the `synth_timing_report` MCP "
            "tool: it synthesises your current RTL through ORFS and returns "
            "a post-synth logical-paths report -- the worst register-to-"
            "register groups ranked by slack, with their containing modules "
            "and LOC. Use it to find which paths to attack and to confirm "
            "an edit actually shortened the longest combinational path."
        ),
        target="",
        metadata={"synth_target": target},
    )


@task
def optimize_timing(output_dir: Path, container: Container) -> Task:
    # Dataset is all valid synthesis targets
    dataset = [_build_sample(t) for t in all_targets]

    # 2 requirements for progress: synthesisable and functionally correct
    scorers = [synthesis(output_dir), testbench(output_dir)]

    # Each solver runs in an isolated ClaudeEnv within a shared container
    return Task(
        dataset=dataset,
        solver=claude_code_solver(container),
        scorer=scorers,
        tags=["claude-code"],
    )
