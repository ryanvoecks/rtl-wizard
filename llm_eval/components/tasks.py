"""Drive the OAUTH-token Claude Code agent against RTL optimisation tasks.

Entry point is `llm_eval/run.py` -- it owns the per-run output dir AND
the shared container lifecycle. Each sample's solver creates its own
ClaudeEnv (fresh unix user + workdir inside the shared container) and
stages RTL into it, so samples are fully independent.
"""

from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.solver import Solver

from common.config import TargetConfig
from common.targets import all_targets

from .scorers import synthesis, testbench


def _build_sample(target: TargetConfig) -> Sample:
    design = target.design
    if not design.rtl_files:
        raise FileNotFoundError(f"design {design.name} has no files - run setup.sh")
    return Sample(
        id=design.name,
        input="",
        target="",
        metadata={"synth_target": target},
    )


@task
def optimize_timing(output_dir: Path, solver: Solver) -> Task:
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
