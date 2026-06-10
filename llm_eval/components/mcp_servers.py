"""Per-sample MCP tool definitions.

`Tools` wraps the per-sample `TargetConfig` plus the live `ClaudeEnv` so
its methods can be exposed as MCP tools with `self` already bound (and
therefore absent from the inferred tool schema). The env reference lets
tool methods diff the agent's in-flight RTL state without a sandbox.
`make_server` picks a subset of methods to register on a fresh FastMCP
instance. The network transport is handled separately by `mcp_connect.py`.
"""

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from common.config import ALL_FLOW_TARGETS, SYNTH_FLOW_TARGETS, RunConfig, TargetConfig
from components.claude_env import ClaudeEnv, build_diff_from_env
from components.scorers import _design_copy, evaluate_synthesis, evaluate_testbench
from eda_eval.analyse import analyse
from eda_eval.run import run_job

OUTPUT_LIMIT = 20_000  # Truncate overly long tool outputs
CHECK_TAIL_LINES = 15  # run_testbench: keep only the last N lines of each check's log
PNR_THREADS = 4  # ORFS cores for the post-route iterative-feedback report


def _format_check(label: str, rc: int, log: str) -> str:
    """Render one check's exit code + (tail-truncated) log as a labelled block."""
    header = f"[{label} rc={rc}]\n"
    lines = log.splitlines()
    if len(lines) > CHECK_TAIL_LINES:
        log = "\n".join(lines[-CHECK_TAIL_LINES:])
        header += f"[{label} output truncated to last {CHECK_TAIL_LINES} lines]\n"
    return header + log


def _flow_report_df(
    synth_target: TargetConfig,
    diff: str = "",
    *,
    flow_targets: tuple[str, ...] = SYNTH_FLOW_TARGETS,
    num_threads: int = 1,
    apply_correction: bool = True,
) -> pd.DataFrame:
    """Apply `diff` to a copy of the design root, drive the ORFS flow
    (`flow_targets` on `num_threads` cores) into a fresh tempdir, then run
    analyse and return its logical-paths report as a DataFrame (ranked
    worst-slack first). `analyse` keys off the latest produced stage, so a
    synth-only `flow_targets` yields a post-synth (zero-RC) report and a full
    P&R one yields a post-route report. Both the design copy and the ORFS
    output dir are removed before returning so repeated tool calls don't fill
    the host disk."""

    # Create a modified target for the agent's RTL
    with _design_copy(synth_target.design, diff) as patched_root:
        patched_design = replace(synth_target.design, root=patched_root)
        patched_target = replace(synth_target, design=patched_design)

        # Run the flow into a fresh tempdir. run_job replaces output_dir with
        # a symlink into the content-addressed EDA cache.
        output_dir = Path(tempfile.mkdtemp())
        try:
            run = RunConfig(
                synth_target=patched_target,
                output_dir=output_dir,
                flow_targets=flow_targets,
                num_threads=num_threads,
            )
            rc = run_job(run)
            if rc != 0:
                log_path = output_dir / "flow.log"
                log = log_path.read_text() if log_path.is_file() else ""
                raise RuntimeError(f"[flow failed] [rc={rc}]\n{log}")

            return analyse(run, apply_correction=apply_correction)
        finally:
            # Drop just the symlink (or the dir, if run_job failed before
            # linking); never the cache entry it points at.
            if output_dir.is_symlink():
                output_dir.unlink()
            else:
                shutil.rmtree(output_dir, ignore_errors=True)


def _synth_report_df(synth_target: TargetConfig, diff: str = "") -> pd.DataFrame:
    """Post-synth (zero-RC) logical-paths report: synth-only flow on one core
    with the corrector-aware extractor. Used by the `synth_report` MCP tool
    and the one-shot initial report."""
    return _flow_report_df(synth_target, diff)


def _pnr_report_df(synth_target: TargetConfig, diff: str = "") -> pd.DataFrame:
    """Post-route logical-paths report: full ORFS P&R on `PNR_THREADS` cores
    with the raw (uncorrected) post-parasitics slacks. Used by the iterative
    solver's per-round feedback."""
    return _flow_report_df(
        synth_target,
        diff,
        flow_targets=ALL_FLOW_TARGETS,
        num_threads=PNR_THREADS,
        apply_correction=False,
    )


@dataclass(frozen=True)
class ReportKind:
    """A logical-paths report generator paired with the stage label used in
    prompts. A solver references a single `ReportKind` for both its initial
    report and its per-round feedback, so the two can never describe or run
    different ORFS flows."""

    df: Callable[[TargetConfig, str], pd.DataFrame]
    label: str  # human-readable stage, e.g. "post-synth" / "post-route"


SYNTH_REPORT = ReportKind(_synth_report_df, "post-synth")
PNR_REPORT = ReportKind(_pnr_report_df, "post-route")


def _synth_and_report(synth_target: TargetConfig, diff: str = "") -> str:
    """Post-synth logical-paths report rendered as a printable table."""
    return _synth_report_df(synth_target, diff).to_string(index=False)


def wns_path_from_df(df: pd.DataFrame) -> tuple[float, str] | None:
    """The worst-negative-slack path the logical-paths report ranks first."""
    if df.empty:
        return None
    row = df.iloc[0]
    return float(row["worst_slack_ns"]), f"{row['start']} -> {row['end']}"


class Tools:
    def __init__(self, synth_target: TargetConfig, env: ClaudeEnv):
        self.synth_target = synth_target
        self.env = env

    async def run_testbench(self) -> str:
        """Check your current RTL for functional correctness and synthesisability.
        A return code of 0 on both means your edits are valid."""
        design = self.synth_target.design
        try:
            diff = await asyncio.to_thread(build_diff_from_env, self.env, design)
            synth_log, synth_rc = await asyncio.to_thread(
                evaluate_synthesis, design, diff
            )
            tb_log, tb_rc = await asyncio.to_thread(evaluate_testbench, design, diff)
        except Exception as e:
            return f"[tool error] {type(e).__name__}: {e}"

        return (
            _format_check("synthesis", synth_rc, synth_log)
            + "\n"
            + _format_check("testbench", tb_rc, tb_log)
        )

    async def synth_report(self) -> str:
        """Synthesize your current RTL and return a post-synth logical-paths
        timing report. Also reports total area/power. Optimistic vs. post-route,
        but useful for ranking edits."""
        try:
            diff = await asyncio.to_thread(
                build_diff_from_env,
                self.env,
                self.synth_target.design,
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
