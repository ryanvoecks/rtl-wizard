from common.config import StudyConfig, TargetConfig
from common.designs import all_designs

# Calibrated AES run, 7.5% fmax uplift target
aes_target = TargetConfig(
    design=all_designs["secworks"]["aes"]["reference"],
    period_ns=0.9683,
    side_um=223.9,
    cfg=StudyConfig(),
)

# Calibrated Viterbi decoder at 1.5x fmax
viterbi_target = TargetConfig(
    design=all_designs["coole198669"]["viterbi"]["reference"],
    period_ns=2.454,
    side_um=366.7,
    cfg=StudyConfig(),
)

# Calibrated bitonic sorter at 1.5x fmax
bitonic_target = TargetConfig(
    design=all_designs["mcjtag"]["bitonic_sorter"]["reference"],
    period_ns=0.4349,
    side_um=355.1,
    cfg=StudyConfig(),
)

# Calibrated systolic array at 1.5x fmax
systolic_target = TargetConfig(
    design=all_designs["abdelazeem201"]["systolic_tpu"]["reference"],
    period_ns=1.009,
    side_um=345.4,
    cfg=StudyConfig(),
)

# All calibrated RTL implementation targets
all_targets = [aes_target, viterbi_target, bitonic_target, systolic_target]
