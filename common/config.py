"""Study configuration for the ORFS evaluation flow."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# Config variables
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
EDA_RUNS = REPO_ROOT / "eda-results"
OUTPUTS = REPO_ROOT / "llm-results"
RTLLM = REPO_ROOT / "external" / "RTLLM"
RTL_OPT = REPO_ROOT / "external" / "RTL-OPT"
AES = REPO_ROOT / "external" / "aes"
CORPUS = REPO_ROOT / "corpus"
ORFS_HOME = Path("/") / "OpenROAD-flow-scripts" / "flow"


@dataclass(frozen=True)
class StudyConfig:
    """Knobs for the ORFS flow."""

    platform: str = "nangate45"            # ORFS PDK
    calibration_period_ns: float = 10.0    # loose period for the calibration phase
    calibration_side_um: float = 1000.0    # large square die for the calibration phase
    target_multiplier: float = 1.1         # safety factor on calibration-derived period
    target_utilization: float = 0.6        # core utilization target for the final phase
    area_multiplier: float = 1.1           # safety factor on calibration cell area
    minimum_side_um: float = 50.0          # floor on final floorplan side
    core_margin_um: float = 2.0            # die-to-core boundary on each edge
    place_density: float = 0.75            # global placement target density
    io_delay_ns: float = 0.2               # fixed IO delay at each boundary


@dataclass(frozen=True)
class DesignConfig:
    """A single design instance produced by a DesignLoader."""

    benchmark: str               # which loader/benchmark emitted this design
    name: str                    # design's logical name
    variant: str                 # distinguishes parameterizations sharing a name
    rtl_files: tuple[Path, ...]  # ordered RTL sources making up the design
    top_module: str              # Verilog top module

    # Optional upstream testbench harness. Populated for designs that ship a
    # runnable simulator harness (e.g. AESLoader); left at None for designs
    # where only RTL is available. When tb_run_cmd is set, llm-eval's
    # testbench_passes scorer copies the repo to a tempdir, overlays the
    # agent's modified rtl_files onto their original locations, then runs the
    # build/run commands from tb_workdir_rel and grades stdout against the
    # pass/fail markers.
    tb_repo_root: Path | None = None             # upstream repo root on host
    tb_workdir_rel: str | None = None            # cwd for build/run, relative to tb_repo_root
    tb_build_cmd: tuple[str, ...] | None = None  # argv to build the sim (None = skip)
    tb_run_cmd: tuple[str, ...] | None = None    # argv to invoke the sim
    tb_pass_marker: str | None = None            # substring on stdout signalling pass
    tb_fail_marker: str | None = None            # substring on stdout signalling fail (overrides pass)

@dataclass(frozen=True)
class TargetConfig:
    """Parameters fully specifying a calibrated ORFS run."""

    design: DesignConfig  # design being driven through the flow
    period_ns: float      # clock period rendered into the SDC
    side_um: float        # square floorplan side -> DIE_AREA/CORE_AREA
    cfg: StudyConfig      # shared study-wide knobs


@dataclass(frozen=True)
class RunConfig:
    """Parameters that vary per phase invocation of the ORFS flow."""

    design: DesignConfig  # design being driven through the flow
    output_dir: Path      # where this phase's artifacts land
    period_ns: float      # clock period rendered into the SDC
    side_um: float        # square floorplan side -> DIE_AREA/CORE_AREA
    cfg: StudyConfig      # shared study-wide knobs


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
    path.write_text(json.dumps(asdict(run), default=str, indent=2))
