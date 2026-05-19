"""Drive the OAUTH-token Claude Code agent against RTL optimisation tasks.

Run with:
    uv run inspect eval llm-eval/tasks.py --model none/claude-sonnet-4-5

The Claude Code model is taken from Inspect's `--model` flag at solve time
(see solvers._resolve_claude_model). Pass it as `none/<model>` so Inspect
doesn't try to instantiate an API client. Without `--model`, Inspect falls
back to INSPECT_EVAL_MODEL from .env (currently gpt-oss-120b for the
rtl-wizard benchmark); the solver then falls back to its own
DEFAULT_CLAUDE_MODEL but the viewer will still show that env value. Pass
`--model` to keep the label honest.

The `.eval` log lands under `llm-results/<RUN_TIMESTAMP>/` alongside per-sample
artifacts (diff.patch, etc.) — see `llm-eval/outputs.py` for the recorder
redirect that makes this work without --log-dir.

Prerequisite (one-time on the host):
    claude setup-token
    export CLAUDE_CODE_OAUTH_TOKEN=<paste>

Builds the lightweight llm-eval sandbox image on first run (~2–3 min).
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
    functional,
    synthesisable,
)
from solvers import claude_code_solver

REPO_ROOT = Path(__file__).parent.parent
NB_ROOT = Path(__file__).parent
SANDBOX_COMPOSE = NB_ROOT / "compose.yaml"


def _sandbox_rtl_path(design, host_path: Path) -> str:
    """Sandbox location for an RTL file: mirrors its path relative to
    `design.rtl_dir`, anchored at `SANDBOX_RTL_ROOT`. The scorers invert
    this by reading back `<SANDBOX_RTL_ROOT>/<rel>` and applying the diff
    relative to `rtl_dir`."""
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
    scorers = [synthesisable(), functional()]

    return Task(
        dataset=dataset,
        solver=claude_code_solver(),
        scorer=scorers,
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        tags=["claude-code"],
    )
