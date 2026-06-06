#!/usr/bin/env python3
"""Categorise each of the 4 round-level edits per (design, epoch) artifact.

Reconstructs per-round cumulative diffs via iteration_sweep helpers, computes
the incremental diff for each round (vs. the previous round), then asks
Claude Opus 4.8 to bucket each into one of the categories from
llm_eval/prompts/generator/categorise.md. Writes the chosen label to
`edit<n>_category.txt` next to `diff.patch` in the artifact dir.

Usage:
    uv run python analysis/categorise_edits.py [--parallel N] [--force]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import subprocess
import sys
import traceback
from pathlib import Path

from inspect_ai.log import read_eval_log_sample

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from eda_eval.iteration_sweep import (  # noqa: E402
    NUM_ROUNDS,
    _epoch_index,
    _find_eval_log,
    _list_epoch_dirs,
    _load_target,
    _reconstruct_iteration_diffs,
    _verify_final_diff,
)

ARTIFACT_ROOT = REPO / "artifacts" / "llm"
PROMPT_PATH = REPO / "llm_eval" / "prompts" / "generator" / "categorise.md"
MODEL = "claude-opus-4-8"

# The canonical labels the prompt asks Opus to choose from. Used to normalise
# / validate the model's response so the saved files stay machine-parseable.
# The prompt itself handles the empty-diff case via the "None" category.
CATEGORIES = (
    "Logic simplification",
    "Combinational restructuring",
    "Parallelisation",
    "Pipeline insertion",
    "Fanout reduction",
    "Mixed",
    "None",
    "Other",
)


def _apply_unified_diff(originals: dict[str, str], diff_text: str) -> dict[str, str]:
    """Apply a unified diff (as produced by `_build_diff`) to `originals`
    and return the resulting file contents. Pure-python so we don't need
    a temp dir per round."""
    state = dict(originals)
    if not diff_text.strip():
        return state

    # Split into per-file hunks. The `_build_diff` helper emits one chunk
    # per original file, each starting with `--- a/<rel>` / `+++ b/<rel>`.
    lines = diff_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- a/"):
            i += 1
            continue
        rel_a = lines[i][len("--- a/") :].rstrip()
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ b/"):
            continue
        i += 1
        # Collect this file's hunks until the next `--- ` header.
        hunk_lines: list[str] = []
        while i < len(lines) and not lines[i].startswith("--- a/"):
            hunk_lines.append(lines[i])
            i += 1
        if rel_a not in state:
            continue
        state[rel_a] = _apply_file_hunks(state[rel_a], hunk_lines)
    return state


def _apply_file_hunks(original: str, hunk_lines: list[str]) -> str:
    """Apply unified-diff hunks to `original`. Mirrors `patch -p1` semantics
    for hunks produced by `difflib.unified_diff`."""
    original_lines = original.splitlines(keepends=True)
    out: list[str] = []
    src_idx = 0
    i = 0
    while i < len(hunk_lines):
        line = hunk_lines[i]
        if not line.startswith("@@"):
            i += 1
            continue
        # Parse `@@ -a,b +c,d @@`. We only need `a` (1-based start).
        header = line.split("@@")[1].strip()
        minus = header.split()[0]
        start = int(minus.split(",")[0].lstrip("-")) - 1
        # Copy any unmodified lines up to the hunk start.
        if start < src_idx:
            raise ValueError(f"hunk start {start} < cursor {src_idx}")
        out.extend(original_lines[src_idx:start])
        src_idx = start
        i += 1
        while i < len(hunk_lines) and not hunk_lines[i].startswith("@@"):
            h = hunk_lines[i]
            if h.startswith("---") or h.startswith("+++"):
                i += 1
                continue
            if h.startswith(" "):
                out.append(original_lines[src_idx])
                src_idx += 1
            elif h.startswith("-"):
                src_idx += 1
            elif h.startswith("+"):
                out.append(h[1:])
            elif h == "\n" or h == "":
                # Some diffs omit the leading space on empty context lines.
                if src_idx < len(original_lines):
                    out.append(original_lines[src_idx])
                    src_idx += 1
            i += 1
    out.extend(original_lines[src_idx:])
    return "".join(out)


def _build_unified_diff(prev: dict[str, str], cur: dict[str, str]) -> str:
    """Unified diff prev -> cur, same `a/<rel>` `b/<rel>` layout as
    `_build_diff` in iteration_sweep."""
    parts: list[str] = []
    for rel in prev:
        old = prev[rel]
        new = cur[rel]
        if old == new:
            continue
        d = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        parts.append("".join(d))
    return "".join(parts)


def _round_states_from_cumulative(
    originals: dict[str, str], cumulative: list[str]
) -> list[dict[str, str]]:
    """Apply each cumulative diff to `originals` -> per-round file states."""
    return [_apply_unified_diff(originals, d) for d in cumulative]


def _categorise(diff_text: str, prompt_template: str) -> str:
    """Invoke claude -p Opus 4.8 with the categorise prompt + diff. Returns
    the raw model output (single category name expected)."""
    prompt = prompt_template.replace("<diff>", diff_text)
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model",
            MODEL,
            "--output-format",
            "text",
            "--permission-mode",
            "bypassPermissions",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}: "
            f"stdout={result.stdout[-2000:]!r} stderr={result.stderr[-2000:]!r}"
        )
    return result.stdout.strip()


def _normalise(label: str) -> str:
    """Pick the canonical category whose name is contained in `label`
    (case-insensitive). Falls back to `Other` if no match."""
    low = label.lower().strip()
    # Exact match first (after stripping punctuation).
    cleaned = low.strip(".:*-_ \t\n").splitlines()[0] if low else ""
    cleaned = cleaned.strip(".:*-_ \t\n")
    for cat in CATEGORIES:
        if cleaned == cat.lower():
            return cat
    # Substring fallback.
    for cat in CATEGORIES:
        if cat.lower() in low:
            return cat
    return "Other"


def _process_epoch(
    epoch_dir: Path,
    design_name: str,
    epoch: int,
    cumulative_diffs: list[str],
    originals: dict[str, str],
    prompt_template: str,
    force: bool,
) -> tuple[str, list[str], list[str]]:
    """Categorise all 4 rounds for one epoch. Returns (tag, labels, warnings)."""
    tag = f"{epoch_dir.parent.parent.name}/{design_name}/epoch_{epoch}"
    warnings: list[str] = []
    labels: list[str] = []
    if len(cumulative_diffs) < NUM_ROUNDS:
        warnings.append(
            f"{tag}: only {len(cumulative_diffs)}/{NUM_ROUNDS} reconstructed rounds"
        )
    # Reconstruct per-round file states then take incremental diffs.
    states = _round_states_from_cumulative(originals, cumulative_diffs)
    prev_state = originals
    for n in range(1, NUM_ROUNDS + 1):
        out_path = epoch_dir / f"edit{n}_category.txt"
        if out_path.is_file() and not force:
            labels.append(out_path.read_text().strip())
            if n - 1 < len(states):
                prev_state = states[n - 1]
            continue
        if n - 1 >= len(states):
            out_path.write_text("MISSING\n")
            labels.append("MISSING")
            continue
        incremental = _build_unified_diff(prev_state, states[n - 1])
        try:
            raw = _categorise(incremental, prompt_template)
            label = _normalise(raw)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{tag} round {n}: categorise failed: {e}")
            label = "ERROR"
        out_path.write_text(label + "\n")
        labels.append(label)
        prev_state = states[n - 1]
    return tag, labels, warnings


def _gather_jobs(
    run_dir: Path,
) -> list[tuple[Path, str, int, list[str], dict[str, str]]]:
    """For one run dir, return (epoch_dir, design_name, epoch, cumulative_diffs,
    originals) tuples for every epoch."""
    log_path = _find_eval_log(run_dir)
    epoch_dirs = {
        (design, _epoch_index(epoch)): ep_dir
        for design, epoch, ep_dir in _list_epoch_dirs(run_dir)
    }
    out: list[tuple[Path, str, int, list[str], dict[str, str]]] = []
    for (design_name, epoch), ep_dir in epoch_dirs.items():
        target = _load_target(ep_dir)
        sample = read_eval_log_sample(str(log_path), id=design_name, epoch=epoch)
        diffs, _bash_warnings = _reconstruct_iteration_diffs(
            target.design, sample.messages
        )
        # Use the persisted final diff.patch if reconstruction disagrees --
        # mirrors what iteration_sweep does.
        if diffs:
            mismatch = _verify_final_diff(diffs[-1], ep_dir, f"{design_name}/{epoch}")
            if mismatch:
                diffs[-1] = (ep_dir / "diff.patch").read_text()
        originals = {
            str(rel): abs_p.read_text()
            for rel, abs_p in zip(target.design.rtl_files, target.design.rtl_abs_paths)
        }
        out.append((ep_dir, design_name, epoch, diffs, originals))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing edit<n>_category.txt files.",
    )
    ap.add_argument(
        "--run",
        action="append",
        default=None,
        help="Only process this artifact run dir (basename).",
    )
    args = ap.parse_args()

    prompt_template = PROMPT_PATH.read_text()

    run_dirs = [p for p in sorted(ARTIFACT_ROOT.iterdir()) if p.is_dir()]
    if args.run:
        run_dirs = [p for p in run_dirs if p.name in set(args.run)]

    all_jobs: list[tuple] = []
    for run_dir in run_dirs:
        try:
            jobs = _gather_jobs(run_dir)
        except Exception as e:  # noqa: BLE001
            print(
                f"[WARN] {run_dir.name}: {e}\n{traceback.format_exc()}", file=sys.stderr
            )
            continue
        all_jobs.extend(jobs)
        print(f"[plan] {run_dir.name}: {len(jobs)} epochs")

    print(
        f"[plan] total epochs: {len(all_jobs)} "
        f"(expect 4 round labels each => {4 * len(all_jobs)} categorisations)"
    )

    results: list[tuple[str, list[str]]] = []
    warnings: list[str] = []

    def worker(job):
        ep_dir, design_name, epoch, diffs, originals = job
        return _process_epoch(
            ep_dir,
            design_name,
            epoch,
            diffs,
            originals,
            prompt_template,
            args.force,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(worker, j) for j in all_jobs]
        for fut in concurrent.futures.as_completed(futures):
            try:
                tag, labels, ws = fut.result()
            except Exception as e:  # noqa: BLE001
                print(
                    f"[ERROR] worker failed: {e}\n{traceback.format_exc()}",
                    file=sys.stderr,
                )
                continue
            results.append((tag, labels))
            warnings.extend(ws)
            print(f"[done] {tag}: {labels}")

    print()
    print(f"=== SUMMARY ({len(results)} epochs processed) ===")
    from collections import Counter

    totals: Counter = Counter()
    for _tag, labels in results:
        totals.update(labels)
    print(f"Totals: {dict(totals)}")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
