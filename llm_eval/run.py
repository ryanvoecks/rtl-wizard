"""Run llm_eval's task via the inspect_ai Python API.

Owns the per-run output dir AND the shared sandbox container's lifecycle:
the container is built once (first run only -- subsequent runs just restart
the existing container so the OAUTH login is preserved) and stopped at the
end. Every sample's ClaudeEnv attaches to the same running container (which
keeps one OAUTH Claude Code session across samples) and mounts onto the same
eval-wide MCP host (so `sse_starlette`'s process-global shutdown signal
never fires between samples). To force a rebuild + fresh OAUTH login, call
`Container.teardown()` explicitly.

    uv run python llm_eval/run.py
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from components.container import Container
from components.solvers import claude_code_iterative_solver
from components.tasks import optimize_timing
from inspect_ai import Task, eval_async, eval_retry_async
from inspect_ai.log import read_eval_log

from common.config import LLM_RESULTS

# Inspect requires an API key (working or not) in environment, so set a fake one
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

# Eval config
# PNR-feedback runs: the iterative solver re-runs the FULL ORFS P&R flow between
# rounds (heavy), so parallelism is capped at 2 to stay within this host's RAM.
MAX_PARALLEL_SESSIONS = 2
MODEL = "anthropic/claude-sonnet-4-6"
EPOCHS = 1
SOLVER = claude_code_iterative_solver

# Three .eval runs, 4 designs each (12 designs total). Same groupings as the
# prior synth_/predict_ runs so the PNR results line up for comparison.
GROUPS: list[tuple[str, list[str]]] = [
    (
        "pnr_aes_sha512_doublefpu_bitonicsorter_n_1_d_4",
        ["aes", "sha512", "double_fpu", "bitonic_sorter"],
    ),
    (
        "pnr_e203_reedsolomon_systolictpu_viterbi_n_1_d_4",
        ["e203", "reed_solomon", "systolic_tpu", "viterbi"],
    ),
    (
        "pnr_verilogaxi_uberddr3_wbdma_jpegencoder_n_1_d_4",
        ["verilog_axi", "uberddr3", "wb_dma", "jpeg_encoder"],
    ),
]


async def run(run_dir: Path, task: Task, **kwargs: Any) -> None:
    """Start, retry, or skip based on any existing .eval log in `run_dir`."""
    existing = sorted(run_dir.glob("*.eval"))
    if not existing:
        await eval_async(task, log_dir=str(run_dir), fail_on_error=False, **kwargs)
        return
    log = read_eval_log(str(existing[-1]), header_only=True)
    r = log.results
    all_done = r is not None and r.completed_samples == r.total_samples
    if log.status == "success" and all_done:
        print(f"Skipping (success): {existing[-1]}")
        return
    done = f"{r.completed_samples}/{r.total_samples}" if r is not None else "?"
    print(f"Retrying ({log.status}, {done}): {existing[-1]}")
    await eval_retry_async(str(existing[-1]), log_dir=str(run_dir), fail_on_error=False)


async def main_async() -> None:
    """We need to run this async to allow shared MCPService __aenter__ and __aexit__"""
    # One shared container for all groups (keeps the single OAUTH session alive).
    async with Container():
        for dirname, sample_ids in GROUPS:
            run_dir = LLM_RESULTS / dirname
            run_dir.mkdir(parents=True, exist_ok=True)
            print(f"Output dir: {run_dir}", flush=True)
            await run(
                run_dir,
                optimize_timing(str(run_dir), SOLVER()),
                model=MODEL,
                max_samples=MAX_PARALLEL_SESSIONS,
                epochs=EPOCHS,
                sample_id=sample_ids,
            )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
