from common.config import StudyConfig, TargetConfig
from common.designs import all_designs

# Calibrated AES run, 7.5% fmax uplift target
aes_target = TargetConfig(
    design=all_designs["secworks"]["aes"]["reference"],
    period_ns=0.9683,
    side_um=223.9,
    cfg=StudyConfig(),
)

# All calibrated RTL implementation targets
all_targets = [aes_target]
