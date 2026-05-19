"""Discover the designs available for each supported benchmark."""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

import pyslang

from common.config import AES, CORPUS, RTL_OPT, RTLLM, DesignConfig, Result

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


# RTLLM, RTL-OPT, and Corpus loaders are commented out for now: their
# designs ship only RTL (no separate testbench harness), so the new
# required `root` field has no obvious value distinct from `rtl_dir`.
# Re-enable by adding `root=<repo or rtl dir>` to each DesignConfig and
# putting the loader back into AllDesigns.LOADERS.
#
# class RTLLMLoader(DesignLoader):
#     """Get designs from RTLLM benchmark."""
#
#     benchmark = "rtllm"
#
#     # Mapping of variant-name prefix to subdir
#     TRIAL_GROUPS = (
#         ("chatgpt35", "_chatgpt35"),
#         ("chatgpt4", "_chatgpt4"),
#     )
#
#     def __init__(self, rtllm_root: Path = RTLLM):
#         self.rtllm_root = rtllm_root
#
#     def designs(self) -> DesignTree:
#         names: dict[str, dict[str, DesignConfig]] = {}
#         for desc in sorted(self.rtllm_root.rglob("design_description.txt")):
#             design_dir = desc.parent
#             name = design_dir.name
#             verified = sorted(design_dir.glob("verified_*.v"))
#             variants: dict[str, DesignConfig] = {
#                 "reference": DesignConfig(
#                     benchmark=self.benchmark,
#                     name=name,
#                     variant="reference",
#                     rtl_dir=design_dir,
#                     rtl_files=tuple(verified),
#                     top_module=detect_top_module(verified),
#                 ),
#             }
#             for prefix, subdir in self.TRIAL_GROUPS:
#                 for cand in sorted((self.rtllm_root / subdir).glob(f"t*/{name}.v")):
#                     variant = f"{prefix}_{cand.parent.name}"
#                     variants[variant] = DesignConfig(
#                         benchmark=self.benchmark,
#                         name=name,
#                         variant=variant,
#                         rtl_dir=cand.parent,
#                         rtl_files=(cand,),
#                         top_module=name,
#                     )
#             names[name] = variants
#         return {self.benchmark: names}


_AES_BUILD_TIMEOUT_S = 120
_AES_RUN_TIMEOUT_S = 300


def _run_aes_tb(repo_root: Path) -> Result:
    """Build and run secworks/aes's tb_aes.v under <repo_root>/toolruns.

    tb_aes.v emits "*** All NN test cases completed successfully" on pass and
    "*** NN tests completed - MM test cases did not complete successfully." on
    fail. The two phrases overlap on "test cases ... successfully", so the
    fail substring is checked first."""
    workdir = repo_root / "toolruns"
    try:
        build = subprocess.run(
            ["make", "top.sim"], cwd=workdir,
            capture_output=True, text=True, timeout=_AES_BUILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"build timed out after {e.timeout}s\n{e.stdout or ''}", 124
    if build.returncode != 0:
        return (
            f"# build failed (rc={build.returncode})\n"
            f"{build.stdout}\n--- stderr ---\n{build.stderr}",
            build.returncode,
        )
    try:
        run = subprocess.run(
            ["./top.sim"], cwd=workdir,
            capture_output=True, text=True, timeout=_AES_RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"sim timed out after {e.timeout}s\n{e.stdout or ''}", 124

    stdout = run.stdout
    if "did not complete successfully" in stdout:
        return stdout, 1
    if "test cases completed successfully" in stdout:
        return stdout, 0
    return stdout, 1


class AESLoader(DesignLoader):
    """Get the secworks/aes core. Single design, single variant."""

    benchmark = "secworks"

    def __init__(self, aes_root: Path = AES):
        self.aes_root = aes_root

    def designs(self) -> DesignTree:
        rtl_dir = self.aes_root / "src" / "rtl"
        rtl_files = sorted(rtl_dir.glob("*.v"))
        if not rtl_files:
            return {self.benchmark: {}}
        design = DesignConfig(
            benchmark=self.benchmark,
            name="aes",
            variant="reference",
            root=self.aes_root,
            rtl_dir=rtl_dir,
            rtl_files=tuple(rtl_files),
            top_module=detect_top_module(rtl_files),
            run_tb=_run_aes_tb,
        )
        return {self.benchmark: {"aes": {"reference": design}}}


# class RTLOPTLoader(DesignLoader):
#     """Get designs from the RTL-OPT benchmark."""
#
#     benchmark = "rtl-opt"
#
#     # LLM-generated optimization attempts shipped alongside the benchmark
#     LLM_VARIANTS = ("ds", "dsr", "gpt", "mini")
#
#     def __init__(self, rtl_opt_root: Path = RTL_OPT):
#         self.rtl_opt_root = rtl_opt_root
#
#     @staticmethod
#     def _rtl_in(d: Path) -> list[Path]:
#         return sorted(p for ext in ("*.v", "*.sv") for p in d.glob(ext))
#
#     def designs(self) -> DesignTree:
#         bench_dir = self.rtl_opt_root / "benchmark"
#         llm_dir = self.rtl_opt_root / "Results" / "LLM_Test_result" / "Code"
#         names: dict[str, dict[str, DesignConfig]] = {}
#         for ref_dir in sorted(bench_dir.glob("*_ref")):
#             name = ref_dir.name[: -len("_ref")]
#             sub_dir = bench_dir / name
#             ref_files = self._rtl_in(ref_dir)
#             sub_files = self._rtl_in(sub_dir)
#
#             # Add hand-written variants
#             variants: dict[str, DesignConfig] = {
#                 "reference": DesignConfig(
#                     benchmark=self.benchmark,
#                     name=name,
#                     variant="reference",
#                     rtl_dir=ref_dir,
#                     rtl_files=tuple(ref_files),
#                     top_module=detect_top_module(ref_files),
#                 ),
#                 "suboptimal": DesignConfig(
#                     benchmark=self.benchmark,
#                     name=name,
#                     variant="suboptimal",
#                     rtl_dir=sub_dir,
#                     rtl_files=tuple(sub_files),
#                     top_module=detect_top_module(sub_files),
#                 ),
#             }
#
#             # Add LLM-generated variants
#             for suffix in self.LLM_VARIANTS:
#                 variant_dir = llm_dir / f"{name}_{suffix}"
#                 llm_files = self._rtl_in(variant_dir)
#                 variants[suffix] = DesignConfig(
#                     benchmark=self.benchmark,
#                     name=name,
#                     variant=suffix,
#                     rtl_dir=variant_dir,
#                     rtl_files=tuple(llm_files),
#                     top_module=detect_top_module(llm_files),
#                 )
#             names[name] = variants
#         return {self.benchmark: names}
#
#
# class CorpusLoader(DesignLoader):
#     """Get designs from the in-repo `corpus/` tree.
#
#     Layout: `corpus/<subdir>/<name>/*.v`, where each subdir maps to one
#     variant (`default` → `reference`, `opt` → `claude`)."""
#
#     benchmark = "corpus"
#
#     # Mapping of on-disk subdir to variant name
#     VARIANT_DIRS = (
#         ("default", "reference"),
#         ("opt", "claude"),
#     )
#
#     def __init__(self, corpus_root: Path = CORPUS):
#         self.corpus_root = corpus_root
#
#     @staticmethod
#     def _rtl_in(d: Path) -> list[Path]:
#         return sorted(p for ext in ("*.v", "*.sv") for p in d.glob(ext))
#
#     def designs(self) -> DesignTree:
#         names: dict[str, dict[str, DesignConfig]] = {}
#         for subdir, variant in self.VARIANT_DIRS:
#             for design_dir in sorted((self.corpus_root / subdir).glob("*")):
#                 if not design_dir.is_dir():
#                     continue
#                 rtl_files = self._rtl_in(design_dir)
#                 if not rtl_files:
#                     continue
#                 name = design_dir.name
#                 names.setdefault(name, {})[variant] = DesignConfig(
#                     benchmark=self.benchmark,
#                     name=name,
#                     variant=variant,
#                     rtl_dir=design_dir,
#                     rtl_files=tuple(rtl_files),
#                     top_module=detect_top_module(rtl_files),
#                 )
#         return {self.benchmark: names}


class AllDesigns:
    """Union of every DesignLoader's tree. The single entry point for
    callers that want every discoverable design without picking benchmarks
    by hand."""

    LOADERS: tuple[type[DesignLoader], ...] = (
        AESLoader,
        # RTLLMLoader, RTLOPTLoader, CorpusLoader — commented out pending
        # a meaningful `root` value (see DesignConfig). Their loader
        # classes are still defined (but commented) above.
    )

    @classmethod
    def designs(cls) -> DesignTree:
        out: DesignTree = {}
        for loader in cls.LOADERS:
            out |= loader().designs()
        return out
