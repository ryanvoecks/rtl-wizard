"""Drive the OAUTH-token Claude Code agent against RTL optimisation tasks.

Entry point is `llm_eval/run.py` -- it owns the per-run output dir and
forwards it into `optimize_timing(output_dir)`. Prerequisite:
`claude setup-token` once, then export CLAUDE_CODE_OAUTH_TOKEN.
"""

from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.util import SandboxEnvironmentSpec
from inspect_ai.util._sandbox.compose import ComposeConfig, parse_compose_yaml

from common.config import LLM_EVAL, TargetConfig
from common.targets import all_targets

from .mcp_connect import discover_shared_network
from .scorers import (
    SANDBOX_RTL_ROOT,
    synthesis,
    testbench,
)
from .solvers import claude_code_solver

SANDBOX_COMPOSE = LLM_EVAL / "sandbox" / "compose.yaml"


def _build_sandbox_compose() -> ComposeConfig:
    """Load the compose template and inject the discovered shared-network
    name directly into the config."""
    config = parse_compose_yaml(str(SANDBOX_COMPOSE))
    network, _ = discover_shared_network()
    if config.networks and "shared" in config.networks:
        config.networks["shared"]["name"] = network
    sandbox_dir = SANDBOX_COMPOSE.parent.resolve()
    for svc in config.services.values():
        if svc.build and not isinstance(svc.build, str) and svc.build.context:
            svc.build.context = str(sandbox_dir / svc.build.context)
    return config


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
            "RTL files -- run `git submodule update --init` if the upstream "
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
            "rename them."
        ),
        target="",
        files={
            sandbox_path: str(host_path.resolve())
            for sandbox_path, host_path in zip(sandbox_paths, rtl_files)
        },
        metadata={"synth_target": target},
    )


@task
def optimize_timing(output_dir: Path) -> Task:
    # Dataset is all valid synthesis targets
    dataset = [_build_sample(t) for t in all_targets]

    # 2 requirements for progress: synthesisable and functionally correct
    scorers = [synthesis(output_dir), testbench(output_dir)]

    # Sandbox needs network config to access MCP servers
    sandbox = SandboxEnvironmentSpec("docker", config=_build_sandbox_compose())

    return Task(
        dataset=dataset,
        solver=claude_code_solver(),
        scorer=scorers,
        sandbox=sandbox,
        tags=["claude-code"],
    )
