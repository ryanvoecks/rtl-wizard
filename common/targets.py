import sys

from common.config import StudyConfig, TargetConfig
from common.designs import (
    aes_reference,
    bitonic_sorter_reference,
    double_fpu_reference,
    e203_reference,
    jpeg_encoder_reference,
    reed_solomon_reference,
    sha512_reference,
    systolic_tpu_reference,
    uberddr3_reference,
    verilog_axi_reference,
    viterbi_reference,
    wb_dma_reference,
)

# All targets below were produced by `eda_eval/calibrate.py` at PRESSURE=1.5

aes_target = TargetConfig(
    design=aes_reference,
    period_ns=0.8421,
    side_um=271.5,
    cfg=StudyConfig(),
)

sha512_target = TargetConfig(
    design=sha512_reference,
    period_ns=1.6657,
    side_um=296.9,
    cfg=StudyConfig(),
)

double_fpu_target = TargetConfig(
    design=double_fpu_reference,
    period_ns=0.9507,
    side_um=355.9,
    cfg=StudyConfig(),
)

reed_solomon_target = TargetConfig(
    design=reed_solomon_reference,
    period_ns=0.7450,
    side_um=389.5,
    cfg=StudyConfig(),
)

jpeg_encoder_target = TargetConfig(
    design=jpeg_encoder_reference,
    period_ns=0.7979,
    side_um=385.6,
    cfg=StudyConfig(),
)

# Calibrator converged to realised_pressure=1.00 (timing-met point) rather
# than the 1.5x target.
wb_dma_target = TargetConfig(
    design=wb_dma_reference,
    period_ns=2.5121,
    side_um=328.0,
    cfg=StudyConfig(),
)

systolic_target = TargetConfig(
    design=systolic_tpu_reference,
    period_ns=0.9126,
    side_um=384.7,
    cfg=StudyConfig(),
)

# IO-perimeter-bound floor (3922 pins -> 2615 um). Tighter P&R hits congestion errors.
# Selected realised_pressure=1.23.
verilog_axi_target = TargetConfig(
    design=verilog_axi_reference,
    period_ns=1.8300,
    side_um=2615.0,
    cfg=StudyConfig(),
)

bitonic_target = TargetConfig(
    design=bitonic_sorter_reference,
    period_ns=0.4554,
    side_um=683.4,
    cfg=StudyConfig(),
)

viterbi_target = TargetConfig(
    design=viterbi_reference,
    period_ns=1.9183,
    side_um=374.8,
    cfg=StudyConfig(),
)

# realised_pressure=1.28.
e203_target = TargetConfig(
    design=e203_reference,
    period_ns=3.5049,
    side_um=644.7,
    cfg=StudyConfig(),
)

# IO-perimeter-bound floor (2645 pins -> 1764 um). Tighter P&R hits congestion errors.
# Selected realised_pressure=1.15.
uberddr3_target = TargetConfig(
    design=uberddr3_reference,
    period_ns=1.6050,
    side_um=1763.6,
    cfg=StudyConfig(),
)


# All calibrated RTL implementation targets
all_targets = [
    aes_target,
    sha512_target,
    double_fpu_target,
    reed_solomon_target,
    jpeg_encoder_target,
    wb_dma_target,
    systolic_target,
    verilog_axi_target,
    bitonic_target,
    viterbi_target,
    e203_target,
    uberddr3_target,
]


def resolve_target(name: str) -> TargetConfig:
    """Look up a `TargetConfig` by its variable name in this module."""
    obj = getattr(sys.modules[__name__], name, None)
    if not isinstance(obj, TargetConfig):
        available = sorted(
            n
            for n, v in vars(sys.modules[__name__]).items()
            if isinstance(v, TargetConfig)
        )
        raise ValueError(
            f"No TargetConfig named {name!r} in common.targets. "
            f"Available: {', '.join(available)}"
        )
    return obj
