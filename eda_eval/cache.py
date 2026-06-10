"""Content-addressed cache for ORFS synth runs.

Indexes flow outputs by a hash of the cache-relevant `RunConfig` fields
(study knobs, period, side, top module, clock port, RTL contents, flow
targets), so re-runs with identical inputs share a single materialised
directory under `eda_results/_cache/<hash>/`. RTL hashing is comment-
and whitespace-insensitive; preprocessor directives are preserved.

`lookup` returns the cache entry for a run if present. `publish`
inserts a freshly completed run, deduplicating against concurrent
writers. `link` points an arbitrary destination at a cache entry so
artifacts are reachable from both locations without copying.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from common.config import EDA_RUNS, DesignConfig, RunConfig

EDA_CACHE = EDA_RUNS / "_cache"
RTL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
EXIT_CODE_FILE = "exit_code"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_WHITESPACE = re.compile(r"\s+")


def _normalize_rtl(text: str) -> str:
    """Strip comments and collapse whitespace."""
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _LINE_COMMENT.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def _hash_rtl(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha256(_normalize_rtl(raw).encode("utf-8")).hexdigest()


def _enumerate_include_files(design: DesignConfig) -> list[tuple[str, Path]]:
    """For each include_dir, glob RTL sources beneath it."""
    rtl_src = design.root / design.rtl_dir
    out: list[tuple[str, Path]] = []
    for i, d in enumerate(design.include_dirs):
        resolved = rtl_src / d
        if not resolved.is_dir():
            continue
        files = [
            p for p in resolved.rglob("*") if p.is_file() and p.suffix in RTL_SUFFIXES
        ]
        files.sort(key=lambda p: p.relative_to(resolved).as_posix())
        for p in files:
            rel = f"{i}/{p.relative_to(resolved).as_posix()}"
            out.append((rel, p))
    return out


def compute_run_hash(run: RunConfig) -> str:
    """Deterministic sha256 over the cache-relevant subset of `run`."""
    target = run.synth_target
    design = target.design
    payload = {
        "study": asdict(target.cfg),
        "period_ns": target.period_ns,
        "target_utilization": target.target_utilization,
        "top_module": design.top_module,
        "clock_port": design.clock_port,
        "rtl_files": [
            {"rel": str(rel), "hash": _hash_rtl(abs_p)}
            for rel, abs_p in zip(design.rtl_files, design.rtl_abs_paths)
        ],
        "include_files": [
            {"rel": rel, "hash": _hash_rtl(abs_p)}
            for rel, abs_p in _enumerate_include_files(design)
        ],
        "flow_targets": list(run.flow_targets),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_path(run: RunConfig) -> Path:
    """Canonical cache directory for `run` (may not yet exist)."""
    return EDA_CACHE / compute_run_hash(run)


@contextmanager
def _locked() -> Iterator[None]:
    """Hold an exclusive lock on `_cache/.lock` so publishing it atomic."""
    EDA_CACHE.mkdir(parents=True, exist_ok=True)
    lock_path = EDA_CACHE / ".lock"
    with lock_path.open("w") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def lookup(run: RunConfig) -> Path | None:
    """Return the cached directory for a successful `run`, or None on
    miss. Failed runs (non-zero `exit_code` file) are treated as misses
    so the caller re-runs the flow; legacy entries without the file are
    accepted on the assumption they predate the marker."""
    target = cache_path(run)
    if not (target.is_dir() and (target / RunConfig.FILENAME).is_file()):
        return None
    rc_file = target / EXIT_CODE_FILE
    if rc_file.is_file() and rc_file.read_text().strip() != "0":
        return None
    return target


def publish(work_dir: Path, run: RunConfig) -> Path:
    """Promote `work_dir` to its canonical cache slot."""
    target = cache_path(run)
    with _locked():
        if target.is_dir() and (target / RunConfig.FILENAME).is_file():
            shutil.rmtree(work_dir, ignore_errors=True)
            return target
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(work_dir, target)
        return target


def link(dst: Path, src: Path) -> None:
    """Symlink `dst` -> `src`."""
    if dst.resolve() == src.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    dst.symlink_to(src)
