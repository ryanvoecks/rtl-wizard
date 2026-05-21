# Designs

Reference designs we evaluate. Each lives under `external/<name>/` and is
registered in `common/designs.py`. Cell counts below are from the
nangate45 synthesis-only flow.

| Design | Source | Purpose | Cells | Modifications |
|---|---|---|---:|---|
| `aes` | secworks/aes | AES-128/256 block cipher | 21.2k | none |
| `double_fpu` | klyone/opencores-ip | IEEE-754 double-precision FPU | 36.6k | none |
| `reed_solomon` | klyone/opencores-ip | Reed-Solomon decoder + encoder | 40.3k | none |
