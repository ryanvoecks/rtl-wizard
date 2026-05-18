"""Discover the designs available for each supported benchmark."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

import pyslang

from config import AES, CORPUS, RTL_OPT, RTLLM, DesignConfig

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

    def __init__(self, rtllm_root: Path = RTLLM):
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


class AESLoader(DesignLoader):
    """Get the secworks/aes core. Single design, single variant."""

    benchmark = "secworks"

    def __init__(self, aes_root: Path = AES):
        self.aes_root = aes_root

    def designs(self) -> DesignTree:
        rtl_files = sorted((self.aes_root / "src" / "rtl").glob("*.v"))
        if not rtl_files:
            return {self.benchmark: {}}
        design = DesignConfig(
            benchmark=self.benchmark,
            name="aes",
            variant="reference",
            rtl_files=tuple(rtl_files),
            top_module=detect_top_module(rtl_files),
        )
        return {self.benchmark: {"aes": {"reference": design}}}


class RTLOPTLoader(DesignLoader):
    """Get designs from the RTL-OPT benchmark."""

    benchmark = "rtl-opt"

    # LLM-generated optimization attempts shipped alongside the benchmark
    LLM_VARIANTS = ("ds", "dsr", "gpt", "mini")

    def __init__(self, rtl_opt_root: Path = RTL_OPT):
        self.rtl_opt_root = rtl_opt_root

    @staticmethod
    def _rtl_in(d: Path) -> list[Path]:
        return sorted(p for ext in ("*.v", "*.sv") for p in d.glob(ext))

    def designs(self) -> DesignTree:
        bench_dir = self.rtl_opt_root / "benchmark"
        llm_dir = self.rtl_opt_root / "Results" / "LLM_Test_result" / "Code"
        names: dict[str, dict[str, DesignConfig]] = {}
        for ref_dir in sorted(bench_dir.glob("*_ref")):
            name = ref_dir.name[: -len("_ref")]
            sub_dir = bench_dir / name
            ref_files = self._rtl_in(ref_dir)
            sub_files = self._rtl_in(sub_dir)

            # Add hand-written variants
            variants: dict[str, DesignConfig] = {
                "reference": DesignConfig(
                    benchmark=self.benchmark,
                    name=name,
                    variant="reference",
                    rtl_files=tuple(ref_files),
                    top_module=detect_top_module(ref_files),
                ),
                "suboptimal": DesignConfig(
                    benchmark=self.benchmark,
                    name=name,
                    variant="suboptimal",
                    rtl_files=tuple(sub_files),
                    top_module=detect_top_module(sub_files),
                ),
            }

            # Add LLM-generated variants
            for suffix in self.LLM_VARIANTS:
                llm_files = self._rtl_in(llm_dir / f"{name}_{suffix}")
                variants[suffix] = DesignConfig(
                    benchmark=self.benchmark,
                    name=name,
                    variant=suffix,
                    rtl_files=tuple(llm_files),
                    top_module=detect_top_module(llm_files),
                )
            names[name] = variants
        return {self.benchmark: names}


class CorpusLoader(DesignLoader):
    """Get designs from the in-repo `corpus/` tree.

    Layout: `corpus/<subdir>/<name>/*.v`, where each subdir maps to one
    variant (`default` → `reference`, `opt` → `claude`)."""

    benchmark = "corpus"

    # Mapping of on-disk subdir to variant name
    VARIANT_DIRS = (
        ("default", "reference"),
        ("opt", "claude"),
    )

    def __init__(self, corpus_root: Path = CORPUS):
        self.corpus_root = corpus_root

    @staticmethod
    def _rtl_in(d: Path) -> list[Path]:
        return sorted(p for ext in ("*.v", "*.sv") for p in d.glob(ext))

    def designs(self) -> DesignTree:
        names: dict[str, dict[str, DesignConfig]] = {}
        for subdir, variant in self.VARIANT_DIRS:
            for design_dir in sorted((self.corpus_root / subdir).glob("*")):
                if not design_dir.is_dir():
                    continue
                rtl_files = self._rtl_in(design_dir)
                if not rtl_files:
                    continue
                name = design_dir.name
                names.setdefault(name, {})[variant] = DesignConfig(
                    benchmark=self.benchmark,
                    name=name,
                    variant=variant,
                    rtl_files=tuple(rtl_files),
                    top_module=detect_top_module(rtl_files),
                )
        return {self.benchmark: names}


class AllDesigns:
    """Union of every DesignLoader's tree. The single entry point for
    callers that want every discoverable design without picking benchmarks
    by hand."""

    LOADERS: tuple[type[DesignLoader], ...] = (
        RTLLMLoader,
        RTLOPTLoader,
        AESLoader,
        CorpusLoader,
    )

    @classmethod
    def designs(cls) -> DesignTree:
        out: DesignTree = {}
        for loader in cls.LOADERS:
            out |= loader().designs()
        return out
