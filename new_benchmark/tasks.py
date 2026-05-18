"""Smoke test: drive the OAUTH-token Claude Code agent against a broken hello.py.

Run with:
    uv run inspect eval new_benchmark/tasks.py --model none/claude-sonnet-4-5

The `.eval` log lands under `outputs/<RUN_TIMESTAMP>/` alongside per-sample
artifacts (diff.patch, etc.) — see `new_benchmark/outputs.py` for the
recorder redirect that makes this work without --log-dir.

Prerequisite (one-time on the host):
    claude setup-token
    export CLAUDE_CODE_OAUTH_TOKEN=<paste>

Builds the lightweight new_benchmark sandbox image on first run (~2–3 min).
"""
import sys
from pathlib import Path

# inspect_ai loads task files via SourceFileLoader without adding their parent
# directory to sys.path, so the `new_benchmark` package isn't importable by name
# yet. Add the repo root before importing siblings. Mirrors benchmark/tasks.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspect_ai import Task, task
from inspect_ai.dataset import Sample

from new_benchmark.scorers import aes_yosys_synthesisable, hello_world_runs
from new_benchmark.solvers import claude_code_solver

REPO_ROOT = Path(__file__).parent.parent
NB_ROOT = Path(__file__).parent
SANDBOX_COMPOSE = NB_ROOT / "compose.yaml"
BROKEN_HELLO = NB_ROOT / "samples" / "broken_hello.py"
AES_RTL_DIR = REPO_ROOT / "external" / "aes" / "src" / "rtl"


def _build_dataset() -> list[Sample]:
    # original_files is consumed by the scorer to compute a unified diff against
    # the agent's final state. Pre-reading the contents here keeps the scorer
    # decoupled from host paths (sandbox-only at score time).
    hello_original = BROKEN_HELLO.read_text()
    return [
        Sample(
            id="broken_hello",
            input=(
                "There is a broken Python program at `hello.py` in your working "
                "directory. Fix it so that running `python3 hello.py` prints "
                "exactly: Hello, World!"
            ),
            target="Hello, World!",
            files={"hello.py": str(BROKEN_HELLO.resolve())},
            metadata={
                "original_files": {"hello.py": hello_original},
            },
        )
    ]


def _build_aes_dataset() -> list[Sample]:
    rtl_files = sorted(AES_RTL_DIR.glob("*.v"))
    if not rtl_files:
        raise FileNotFoundError(
            f"no .v files under {AES_RTL_DIR} — run `git submodule update --init` "
            "to fetch the secworks/aes submodule"
        )
    sandbox_paths = [f"rtl/{p.name}" for p in rtl_files]
    return [
        Sample(
            id="aes",
            input=(
                "There is an AES-128 RTL design in `rtl/` (top module: `aes`, "
                "in `rtl/aes.v`). Your job is to reduce the **longest "
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
def optimize_aes(message_limit: int = 200) -> Task:
    return Task(
        dataset=_build_aes_dataset(),
        solver=claude_code_solver(),
        scorer=aes_yosys_synthesisable(),
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        message_limit=message_limit,
        tags=["claude-code", "rtl"],
    )


@task
def fix_broken_hello(message_limit: int = 200) -> Task:
    # The Claude Code model is taken from Inspect's `--model` flag at solve
    # time (see solvers._resolve_claude_model). Pass it as `none/<model>` so
    # Inspect doesn't try to instantiate an API client — e.g.:
    #     inspect eval new_benchmark/tasks.py --model none/claude-sonnet-4-5
    # Without `--model`, Inspect falls back to INSPECT_EVAL_MODEL from .env
    # (currently gpt-oss-120b for the rtl-wizard benchmark); the solver then
    # falls back to its own DEFAULT_CLAUDE_MODEL but the viewer will still
    # show that env value. Pass `--model` to keep the label honest.
    return Task(
        dataset=_build_dataset(),
        solver=claude_code_solver(),
        scorer=hello_world_runs(),
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        message_limit=message_limit,
        tags=["claude-code"],
    )
