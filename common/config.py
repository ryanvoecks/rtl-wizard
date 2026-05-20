"""Study configuration for the ORFS evaluation flow."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# Config variables
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EDA_RUNS = REPO_ROOT / "eda-results"
LLM_EVAL = REPO_ROOT / "llm-eval"
LLM_RESULTS = REPO_ROOT / "llm-results"
RTLLM = REPO_ROOT / "external" / "RTLLM"
RTL_OPT = REPO_ROOT / "external" / "RTL-OPT"
AES = REPO_ROOT / "external" / "aes"
DOUBLE_FPU = REPO_ROOT / "external" / "double_fpu"
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
    root: Path  # design's top-level dir
    rtl_dir: Path  # dir each rtl_files entry lives under
    rtl_files: tuple[Path, ...]  # ordered RTL sources (absolute paths)
    top_module: str  # Verilog top module
    run_tb: Callable[[Path], Result]  # run the testbench in a given root


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

    design: DesignConfig  # design being driven through the flow
    output_dir: Path  # where this phase's artifacts land
    period_ns: float  # clock period rendered into the SDC
    side_um: float  # square floorplan side -> DIE_AREA/CORE_AREA
    flow_targets: tuple[str, ...]  # ORFS targets to run, in dependency order
    cfg: StudyConfig  # shared study-wide knobs


@dataclass(frozen=True)
class RunJob:
    """A pending run paired with an optional upstream error. When `error`
    is None the run is executed; otherwise it's skipped and the message
    propagates to the final summary."""

    run: RunConfig
    error: str | None = None


RUN_CONFIG_FILENAME = "run_config.json"


def dump_run_config(run: RunConfig, path: Path) -> None:
    """Serialise a RunConfig (and its nested DesignConfig + StudyConfig) to
    JSON. The artifact alone is enough to reproduce the run: every knob and
    derived parameter is captured. Paths are serialised as plain strings."""
    payload = asdict(run)
    payload["design"].pop("run_tb", None)
    path.write_text(json.dumps(payload, default=str, indent=2))
