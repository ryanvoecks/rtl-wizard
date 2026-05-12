"""Discover the designs available for each supported benchmark."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from config import DesignConfig


class DesignLoader(ABC):
    """Base class for benchmark-specific design discovery."""

    benchmark: str

    @abstractmethod
    def designs(self) -> list[DesignConfig]:
        """All `DesignConfig`s this loader can find on disk."""


class CorpusLoader(DesignLoader):
    """Each subfolder under the corpus root is one design with one .v file."""

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
                variant="default",
                rtl_files=rtl_files,
            ))
        return configs
