"""Study configuration for the ORFS evaluation flow."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path

# Config variables
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EDA_RUNS = REPO_ROOT / "eda_results"
LLM_EVAL = REPO_ROOT / "llm_eval"
LLM_RESULTS = REPO_ROOT / "llm_results"
AES = REPO_ROOT / "external" / "aes"
DOUBLE_FPU = REPO_ROOT / "external" / "double_fpu"
REED_SOLOMON = REPO_ROOT / "external" / "reed_solomon"
CORPUS = REPO_ROOT / "corpus"

# EDA tools and PDK
ORFS_HOME = Path("/") / "OpenROAD-flow-scripts" / "flow"
YOSYS_BIN = (
    Path("/")
    / "OpenROAD-flow-scripts"
    / "tools"
    / "install"
    / "yosys"
    / "bin"
    / "yosys"
)
SYNTH_FLOW_TARGETS = ("synth", "synth-report")
PNR_FLOW_TARGETS = (
    "floorplan",
    "place",
    "cts",
    "route",
    "do-finish",
)
ALL_FLOW_TARGETS = SYNTH_FLOW_TARGETS + PNR_FLOW_TARGETS

# Common types
Result = tuple[str, int]  # Output message, return code tuple


@dataclass(frozen=True)
class StudyConfig:
    """Knobs for the ORFS flow."""

    platform: str = "nangate45"  # ORFS PDK
    calibration_period_ns: float = 10.0  # loose period for the calibration phase
    calibration_side_um: float = 1000.0  # large square die for the calibration phase
    target_multiplier: float = 1.1  # safety factor on calibration-derived period
    target_utilization: float = 0.6  # core utilization target for the final phase
    area_multiplier: float = 1.1  # safety factor on calibration cell area
    minimum_side_um: float = 50.0  # floor on final floorplan side
    core_margin_um: float = 2.0  # die-to-core boundary on each edge
    place_density: float = 0.75  # global placement target density
    io_delay_ns: float = 0.2  # fixed IO delay at each boundary
    seed: int = 0  # seed passed to detailed routing


@dataclass(frozen=True)
class DesignConfig:
    """A single design instance produced by a DesignLoader."""

    benchmark: str  # loader/benchmark that emitted this design
    name: str  # design's logical name
    variant: str  # parameterization tag within a name
    root: Path  # absolute path to design's top-level dir
    rtl_dir: Path  # RTL source dir, relative to `root`
    rtl_files: tuple[Path, ...]  # ordered RTL sources, each relative to `rtl_dir`
    top_module: str  # Verilog top module
    # shell command run from the design root; None = no TB available
    run_tb_cmd: str | None = None
    tb_pass_str: str | None = None  # substring in stdout that marks a passing run
    tb_timeout_s: int = 600  # wall-clock cap on the full tb command
    clock_ports: tuple[str, ...] = ("clk",)  # top-level ports driven by the SDC clock

    @cached_property
    def rtl_abs_paths(self) -> list[Path]:
        """Absolute on-disk path to each RTL source file."""
        return [self.root / self.rtl_dir / f for f in self.rtl_files]

    def run_tb(self, root: Path | None = None) -> Result:
        """Run `run_tb_cmd` under /bin/sh from `root` (defaulting to
        `self.root`) and return (combined-output, rc). Pass (rc=0) iff
        `tb_pass_str` appears in stdout. Designs with no shipped TB
        (`run_tb_cmd is None`) report that and return rc=2."""
        if self.run_tb_cmd is None or self.tb_pass_str is None:
            return ("No usable shipped testbench for this design.", 2)
        cwd = root if root is not None else self.root
        try:
            proc = subprocess.run(
                self.run_tb_cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                shell=True,
                timeout=self.tb_timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            return f"tb timed out after {e.timeout}s\n{e.stdout or ''}", 124
        out = (proc.stdout or "") + (proc.stderr or "")
        if self.tb_pass_str in (proc.stdout or ""):
            return out, 0
        return out, proc.returncode or 1


@dataclass(frozen=True)
class TargetConfig:
    """Parameters fully specifying a calibrated ORFS run."""

    design: DesignConfig  # design being driven through the flow
    period_ns: float  # clock period rendered into the SDC
    side_um: float  # square floorplan side -> DIE_AREA/CORE_AREA
    cfg: StudyConfig  # shared study-wide knobs


@dataclass(frozen=True)
class RunConfig:
    """Parameters that vary per phase invocation of the ORFS flow."""

    synth_target: TargetConfig  # calibrated synthesis target for design
    output_dir: Path  # where this phase's artifacts land
    flow_targets: tuple[str, ...]  # ORFS targets to run, in dependency order


@dataclass(frozen=True)
class RunJob:
    """A pending run paired with an optional upstream error. When `error`
    is None the run is executed; otherwise it's skipped and the message
    propagates to the final summary."""

    run: RunConfig
    error: str | None = None


RUN_CONFIG_FILENAME = "run_config.json"
TARGET_CONFIG_FILENAME = "target_config.json"


def dump_run_config(run: RunConfig, path: Path) -> None:
    """Serialise a RunConfig (and its nested TargetConfig -> DesignConfig +
    StudyConfig) to JSON. The artifact alone is enough to reproduce the run:
    every knob and derived parameter is captured. Paths are serialised as
    plain strings."""
    path.write_text(json.dumps(asdict(run), default=str, indent=2))


def dump_target_config(target: TargetConfig, path: Path) -> None:
    """Serialise a TargetConfig (and its nested DesignConfig + StudyConfig)
    to JSON. Paths are serialised as plain strings."""
    path.write_text(json.dumps(asdict(target), default=str, indent=2))
