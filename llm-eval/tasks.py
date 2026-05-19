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
from common.targets import aes_target
from scorers import (
    testbench_passes,
    yosys_synthesisable,
)
from solvers import claude_code_solver

REPO_ROOT = Path(__file__).parent.parent
NB_ROOT = Path(__file__).parent
SANDBOX_COMPOSE = NB_ROOT / "compose.yaml"


def _sandbox_rtl_path(host_path: Path) -> str:
    """Sandbox-relative location where an RTL file lands in the agent's tree.

    Flat namespace under `rtl/` regardless of upstream repo layout — the
    scorers reverse this mapping using `design.tb_repo_root` + each
    `rtl_file`'s repo-relative path."""
    return f"rtl/{host_path.name}"


def _build_aes_dataset(target: TargetConfig) -> list[Sample]:
    design = target.design
    rtl_files = list(design.rtl_files)
    if not rtl_files:
        raise FileNotFoundError(
            f"design {design.benchmark}/{design.name}/{design.variant} has no "
            "RTL files — run `git submodule update --init` if the upstream "
            "repo is a submodule"
        )
    sandbox_paths = [_sandbox_rtl_path(p) for p in rtl_files]
    top_file = next((p for p in rtl_files if p.stem == design.top_module), rtl_files[0])
    top_sandbox = _sandbox_rtl_path(top_file)
    return [
        Sample(
            id=design.name,
            input=(
                f"There is an RTL design in `rtl/` (top module: `{design.top_module}`, "
                f"in `{top_sandbox}`). Your job is to reduce the **longest "
                "combinational path** through the design — the path that "
                "gates the achievable clock period.\n\n"
                "Constraints:\n"
                "- Preserve functional behaviour. No testbench is available "
                "to you, so reason about the RTL directly.\n"
                "- The design must remain synthesisable by yosys (the scorer "
                "runs yosys over `rtl/*.v` after you finish).\n"
                "- Edit the files in `rtl/` in place; do not rename them."
            ),
            target="",
            files={
                sandbox_path: str(host_path.resolve())
                for sandbox_path, host_path in zip(sandbox_paths, rtl_files)
            },
            metadata={
                "original_files": {
                    sandbox_path: host_path.read_text()
                    for sandbox_path, host_path in zip(sandbox_paths, rtl_files)
                },
            },
        )
    ]


@task
def optimize_aes(
    target: TargetConfig = aes_target, message_limit: int = 200
) -> Task:
    design = target.design
    scorers = [yosys_synthesisable(design)]
    # Gate the testbench scorer on the design actually shipping a runnable
    # harness. Designs from RTLLM/RTL-OPT/Corpus loaders leave run_tb None
    # and grade on synthesisability alone.
    if design.run_tb is not None:
        scorers.append(testbench_passes(design))
    return Task(
        dataset=_build_aes_dataset(target),
        solver=claude_code_solver(),
        scorer=scorers,
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        message_limit=message_limit,
        tags=["claude-code", "rtl"],
    )
