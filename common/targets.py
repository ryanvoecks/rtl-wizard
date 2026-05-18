from config import StudyConfig, TargetConfig
from loader import AllDesigns

# Calibrated AES run, 7.5% fmax uplift target
aes_target = TargetConfig(
    design=AllDesigns.designs()["secworks"]["aes"]["reference"],
    period_ns=0.9683,
    side_um=223.9,
    study=StudyConfig(),
)
