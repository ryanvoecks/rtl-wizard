"""Study configuration for the ORFS evaluation flow."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

# Important directories
HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE / "scripts"
REPO_ROOT = HERE.parent
EDA_EVAL = REPO_ROOT / "eda_eval"
EDA_RUNS = REPO_ROOT / "eda_results"
LLM_EVAL = REPO_ROOT / "llm_eval"
LLM_RESULTS = REPO_ROOT / "llm_results"
EXTERNAL = REPO_ROOT / "external"
ARTIFACTS = REPO_ROOT / "artifacts"

# Submodules
AES = EXTERNAL / "aes"
DOUBLE_FPU = EXTERNAL / "double_fpu"
REED_SOLOMON = EXTERNAL / "reed_solomon"
SHA512 = EXTERNAL / "sha512"
JPEG_ENCODER = EXTERNAL / "jpeg_encoder"
SYSTOLIC_TPU = EXTERNAL / "systolic_tpu"
VERILOG_AXI = EXTERNAL / "verilog_axi"
BITONIC_SORTER = EXTERNAL / "bitonic_sorter"
VITERBI = EXTERNAL / "viterbi"
E203 = EXTERNAL / "e203"
WB_DMA = EXTERNAL / "wb_dma"
UBERDDR3 = EXTERNAL / "uberddr3"
WB_CONMAX = EXTERNAL / "wb_conmax"
CORDIC = EXTERNAL / "cordic"
FIR = EXTERNAL / "fir"
MODEXP = EXTERNAL / "modexp"

# EDA tools and PDK
ORFS_HOME = Path("/") / "OpenROAD-flow-scripts"
ORFS_FLOW = ORFS_HOME / "flow"
YOSYS_BIN = ORFS_HOME / "tools" / "install" / "yosys" / "bin" / "yosys"
EQY_BIN = ORFS_HOME / "dependencies" / "bin" / "eqy"
SYNTH_FLOW_TARGETS = ("synth", "synth-report")
PNR_FLOW_TARGETS = (
    "floorplan",
    "place",
    "cts",
    "route",
    "do-finish",
)
ALL_FLOW_TARGETS = SYNTH_FLOW_TARGETS + PNR_FLOW_TARGETS


# Raise if ORFS isn't installed at `ORFS_FLOW`
if not (ORFS_FLOW / "Makefile").is_file():
    raise FileNotFoundError(f"ORFS flow not found at {ORFS_FLOW}")


# Common types
Result = tuple[str, int]  # Output message, return code tuple


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the whole process group led by `proc` and reap the leader.

    `proc` must have been started with `start_new_session=True` so it leads
    its own group; killing the group takes any background simulators the
    script spawned (e.g. iverilog `vvp`) down with it. Idempotent -- a no-op
    once the group is already gone."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _from_json(cls: Any, val: Any) -> Any:
    """Inverse of `asdict(...) + default=str`: rebuild nested dataclasses,
    rehydrate `tuple[T, ...]` from JSON lists, and reconstruct `Path`s."""
    if isinstance(cls, type) and is_dataclass(cls):
        h = get_type_hints(cls)
        return cls(**{f.name: _from_json(h[f.name], val[f.name]) for f in fields(cls)})
    if get_origin(cls) is tuple:
        return tuple(_from_json(get_args(cls)[0], v) for v in val)
    return cls(val) if cls is Path else val


@dataclass(frozen=True)
class StudyConfig:
    """Knobs for the ORFS flow."""

    platform: str = "nangate45"  # ORFS PDK
    anchor_period_ns: float = 0.5  # starting tight period for calibration anchor sweep
    core_aspect_ratio: float = 1.0  # core height/width ratio
    core_margin_um: float = 2.0  # die-to-core boundary on each edge
    place_density: float = 0.75  # global placement target density
    io_delay_fraction: float = 0.4  # IO delay as a fraction of clock period
    seed: int = 0  # seed passed to detailed routing
    synth_memory_max_bits: int = 8192  # maximum memory-inferred-as-logic size
    place_pins_args: str = "-hor_layers metal3 -ver_layers metal4"  # pin settings


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
    test_script: Path  # script run from the design root; rc 0 == pass
    tb_timeout_s: int = 60  # wall-clock cap on the full tb command
    clock_port: str = "clk"  # top-level port driven by the SDC clock
    include_dirs: tuple[Path, ...] = ()  # source dirs added to yosys's `+incdir`

    @cached_property
    def rtl_abs_paths(self) -> list[Path]:
        """Absolute on-disk path to each RTL source file."""
        return [self.root / self.rtl_dir / f for f in self.rtl_files]

    def run_tb(self) -> Result:
        """Run the design testbench in its own process group.

        The script may launch background simulators (e.g. iverilog `vvp`) that
        outlive it. `subprocess`'s own timeout only kills the direct child, so
        a sim that never `$finish`es would keep running and pin CPU forever.
        Starting a new session and SIGKILLing the whole group on exit
        guarantees the sims die with the wrapper -- whether the run finishes
        normally, times out, or errors."""
        proc = subprocess.Popen(
            [str(self.test_script)],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=self.tb_timeout_s)
            return out, proc.returncode
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            out, _ = proc.communicate()
            return f"tb timed out after {self.tb_timeout_s}s\n{out}", 124
        finally:
            _kill_process_group(proc)


@dataclass(frozen=True)
class TargetConfig:
    """Parameters fully specifying a calibrated ORFS run."""

    FILENAME: ClassVar[str] = "target_config.json"  # default dump basename

    cfg: StudyConfig  # shared study-wide knobs
    design: DesignConfig  # design being driven through the flow
    period_ns: float  # clock period rendered into the SDC
    target_utilization: float = 60  # core utilization percentage

    def dump(self, path: Path) -> None:
        """Serialise to JSON."""
        path.write_text(json.dumps(asdict(self), default=str, indent=2))


@dataclass(frozen=True)
class RunConfig:
    """Parameters that vary per phase invocation of the ORFS flow."""

    FILENAME: ClassVar[str] = "run_config.json"  # default dump basename

    synth_target: TargetConfig  # calibrated synthesis target for design
    output_dir: Path  # where this phase's artifacts land
    flow_targets: tuple[str, ...]  # ORFS targets to run, in dependency order
    num_threads: int = field(default_factory=lambda: os.cpu_count() or 1)  # ORFS cores

    def dump(self, path: Path) -> None:
        """Serialise to JSON."""
        path.write_text(json.dumps(asdict(self), default=str, indent=2))

    @classmethod
    def load(cls, path: Path) -> "RunConfig":
        """Inverse of `dump`."""
        return _from_json(cls, json.loads(path.read_text()))


@dataclass(frozen=True)
class RunJob:
    """A pending run paired with an optional upstream error."""

    run: RunConfig
    error: str | None = None
