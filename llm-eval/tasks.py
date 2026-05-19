"""Drive the OAUTH-token Claude Code agent against RTL optimisation tasks.

Run with `uv run inspect eval llm-eval/tasks.py --model none/<model>`.
Prerequisite: `claude setup-token` once, then export CLAUDE_CODE_OAUTH_TOKEN.
Per-run logs + artifacts (diff.patch, *.log) land under `llm-results/<ts>/`.
"""
import sys
from pathlib import Path

# inspect_ai loads task files via SourceFileLoader without adding their parent
# directory to sys.path. The containing dir name (`llm-eval`) has a dash so it
# can't be a Python package; add the dir itself to sys.path so siblings are
# importable as bare modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspect_ai import Task, task
from inspect_ai.dataset import Sample

from common.config import TargetConfig
from common.targets import all_targets
from scorers import (
    SANDBOX_RTL_ROOT,
    synthesis,
    testbench,
)
from solvers import claude_code_solver

REPO_ROOT = Path(__file__).parent.parent
NB_ROOT = Path(__file__).parent
SANDBOX_COMPOSE = NB_ROOT / "sandbox" / "compose.yaml"


def _sandbox_rtl_path(design, host_path: Path) -> str:
    """Sandbox location for an RTL file: mirrors its path relative to
    `design.rtl_dir`, anchored at `SANDBOX_RTL_ROOT`."""
    return f"{SANDBOX_RTL_ROOT}/{host_path.relative_to(design.rtl_dir)}"


def _build_sample(target: TargetConfig) -> Sample:
    design = target.design
    rtl_files = list(design.rtl_files)
    if not rtl_files:
        raise FileNotFoundError(
            f"design {design.benchmark}/{design.name}/{design.variant} has no "
            "RTL files — run `git submodule update --init` if the upstream "
            "repo is a submodule"
        )
    sandbox_paths = [_sandbox_rtl_path(design, p) for p in rtl_files]
    top_file = next((p for p in rtl_files if p.stem == design.top_module), rtl_files[0])
    top_sandbox = _sandbox_rtl_path(design, top_file)
    return Sample(
        id=f"{design.benchmark}/{design.name}/{design.variant}",
        input=(
            f"There is an RTL design in `{SANDBOX_RTL_ROOT}/` (top module: "
            f"`{design.top_module}`, in `{top_sandbox}`). Your job is to "
            "reduce the **longest combinational path** through the design "
            "— the path that gates the achievable clock period.\n\n"
            "Constraints:\n"
            "- Preserve functional behaviour. No testbench is available "
            "to you, so reason about the RTL directly.\n"
            "- The design must remain synthesisable by yosys (the scorer "
            f"runs yosys over `{SANDBOX_RTL_ROOT}/` after you finish).\n"
            f"- Edit the files in `{SANDBOX_RTL_ROOT}/` in place; do not "
            "rename them."
        ),
        target="",
        files={
            sandbox_path: str(host_path.resolve())
            for sandbox_path, host_path in zip(sandbox_paths, rtl_files)
        },
        metadata={"design": design},
    )


@task
def optimize_timing() -> Task:
    # Dataset is all valid synthesis targets
    dataset = [_build_sample(t) for t in all_targets]

    # 2 requirements for progress: synthesisable and functionally correct
    scorers = [synthesis(), testbench()]

    return Task(
        dataset=dataset,
        solver=claude_code_solver(),
        scorer=scorers,
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        tags=["claude-code"],
    )
