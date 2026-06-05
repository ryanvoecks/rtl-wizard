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

# All targets below were produced by `eda_eval/calibrate.py` at PRESSURE=1.5.
# ORFS sizes the floorplan from cfg.target_utilization

aes_target = TargetConfig(
    design=aes_reference,
    period_ns=0.8343,
    cfg=StudyConfig(),
)

sha512_target = TargetConfig(
    design=sha512_reference,
    period_ns=1.6471,
    cfg=StudyConfig(),
)

double_fpu_target = TargetConfig(
    design=double_fpu_reference,
    period_ns=0.9519,
    cfg=StudyConfig(),
)

reed_solomon_target = TargetConfig(
    design=reed_solomon_reference,
    period_ns=0.7394,
    cfg=StudyConfig(),
)

jpeg_encoder_target = TargetConfig(
    design=jpeg_encoder_reference,
    period_ns=0.7991,
    cfg=StudyConfig(),
)

wb_dma_target = TargetConfig(
    design=wb_dma_reference,
    period_ns=1.0715,
    cfg=StudyConfig(),
)

systolic_target = TargetConfig(
    design=systolic_tpu_reference,
    period_ns=0.9157,
    cfg=StudyConfig(),
)

# Calibrator backed off utilization from the 60 default on PPL-0024
verilog_axi_target = TargetConfig(
    design=verilog_axi_reference,
    period_ns=0.6974,
    target_utilization=50,
    cfg=StudyConfig(),
)

bitonic_target = TargetConfig(
    design=bitonic_sorter_reference,
    period_ns=0.3773,
    cfg=StudyConfig(),
)

viterbi_target = TargetConfig(
    design=viterbi_reference,
    period_ns=1.8900,
    cfg=StudyConfig(),
)

e203_target = TargetConfig(
    design=e203_reference,
    period_ns=2.2179,
    cfg=StudyConfig(),
)

uberddr3_target = TargetConfig(
    design=uberddr3_reference,
    period_ns=0.6729,
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
