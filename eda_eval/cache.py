"""Content-addressed cache for ORFS synth runs.

Hashes the cache-relevant subset of a `RunConfig` (study knobs, period,
side, top module, clock port, RTL contents, flow targets) and indexes
prior outputs by that hash in a single `eda_results/.cache.json` file so
re-runs with identical inputs can skip the flow and copy the cached
output directory.

RTL hashing normalises whitespace and strips comments so cosmetic edits
do not bust the cache. Preprocessor directives are preserved.

Concurrent writers are serialised via `fcntl.flock` on a sibling
`.cache.json.lock` file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from common.config import EDA_RUNS, DesignConfig, RunConfig

CACHE_INDEX = EDA_RUNS / ".cache.json"
RTL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}

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
        "side_um": target.side_um,
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


def load_index() -> dict[str, str]:
    if not CACHE_INDEX.is_file():
        return {}
    try:
        data = json.loads(CACHE_INDEX.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_index(index: dict[str, str]) -> None:
    CACHE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, sort_keys=True, indent=2))
    os.replace(tmp, CACHE_INDEX)


@contextmanager
def _locked() -> Iterator[None]:
    """Hold an exclusive flock on `.cache.json.lock` for the duration of
    a read-modify-write on the cache index."""
    CACHE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE_INDEX.with_suffix(".json.lock")
    with lock_path.open("w") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def lookup(run: RunConfig) -> Path | None:
    """Return the cached output dir for `run`, or None on miss.
    Prunes stale entries (target dir or run_config.json missing)."""
    h = compute_run_hash(run)
    with _locked():
        index = load_index()
        rel = index.get(h)
        if rel is None:
            return None
        cached = EDA_RUNS / rel
        if cached.is_dir() and (cached / RunConfig.FILENAME).is_file():
            return cached
        del index[h]
        save_index(index)
        return None


def record(run: RunConfig) -> None:
    """Insert `run.output_dir` into the cache index under `run`'s hash."""
    h = compute_run_hash(run)
    rel = run.output_dir.resolve().relative_to(EDA_RUNS.resolve()).as_posix()
    with _locked():
        index = load_index()
        index[h] = rel
        save_index(index)


def replay(src: Path, dst: Path) -> None:
    """Populate `dst` from cached `src` via `cp -R --reflink=auto`."""
    dst.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cp", "-R", "--reflink=auto", f"{src}/.", str(dst)],
        check=True,
    )
