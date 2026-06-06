#!/usr/bin/env python3
"""Run the full ORFS flow per-iteration for each LLM-eval iterative artifact.

The iterative solver in `llm_eval` drives `ITERATIVE_ROUNDS = 4` rounds per
sample. The artifact dir only persists the *final* RTL diff, but the
companion `.eval` log records every `Edit`/`Write` tool call the agent
made. This script:

  1. For each `<run_dir>/<.eval>` log + companion `<design>/epoch_<n>/`
     dirs, reconstructs the RTL state at the end of each of the 4 rounds
     by replaying `Edit`/`Write` tool calls against the original RTL.
  2. For every iteration runs a `yosys` synthesisability check and the
     design's upstream testbench. Iterations where either fails are
     skipped (no PNR -- they wouldn't close anyway).
  3. For every surviving iteration drives the full ORFS flow against
     the patched RTL via `run_job`. Output lands at:
       `eda_results/<batch>__iterations/<run>/<benchmark>/<name>/
        <variant>_<epoch>/iter_<N>/`.

Usage:
    uv run eda_eval/iteration_sweep.py /workspace/artifacts/llm/<run> \
        --parallel-samples 4 --threads-per-run 4
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from inspect_ai.log import read_eval_log, read_eval_log_sample
from inspect_ai.model import ChatMessageAssistant, ChatMessageUser
from tqdm import tqdm

from common.config import (
    ALL_FLOW_TARGETS,
    ARTIFACTS,
    EDA_RUNS,
    DesignConfig,
    RunConfig,
    TargetConfig,
    _from_json,
)
from common.executor import run_parallel
from eda_eval.run import run_job
from llm_eval.components.scorers import evaluate_synthesis, evaluate_testbench

ARTIFACT_ROOT = ARTIFACTS / "llm"
NUM_ROUNDS = 4
ROUND_BOUNDARY_PREFIX = "# Previous round summary"
SANDBOX_RTL_ROOT = "rtl"  # mirrors llm_eval/components/claude_env.py
SANDBOX_USER_RE = re.compile(r"/home/claude-[0-9a-f]+")
BASH_TIMEOUT_S = 60
# Tokens that, when present in a Bash command, signal a file mutation we
# need to actually execute against the temp-dir mirror (otherwise the
# reconstructed RTL state will diverge from the agent's real state).
BASH_MUTATION_TOKENS = (
    "sed -i",
    "tee ",
    " > ",
    " >> ",
    "mv ",
    "cp ",
    "rm ",
    "patch ",
)


def _discover_runs(run_args: list[str]) -> list[Path]:
    """Resolve CLI args to artifact run dirs. With no args, take every
    subdir of `artifacts/llm/`."""
    if run_args:
        return [Path(r).resolve() for r in run_args]
    if not ARTIFACT_ROOT.is_dir():
        raise SystemExit(f"No artifact root at {ARTIFACT_ROOT}")
    return [p for p in sorted(ARTIFACT_ROOT.iterdir()) if p.is_dir()]


def _find_eval_log(run_dir: Path) -> Path:
    """Pick the single `.eval` log under `run_dir`."""
    logs = sorted(run_dir.glob("*.eval"))
    if not logs:
        raise FileNotFoundError(f"no .eval log in {run_dir}")
    if len(logs) > 1:
        raise RuntimeError(f"multiple .eval logs in {run_dir}: {logs}")
    return logs[0]


def _list_epoch_dirs(run_dir: Path) -> list[tuple[str, str, Path]]:
    """Return (design_name, epoch_name, epoch_dir) for every artifact
    epoch dir under `run_dir`."""
    out: list[tuple[str, str, Path]] = []
    for design_dir in sorted(run_dir.iterdir()):
        if not design_dir.is_dir():
            continue
        for epoch_dir in sorted(design_dir.iterdir()):
            if not epoch_dir.is_dir():
                continue
            if not (epoch_dir / "target_config.json").is_file():
                continue
            out.append((design_dir.name, epoch_dir.name, epoch_dir))
    return out


def _epoch_index(epoch_name: str) -> int:
    """`epoch_3` -> 3."""
    return int(epoch_name.split("_")[-1])


def _load_target(epoch_dir: Path) -> TargetConfig:
    blob = json.loads((epoch_dir / "target_config.json").read_text())
    return _from_json(TargetConfig, blob)


def _path_to_rel(file_path: str) -> str | None:
    """Map a sandbox absolute path `/home/<user>/rtl/<rel>` to `<rel>`."""
    if not file_path:
        return None
    parts = file_path.split("/")
    if SANDBOX_RTL_ROOT not in parts:
        return None
    idx = parts.index(SANDBOX_RTL_ROOT)
    rel = "/".join(parts[idx + 1 :])
    return rel or None


def _detect_sandbox_prefix(messages) -> str | None:
    """Find the `/home/claude-<hex>` user dir the sandbox used, by scanning
    file_path args in Edit/Write/Bash. The agent always references files
    inside this prefix."""
    for m in messages:
        if not (isinstance(m, ChatMessageAssistant) and m.tool_calls):
            continue
        for tc in m.tool_calls:
            args = tc.arguments or {}
            fp = args.get("file_path", "") if tc.function in ("Edit", "Write") else ""
            cmd = args.get("command", "") if tc.function == "Bash" else ""
            for source in (fp, cmd):
                hit = SANDBOX_USER_RE.search(source)
                if hit:
                    return hit.group(0)
    return None


def _command_could_mutate(cmd: str) -> bool:
    return any(tok in cmd for tok in BASH_MUTATION_TOKENS)


def _apply_tool_call(
    tc,
    work_root: Path,
    rtl_files: tuple[str, ...],
    sandbox_prefix: str | None,
    flagged_bash: list[str],
    read_files: set[str],
) -> None:
    """Mutate the temp-dir mirror under `work_root` per one tool call.
    `work_root` is the parent of the `rtl/` subdir (mirrors the agent's
    home dir). `read_files` accumulates rel-paths the agent has Read --
    Claude Code rejects Edit on a file the agent hasn't Read yet, so we
    mirror that gate to avoid applying Edits the real run rejected."""
    fn = tc.function
    args = tc.arguments or {}
    if fn == "Read":
        rel = _path_to_rel(args.get("file_path", ""))
        if rel is not None:
            read_files.add(rel)
    elif fn == "Edit":
        rel = _path_to_rel(args.get("file_path", ""))
        if rel is None or rel not in rtl_files:
            return
        if rel not in read_files:
            # Claude Code: "File has not been read yet. Read it first."
            return
        p = work_root / SANDBOX_RTL_ROOT / rel
        if not p.is_file():
            return
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        ra = args.get("replace_all", False)
        if isinstance(ra, str):
            ra = ra.lower() == "true"
        cur = p.read_text()
        if ra:
            p.write_text(cur.replace(old, new))
        elif cur.count(old) == 1:
            # Edit's contract: fails unless old_string appears exactly once.
            p.write_text(cur.replace(old, new, 1))
    elif fn == "Write":
        rel = _path_to_rel(args.get("file_path", ""))
        if rel is None or rel not in rtl_files:
            return
        p = work_root / SANDBOX_RTL_ROOT / rel
        if not p.parent.is_dir():
            return
        p.write_text(args.get("content", ""))
        # Write also satisfies Claude's "must Read before edit" gate for
        # subsequent Edits on the same file.
        read_files.add(rel)
    elif fn == "Bash":
        cmd = args.get("command", "") or ""
        if not _command_could_mutate(cmd):
            return
        # Rewrite the sandbox absolute prefix to point at our temp mirror,
        # then run with cwd = work_root so relative paths like `rtl/foo.v`
        # resolve too.
        rewritten = cmd
        if sandbox_prefix:
            rewritten = cmd.replace(sandbox_prefix, str(work_root))
        try:
            subprocess.run(
                rewritten,
                shell=True,
                cwd=work_root,
                capture_output=True,
                text=True,
                timeout=BASH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            flagged_bash.append(f"timed out: {cmd[:200]}")
        except Exception as e:  # noqa: BLE001
            flagged_bash.append(f"{type(e).__name__}: {cmd[:200]}")


def _round_starts(messages) -> list[int]:
    """Indices where each round starts. Round 1 starts at 0; rounds 2-N
    start at each "# Previous round summary" user message."""
    starts = [0]
    for i, m in enumerate(messages):
        if isinstance(m, ChatMessageUser) and m.text.startswith(ROUND_BOUNDARY_PREFIX):
            starts.append(i)
    return starts


def _build_diff(originals: dict[str, str], current: dict[str, str]) -> str:
    """Unified diff `originals` -> `current`, in the `a/<rel>` `b/<rel>`
    layout `eda_eval/artifact_sweep.py` expects."""
    parts: list[str] = []
    for rel, old in originals.items():
        new = current[rel]
        d = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        parts.append("".join(d))
    return "".join(parts)


def _reconstruct_iteration_diffs(
    design: DesignConfig, messages
) -> tuple[list[str], list[str]]:
    """For each round in `messages`, return the diff vs the original RTL.

    Maintains a temp-dir mirror of the agent's `rtl/` tree so that Bash
    commands (e.g. `sed -i`) can be executed against it for accurate
    state. Edit/Write calls are applied to the same files."""
    originals = {
        str(rel): abs_p.read_text()
        for rel, abs_p in zip(design.rtl_files, design.rtl_abs_paths)
    }
    rtl_files = tuple(originals)
    sandbox_prefix = _detect_sandbox_prefix(messages)
    base_tmp = Path(tempfile.mkdtemp(prefix="iter_replay_"))
    try:
        work_root = base_tmp / "work"
        rtl_dst = work_root / SANDBOX_RTL_ROOT
        rtl_dst.mkdir(parents=True)
        for rel, abs_p in zip(design.rtl_files, design.rtl_abs_paths):
            dst = rtl_dst / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(abs_p, dst)

        starts = _round_starts(messages)
        bounds = starts + [len(messages)]
        diffs: list[str] = []
        flagged_bash: list[str] = []
        # `claude -p --resume` carries Read-before-Edit state across rounds in
        # almost all cases, so we track Reads cumulatively across the whole
        # session. The rare cross-round reset (one observed in jpeg_encoder/
        # epoch_3 round 3) is flagged by the per-edit diagnostic instead.
        read_files: set[str] = set()
        for k in range(len(starts)):
            for i in range(bounds[k], bounds[k + 1]):
                m = messages[i]
                if isinstance(m, ChatMessageAssistant) and m.tool_calls:
                    for tc in m.tool_calls:
                        _apply_tool_call(
                            tc,
                            work_root,
                            rtl_files,
                            sandbox_prefix,
                            flagged_bash,
                            read_files,
                        )
            current = {rel: (rtl_dst / rel).read_text() for rel in rtl_files}
            diffs.append(_build_diff(originals, current))
        return diffs, flagged_bash
    finally:
        shutil.rmtree(base_tmp, ignore_errors=True)


def _verify_final_diff(reconstructed: str, epoch_dir: Path, tag: str) -> str | None:
    """Sanity-check: the round-4 reconstruction should match the persisted
    `diff.patch` in the artifact dir. Returns a warning string on
    mismatch, else None."""
    artifact = epoch_dir / "diff.patch"
    if not artifact.is_file():
        return None
    ground_truth = artifact.read_text()
    if reconstructed.strip() == ground_truth.strip():
        return None
    return (
        f"WARN {tag}: reconstructed final diff disagrees with "
        f"{artifact} (likely missed a Bash file mutation)"
    )


def _create_patched_copy(design: DesignConfig, diff: str) -> Path:
    """Reflink-copy `design.root`, apply `diff` under `rtl_dir`."""
    dest = Path(tempfile.mkdtemp(prefix="iter_root_")) / design.root.name
    proc = subprocess.run(
        ["cp", "-R", "--reflink=auto", str(design.root), str(dest)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"copy failed: {proc.stderr}")
    if diff.strip():
        rtl_dest = dest / design.rtl_dir
        patch = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(rtl_dest)],
            input=diff,
            text=True,
            capture_output=True,
        )
        if patch.returncode != 0:
            raise RuntimeError(
                f"patch failed:\nSTDOUT: {patch.stdout}\nSTDERR: {patch.stderr}"
            )
    return dest


def _run_one(
    target: TargetConfig,
    diff: str,
    output_dir_str: str,
    num_threads: int,
) -> tuple[str, str, str]:
    """For one (sample, iteration), persist the diff, run synth+TB gates,
    and (if both pass) run the full ORFS flow. Returns (status, msg, log_tail)."""
    output_dir = Path(output_dir_str)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "diff.patch").write_text(diff)
    try:
        target.dump(output_dir / TargetConfig.FILENAME)
        design = target.design

        synth_log, synth_rc = evaluate_synthesis(design, diff)
        (output_dir / "synthesis.log").write_text(synth_log)
        if synth_rc != 0:
            return "SKIP_SYNTH", str(output_dir), synth_log[-1000:]

        tb_log, tb_rc = evaluate_testbench(design, diff)
        (output_dir / "testbench.log").write_text(tb_log)
        if tb_rc != 0:
            return "SKIP_TB", str(output_dir), tb_log[-1000:]

        copy_root = _create_patched_copy(design, diff)
        patched_design = dataclasses.replace(design, root=copy_root)
        patched_target = dataclasses.replace(target, design=patched_design)
        run = RunConfig(
            synth_target=patched_target,
            output_dir=output_dir,
            flow_targets=ALL_FLOW_TARGETS,
            num_threads=num_threads,
        )
        rc = run_job(run)
        if rc == 0:
            return "PASS", str(output_dir), ""
        return f"FAIL_PNR_rc{rc}", str(output_dir), ""
    except Exception as e:
        tb = traceback.format_exc()
        return "ERROR", str(output_dir), f"{type(e).__name__}: {e}\n{tb}"


def _samples_from_log(log_path: Path) -> list[tuple[str, int]]:
    """Enumerate (sample_id, epoch) pairs present in the .eval log."""
    log = read_eval_log(str(log_path), header_only=False)
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for s in log.samples or []:
        key = (str(s.id), int(s.epoch))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _plan_jobs_for_run(
    run_dir: Path,
    batch_dir: Path,
    sample_filter: set[tuple[str, int]] | None = None,
) -> tuple[list[tuple[tuple, tuple]], list[str]]:
    """Build the `run_parallel` job list for one `<run_dir>`. If
    `sample_filter` is provided, only build jobs for `(sample_id, epoch)`
    pairs in it. Returns (jobs, warnings)."""
    log_path = _find_eval_log(run_dir)
    epoch_dirs = {
        (design, _epoch_index(epoch)): ep_dir
        for design, epoch, ep_dir in _list_epoch_dirs(run_dir)
    }
    run_name = run_dir.name
    jobs: list[tuple[tuple, tuple]] = []
    warnings: list[str] = []
    sample_keys = _samples_from_log(log_path)
    for design_name, epoch in sample_keys:
        if sample_filter is not None and (design_name, epoch) not in sample_filter:
            continue
        ep_dir = epoch_dirs.get((design_name, epoch))
        if ep_dir is None:
            warnings.append(
                f"{run_name}/{design_name} epoch={epoch}: no artifact dir, skipping"
            )
            continue
        target = _load_target(ep_dir)
        sample = read_eval_log_sample(str(log_path), id=design_name, epoch=epoch)
        diffs, bash_warnings = _reconstruct_iteration_diffs(
            target.design, sample.messages
        )
        tag = f"{run_name}/{design_name}/epoch_{epoch}"
        if bash_warnings:
            warnings.append(
                f"{tag}: ignored {len(bash_warnings)} bash command(s) that may "
                f"have mutated RTL: {bash_warnings[0]!r}..."
            )
        if len(diffs) < NUM_ROUNDS:
            warnings.append(
                f"{tag}: reconstructed only {len(diffs)}/{NUM_ROUNDS} round(s) "
                f"(sample likely timed out or hit max-turns early)"
            )
        # If the reconstructed final-iter diff diverges from the artifact's
        # persisted `diff.patch`, prefer the artifact: it's the agent's
        # ground-truth final RTL and is strictly more reliable than any
        # replay. Earlier-iter reconstructions are kept (the diagnostic
        # confirmed disagreements only manifest at the final iter for
        # affected samples).
        if diffs:
            mismatch = _verify_final_diff(diffs[-1], ep_dir, tag)
            if mismatch:
                warnings.append(mismatch)
                artifact_diff = (ep_dir / "diff.patch").read_text()
                diffs[-1] = artifact_diff
                warnings.append(
                    f"{tag}: substituted artifact diff.patch for final iter "
                    f"({len(artifact_diff)} chars) to bypass replay divergence"
                )
        design = target.design
        for iter_idx, diff in enumerate(diffs, start=1):
            output_dir = (
                batch_dir
                / run_name
                / design.benchmark
                / design.name
                / f"{design.variant}_epoch_{epoch}"
                / f"iter_{iter_idx}"
            )
            key = (run_name, design_name, epoch, iter_idx)
            jobs.append((key, (target, diff, str(output_dir))))
    return jobs, warnings


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "run_dirs",
        nargs="*",
        help="Artifact run dir(s) under artifacts/llm/ (default: every subdir).",
    )
    parser.add_argument("--parallel-samples", type=int, default=4)
    parser.add_argument("--threads-per-run", type=int, default=4)
    parser.add_argument(
        "--sample",
        action="append",
        default=[],
        help="Filter to specific samples, format '<sample_id>:<epoch>'. May "
        "be passed multiple times. Default: all samples in each run dir.",
    )
    args = parser.parse_args()

    sample_filter: set[tuple[str, int]] | None = None
    if args.sample:
        sample_filter = set()
        for spec in args.sample:
            sid, _, ep = spec.partition(":")
            if not sid or not ep:
                raise SystemExit(f"Bad --sample value {spec!r}; expected sid:epoch")
            sample_filter.add((sid, int(ep)))

    run_dirs = _discover_runs(args.run_dirs)

    batch_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    batch_dir = EDA_RUNS / f"{batch_ts}__iterations"
    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / "sweep.log"
    print(f"Output dir: {batch_dir}")
    print(f"Log: {log_path}")
    print(f"Runs to process: {[r.name for r in run_dirs]}")

    all_jobs: list[tuple[tuple, tuple]] = []
    all_warnings: list[str] = []
    for rd in run_dirs:
        try:
            jobs, warnings = _plan_jobs_for_run(rd, batch_dir, sample_filter)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR planning {rd}: {e}", file=sys.stderr)
            traceback.print_exc()
            continue
        all_jobs.extend(jobs)
        all_warnings.extend(warnings)

    if not all_jobs:
        raise SystemExit("No jobs planned")

    # Append threads-per-run to each job's positional args.
    submitted = [(key, (*job_args, args.threads_per_run)) for key, job_args in all_jobs]

    with log_path.open("w", buffering=1) as log:
        log.write(
            f"# iteration sweep started {batch_ts} "
            f"parallel={args.parallel_samples} threads={args.threads_per_run}\n"
        )
        log.write(f"# {len(all_jobs)} jobs from {len(run_dirs)} run(s)\n")
        for w in all_warnings:
            log.write(f"# {w}\n")
            tqdm.write(w)
        results_by_status: dict[str, int] = {}
        for (run_name, design_name, epoch, iter_idx), (
            status,
            out_dir,
            msg,
        ) in run_parallel(
            _run_one,
            submitted,
            max_workers=args.parallel_samples,
            description="iteration sweep",
        ):
            ts = time.strftime("%H:%M:%S")
            tag = f"{run_name}/{design_name}/epoch_{epoch}/iter_{iter_idx}"
            log.write(f"[{ts}] {status} {tag} -> {out_dir}\n")
            if msg:
                for line in msg.splitlines():
                    log.write(f"    {line}\n")
            results_by_status[status] = results_by_status.get(status, 0) + 1
            tqdm.write(f"{status} {tag}")
        log.write(f"# done. results: {results_by_status}\n")

    print(f"\nResults: {results_by_status}")
    if any(s not in ("PASS", "SKIP_SYNTH", "SKIP_TB") for s in results_by_status):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
