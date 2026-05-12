"""Study configuration for the ORFS evaluation flow."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StudyConfig:
    """Knobs for the ORFS flow."""

    flow_home: str = "/OpenROAD-flow-scripts/flow"  # ORFS flow dir
    platform: str = "nangate45"            # ORFS PDK
    calibration_period_ns: float = 10.0    # loose period for the calibration phase
    calibration_side_um: float = 1000.0    # large square die for the calibration phase
    target_multiplier: float = 1.1         # safety factor on calibration-derived period
    target_utilization: float = 0.4        # core utilization target for the final phase
    area_multiplier: float = 1.1           # safety factor on calibration cell area
    minimum_side_um: float = 50.0          # floor on final floorplan side
    core_margin_um: float = 2.0            # die-to-core boundary on each edge
    place_density: float = 0.3             # global placement target density
    io_delay_ns: float = 0.2               # fixed IO delay at each boundary


@dataclass(frozen=True)
class DesignConfig:
    """A single design instance produced by a DesignLoader."""

    benchmark: str               # which loader/benchmark emitted this design
    name: str                    # design's logical name
    variant: str                 # distinguishes parameterizations sharing a name
    rtl_files: tuple[Path, ...]  # ordered RTL sources making up the design
