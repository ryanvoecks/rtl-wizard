"""Discover the designs available for each supported benchmark."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

import pyslang

from config import DesignConfig

# benchmark -> name -> variant -> DesignConfig.
DesignTree = dict[str, dict[str, dict[str, DesignConfig]]]


def detect_top_module(rtl_files: Iterable[Path]) -> str:
    """Top module name across one or more Verilog/SystemVerilog files."""

    files = list(rtl_files)
    comp = pyslang.Compilation()
    for path in files:
        comp.addSyntaxTree(pyslang.SyntaxTree.fromFile(str(path)))
    tops = [t.name for t in comp.getRoot().topInstances]
    if len(tops) != 1:
        raise ValueError(
            f"expected exactly one top module across "
            f"{[str(p) for p in files]}, got {len(tops)}: {tops}"
        )
    return tops[0]


class DesignLoader(ABC):
    """Base class for benchmark-specific design discovery."""

    benchmark: str

    @abstractmethod
    def designs(self) -> DesignTree:
        """All `DesignConfig`s this loader can find on disk, nested as
        `{benchmark: {name: {variant: design}}}`."""


class RTLLMLoader(DesignLoader):
    """Get designs from RTLLM benchmark."""

    benchmark = "rtllm"

    # Mapping of variant-name prefix to subdir
    TRIAL_GROUPS = (
        ("chatgpt35", "_chatgpt35"),
        ("chatgpt4", "_chatgpt4"),
    )

    def __init__(self, rtllm_root: Path):
        self.rtllm_root = rtllm_root

    def designs(self) -> DesignTree:
        names: dict[str, dict[str, DesignConfig]] = {}
        for desc in sorted(self.rtllm_root.rglob("design_description.txt")):
            design_dir = desc.parent
            name = design_dir.name
            verified = sorted(design_dir.glob("verified_*.v"))
            variants: dict[str, DesignConfig] = {
                "reference": DesignConfig(
                    benchmark=self.benchmark,
                    name=name,
                    variant="reference",
                    rtl_files=tuple(verified),
                    top_module=detect_top_module(verified),
                ),
            }
            for prefix, subdir in self.TRIAL_GROUPS:
                for cand in sorted((self.rtllm_root / subdir).glob(f"t*/{name}.v")):
                    variant = f"{prefix}_{cand.parent.name}"
                    variants[variant] = DesignConfig(
                        benchmark=self.benchmark,
                        name=name,
                        variant=variant,
                        rtl_files=(cand,),
                        top_module=name,
                    )
            names[name] = variants
        return {self.benchmark: names}


class CorpusLoader(DesignLoader):
    """Small corpus of example files."""

    benchmark = "corpus"

    def __init__(self, corpus_dir: Path):
        self.corpus_dir = corpus_dir

    def designs(self) -> DesignTree:
        return {self.benchmark: {
            child.name: {
                "reference": DesignConfig(
                    benchmark=self.benchmark,
                    name=child.name,
                    variant="reference",
                    rtl_files=tuple(sorted(child.glob("*.v"))),
                    top_module=child.name,
                ),
            }
            for child in sorted(self.corpus_dir.iterdir())
        }}
