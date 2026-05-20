"""Per-sample MCP tool definitions.

`Tools` wraps the per-sample `TargetConfig` so its methods can be exposed as
MCP tools with `self` already bound (and therefore absent from the inferred
tool schema). `make_server` picks a subset of methods to register on a fresh
FastMCP instance. The network transport is handled separately by
`mcp_connect.py`.
"""

import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from common.config import TargetConfig

from .scorers import build_diff_from_sandbox, evaluate_testbench

OUTPUT_LIMIT = 20_000  # Truncate overly long testbenches


class Tools:
    def __init__(self, synth_target: TargetConfig):
        self.synth_target = synth_target

    async def run_testbench(self) -> str:
        """Run the design's hidden testbench against your current RTL. A return
        code of 0 means the testbench passed."""
        design = self.synth_target.design
        try:
            diff = await build_diff_from_sandbox(design)
            log, rc = await asyncio.to_thread(evaluate_testbench, design, diff)
        except Exception as e:
            return f"[tool error] {type(e).__name__}: {e}"

        header = f"[rc={rc}]\n"
        if len(log) > OUTPUT_LIMIT:
            log = log[-OUTPUT_LIMIT:]
            header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
        return header + log


def make_server(
    name: str, method_names: list[str], synth_target: TargetConfig
) -> FastMCP:
    """Build a FastMCP server exposing the named `Tools` methods.
    DNS-rebinding protection is disabled for docker-in-docker support."""

    mcp = FastMCP(
        name,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    t = Tools(synth_target)
    for m in method_names:
        mcp.add_tool(getattr(t, m))
    return mcp
