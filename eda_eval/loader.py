"""Discover the designs available for each supported benchmark."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from config import DesignConfig


def _detect_rtllm_reference_top(text: str, name: str) -> str | None:
    """Top module declared in an RTLLM `verified_<name>.v` reference.

    RTLLM golden files are inconsistent: some declare `module <name>`,
    others `module verified_<name>`. Try both in order and return whichever
    the file actually contains. Returns None if neither matches — the
    caller should skip designs without a detectable golden top, because
    ORFS will fail to elaborate them.
    """
    for candidate in (name, f"verified_{name}"):
        if re.search(rf"\bmodule\s+{re.escape(candidate)}\b", text):
            return candidate
    return None


class DesignLoader(ABC):
    """Base class for benchmark-specific design discovery."""

    benchmark: str

    @abstractmethod
    def designs(self) -> list[DesignConfig]:
        """All `DesignConfig`s this loader can find on disk."""


class RTLLMLoader(DesignLoader):
    """RTLLM benchmark.

    Each design folder lives under `<root>/<Category>/<Subcategory>/<name>/`
    and is identified by its `design_description.txt`. The human-written
    reference is the folder's `verified_*.v` file — globbed rather than
    constructed because the upstream naming is inconsistent (e.g.
    `adder_pipe_64bit/verified_adder_64bit.v`).

    Per-trial GPT outputs live at `<root>/_chatgpt35/t<N>/<name>.v` and
    `<root>/_chatgpt4/t<N>/<name>.v`. Trials that did not produce a file
    for a given design simply don't yield a variant; designs without a
    `verified_*.v` are skipped entirely (no reference -> no calibration).
    """

    benchmark = "rtllm"

    # Mapping of variant-name prefix to subdir under the RTLLM root holding
    # that prefix's per-trial output trees.
    TRIAL_GROUPS = (
        ("chatgpt35", "_chatgpt35"),
        ("chatgpt4", "_chatgpt4"),
    )

    def __init__(self, rtllm_root: Path):
        self.rtllm_root = rtllm_root

    def designs(self) -> list[DesignConfig]:
        if not self.rtllm_root.is_dir():
            return []
        configs: list[DesignConfig] = []
        for desc in sorted(self.rtllm_root.rglob("design_description.txt")):
            design_dir = desc.parent
            name = design_dir.name
            verified = sorted(design_dir.glob("verified_*.v"))
            if not verified:
                continue
            # Scan the concatenated reference for the golden top so designs
            # whose verified file declares `module verified_<name>` are
            # picked up correctly. Designs without a detectable top are
            # skipped — they'd fail elaboration in ORFS anyway, and skipping
            # the reference also drops every trial variant (no calibration
            # source).
            ref_text = "\n".join(v.read_text() for v in verified)
            top = _detect_rtllm_reference_top(ref_text, name)
            if top is None:
                continue
            configs.append(DesignConfig(
                benchmark=self.benchmark,
                name=name,
                variant="reference",
                rtl_files=tuple(verified),
                top_module=top,
            ))
            for prefix, subdir in self.TRIAL_GROUPS:
                trial_root = self.rtllm_root / subdir
                if not trial_root.is_dir():
                    continue
                for trial_dir in sorted(trial_root.glob("t*")):
                    if not trial_dir.is_dir():
                        continue
                    cand = trial_dir / f"{name}.v"
                    if not cand.is_file():
                        continue
                    # Trial outputs are written against the spec, so they
                    # declare `module <name>`. If a trial drifted from that
                    # convention ORFS will fail to elaborate it — same
                    # failure mode as before this field existed.
                    configs.append(DesignConfig(
                        benchmark=self.benchmark,
                        name=name,
                        variant=f"{prefix}_{trial_dir.name}",
                        rtl_files=(cand,),
                        top_module=name,
                    ))
        return configs


class CorpusLoader(DesignLoader):
    """Each subfolder under the corpus root is one design with one .v file.

    The single `.v` is the canonical human-written implementation, so it's
    tagged `variant="reference"` to satisfy the calibration contract that
    every (benchmark, name) group has a `reference` variant to calibrate
    against.
    """

    benchmark = "corpus"

    def __init__(self, corpus_dir: Path):
        self.corpus_dir = corpus_dir

    def designs(self) -> list[DesignConfig]:
        if not self.corpus_dir.is_dir():
            return []
        configs: list[DesignConfig] = []
        for child in sorted(self.corpus_dir.iterdir()):
            if not child.is_dir():
                continue
            rtl_files = tuple(sorted(child.glob("*.v")))
            if not rtl_files:
                continue
            configs.append(DesignConfig(
                benchmark=self.benchmark,
                name=child.name,
                variant="reference",
                rtl_files=rtl_files,
                top_module=child.name,
            ))
        return configs
