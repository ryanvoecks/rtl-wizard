"""Per-sample MCP tool definitions.

`Tools` wraps the per-sample `TargetConfig` so its methods can be exposed as
MCP tools with `self` already bound (and therefore absent from the inferred
tool schema). `make_server` picks a subset of methods to register on a fresh
FastMCP instance. The network transport is handled separately by
`mcp_connect.py`.
"""

import asyncio
import contextlib
import io
import tempfile
from dataclasses import replace
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from common.config import RunConfig, TargetConfig
from eda_eval import analyse_synth
from eda_eval.run import run_job

from .scorers import _create_copy, build_diff_from_sandbox, evaluate_testbench

OUTPUT_LIMIT = 20_000  # Truncate overly long tool outputs

# ORFS targets that land 1_synth.odb/sdc -- the inputs analyse_synth needs.
# Floorplan/CTS/route are skipped; this is post-synth, pre-P&R.
SYNTH_FLOW_TARGETS = ("synth", "synth-report")

# How aggressively analyse_synth samples timing paths. Logical-group count
# is what we surface; the per-path pool stays large so the grouping is
# statistically meaningful, but we don't need the raw critical-paths report.
_ANALYSE_TOP_LOGICAL = 10
_ANALYSE_POOL = 1000


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

    async def synth_timing_report(self) -> str:
        """Synthesize your current RTL and return a post-synth logical-paths
        timing report. Optimistic vs. post-route, but useful for ranking edits."""
        try:
            diff = await build_diff_from_sandbox(self.synth_target.design)
            report = await asyncio.to_thread(
                _synth_and_report, self.synth_target, diff
            )
        except Exception as e:
            return f"[tool error] {type(e).__name__}: {e}"
        if len(report) > OUTPUT_LIMIT:
            report = (
                report[-OUTPUT_LIMIT:]
                + f"\n[output truncated to last {OUTPUT_LIMIT} chars]"
            )
        return report


def _synth_and_report(synth_target: TargetConfig, diff: str) -> str:
    """Apply `diff` to a copy of the design root, drive the ORFS synth flow
    into a fresh tempdir, then run analyse_synth and return its logical-
    paths report."""
    design = synth_target.design

    # Patch the agent's edits onto a working copy of design.root, then build
    # a parallel DesignConfig anchored under it so run_job's snapshot picks
    # up the modified sources instead of the originals.
    patched_root = _create_copy(design, diff)
    rel_rtl_dir = design.rtl_dir.relative_to(design.root)
    patched_rtl_dir = patched_root / rel_rtl_dir
    patched_design = replace(
        design,
        root=patched_root,
        rtl_dir=patched_rtl_dir,
        rtl_files=tuple(
            patched_rtl_dir / f.relative_to(design.rtl_dir) for f in design.rtl_files
        ),
    )
    patched_target = replace(synth_target, design=patched_design)

    output_dir = Path(tempfile.mkdtemp(prefix="synth_timing_"))
    run = RunConfig(
        synth_target=patched_target,
        output_dir=output_dir,
        flow_targets=SYNTH_FLOW_TARGETS,
    )
    rc = run_job(run)
    if rc != 0:
        log_path = output_dir / "flow.log"
        tail = log_path.read_text()[-OUTPUT_LIMIT:] if log_path.is_file() else ""
        return f"[synth failed, rc={rc}] see {log_path}\n{tail}"

    # analyse_synth.main prints status to stdout; capture it so it doesn't
    # bleed into the MCP server's logs, and grab its rc to detect zero-path
    # outcomes (purely combinational designs, etc.).
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            rc_an = analyse_synth.main(
                [
                    str(output_dir),
                    "--top-logical",
                    str(_ANALYSE_TOP_LOGICAL),
                    "--pool",
                    str(_ANALYSE_POOL),
                ]
            )
    except SystemExit as exc:
        rc_an = exc.code if isinstance(exc.code, int) else 2
    if rc_an != 0:
        return f"[analyse_synth failed, rc={rc_an}]\n{sink.getvalue()[-OUTPUT_LIMIT:]}"

    top_module = patched_design.top_module
    platform = patched_target.cfg.platform
    logical_rpt = (
        output_dir / "reports" / platform / top_module / "base" / "logical_paths_synth.rpt"
    )
    if not logical_rpt.is_file():
        return f"[missing logical_paths_synth.rpt under {logical_rpt.parent}]"
    return logical_rpt.read_text()


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
