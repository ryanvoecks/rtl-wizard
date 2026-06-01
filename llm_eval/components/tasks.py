"""Drive the OAUTH-token Claude Code agent against RTL optimisation tasks.

Entry point is `llm_eval/run.py` -- it owns the per-run output dir AND
the shared container lifecycle. Each sample's solver creates its own
ClaudeEnv (fresh unix user + workdir inside the shared container) and
stages RTL into it, so samples are fully independent.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.solver import Solver

from common.config import TargetConfig
from common.targets import all_targets
from components.claude_env import SANDBOX_RTL_ROOT
from components.scorers import synthesis, testbench


def _design_prompt(target: TargetConfig) -> str:
    """Task-side prompt: describes design, file layout, and goal. Solver-specific
    instructions are appended before sending to Claude."""
    design = target.design
    top_file = next(
        (p for p in design.rtl_files if p.stem == design.top_module),
        design.rtl_files[0],
    )
    top_sandbox = f"{SANDBOX_RTL_ROOT}/{top_file}"
    return (
        f"There is an RTL design in `{SANDBOX_RTL_ROOT}/` (top module: "
        f"`{design.top_module}`, in `{top_sandbox}`). Your job is to "
        "increase the maximum clock frequency of the design by as much "
        "as possible.\n\n"
        "Constraints:\n"
        "- Preserve functional behaviour.\n"
        "- The design must remain synthesisable by yosys.\n"
        "- The area/power of the design should not increase by more than "
        "10%.\n"
        f"- Edit the files in `{SANDBOX_RTL_ROOT}/` in place. Do not "
        "rename them."
    )


def _build_sample(target: TargetConfig) -> Sample:
    design = target.design
    if not design.rtl_files:
        raise FileNotFoundError(f"design {design.name} has no files - run setup.sh")
    return Sample(
        id=design.name,
        input=_design_prompt(target),
        target="",
        metadata={"synth_target": target},
    )


@task
def optimize_timing(output_dir: str, solver: Solver) -> Task:
    # Dataset is all valid synthesis targets
    dataset = [_build_sample(t) for t in all_targets]

    # 2 requirements for progress: synthesisable and functionally correct
    scorers = [synthesis(output_dir), testbench(output_dir)]

    return Task(
        dataset=dataset,
        solver=solver,
        scorer=scorers,
        tags=["claude-code"],
    )
