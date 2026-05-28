"""Parallel job runner with a progress bar.

Wraps `ProcessPoolExecutor` + `as_completed` + `tqdm` so callers can
submit a list of `(key, args)` pairs and stream `(key, result)` back as
each job finishes. The key is opaque -- it's whatever the caller wants
to attach to a job for downstream attribution (a tuple, an id, the
inputs themselves). Per-job logging is left to the caller; this module
only handles execution + progress.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import TypeVar

from tqdm import tqdm

K = TypeVar("K")
R = TypeVar("R")


def run_parallel(
    fn: Callable[..., R],
    jobs: Iterable[tuple[K, tuple]],
    max_workers: int,
    description: str = "jobs",
) -> Iterator[tuple[K, R]]:
    """Run `fn(*args)` for each `(key, args)` pair on a process pool
    and yield `(key, result)` as completions arrive. A tqdm bar advances
    once per finished job."""
    pending = list(jobs)
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fn, *args): key for key, args in pending}
        with tqdm(total=len(futures), desc=description) as bar:
            for fut in as_completed(futures):
                key = futures[fut]
                result = fut.result()
                bar.update(1)
                yield key, result
