"""Per-sample MCP tool definitions.

`Tools` wraps the per-sample `TargetConfig` plus the live `ClaudeEnv` so
its methods can be exposed as MCP tools with `self` already bound (and
therefore absent from the inferred tool schema). The env reference lets
tool methods diff the agent's in-flight RTL state without a sandbox.
`make_server` picks a subset of methods to register on a fresh FastMCP
instance. The network transport is handled separately by `mcp_connect.py`.
"""

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from common.config import SYNTH_FLOW_TARGETS, RunConfig, TargetConfig
from eda_eval.analyse_synth import analyse_synth
from eda_eval.run import run_job

from .claude_env import ClaudeEnv, build_diff_from_env
from .scorers import _create_copy, evaluate_testbench

OUTPUT_LIMIT = 20_000  # Truncate overly long tool outputs


def _synth_and_report(synth_target: TargetConfig, diff: str) -> str:
    """Apply `diff` to a copy of the design root, drive the ORFS synth flow
    into a fresh tempdir, then run analyse_synth and return its logical-
    paths report."""

    # Create a modified target for the agent's RTL
    patched_root = _create_copy(synth_target.design, diff)
    patched_design = replace(synth_target.design, root=patched_root)
    patched_target = replace(synth_target, design=patched_design)

    # Run synthesis
    output_dir = Path(tempfile.mkdtemp())
    run = RunConfig(
        synth_target=patched_target,
        output_dir=output_dir,
        flow_targets=SYNTH_FLOW_TARGETS,
    )
    rc = run_job(run)
    if rc != 0:
        log_path = output_dir / "flow.log"
        log = log_path.read_text() if log_path.is_file() else ""
        raise RuntimeError(f"[synth failed] [rc={rc}]\n{log}")

    return analyse_synth(
        output_dir,
        patched_design.top_module,
        patched_target.cfg.platform,
        patched_design.rtl_abs_paths,
    )


class Tools:
    def __init__(self, synth_target: TargetConfig, env: ClaudeEnv):
        self.synth_target = synth_target
        self.env = env

    async def run_testbench(self) -> str:
        """Run the design's hidden testbench against your current RTL. A return
        code of 0 means the testbench passed."""
        design = self.synth_target.design
        try:
            diff = await asyncio.to_thread(build_diff_from_env, self.env, design)
            log, rc = await asyncio.to_thread(evaluate_testbench, design, diff)
        except Exception as e:
            return f"[tool error] {type(e).__name__}: {e}"

        header = f"[rc={rc}]\n"
        if len(log) > OUTPUT_LIMIT:
            log = log[-OUTPUT_LIMIT:]
            header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
        return header + log

    async def synth_timing_report(self) -> str:
        """Synthesize your current RTL and return a post-synth logical-paths
        timing report. Optimistic vs. post-route, but useful for ranking edits."""
        try:
            diff = await asyncio.to_thread(
                build_diff_from_env, self.env, self.synth_target.design,
            )
            report = await asyncio.to_thread(_synth_and_report, self.synth_target, diff)
        except Exception as e:
            return f"[tool error] {type(e).__name__}: {e}"
        if len(report) > OUTPUT_LIMIT:
            report = (
                report[-OUTPUT_LIMIT:]
                + f"\n[output truncated to last {OUTPUT_LIMIT} chars]"
            )
        return report


def make_server(
    name: str,
    method_names: list[str],
    synth_target: TargetConfig,
    env: ClaudeEnv,
) -> FastMCP:
    """Build a FastMCP server exposing the named `Tools` methods.
    DNS-rebinding protection is disabled for docker-in-docker support."""

    mcp = FastMCP(
        name,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    t = Tools(synth_target, env)
    for m in method_names:
        mcp.add_tool(getattr(t, m))
    return mcp
