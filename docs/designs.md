# Designs

Reference designs we evaluate. Each lives under `external/<name>/` and is
registered in `common/designs.py`. Cell counts below are from the
nangate45 synthesis-only flow.

| Design | Source | Purpose | Cells | Modifications |
|---|---|---|---:|---|
| `aes` | secworks/aes | AES-128/256 block cipher | 21.2k | none |
| `double_fpu` | klyone/opencores-ip | IEEE-754 double-precision FPU | 36.6k | none |
| `reed_solomon` | klyone/opencores-ip | Reed-Solomon decoder + encoder | 40.3k | Added TB timeout |
| `sha512` | secworks/sha512 | SHA-512 hash core + register interface | 24.1k | Inferred 8kb memory as flops |
| `jpeg_encoder` | freecores/video_systems (Herveille) | JPEG baseline encoder: FDCT + quantizer + RLE | 40.2k | Patched TB reference values |
| `wb_dma` | opencores/wb_dma (Usselmann) | Wishbone DMA bridge, 16 channels | 25.8k | Increased channel count from 4 to 16 |
| `systolic_tpu` | abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU | 8x8 INT8 output-stationary systolic GEMM | 44.7k | Fixed TB syntax |
| `verilog_axi` | alexforencich/verilog-axi | 8x8 32-bit AXI4 crossbar | 39.0k | Generated 8x8 wrapper |
| `bitonic_sorter` | mcjtag/bitonic_sorter | Pipelined Batcher bitonic sort network | 25.9k | Increased channel count from 8 to 32 and added TB |
| `viterbi` | coole198669/viterbi_decoder | K=7 Viterbi decoder | 45.2k | Fixed TB syntax |
| `e203` | riscv-mcu/e203_hbirdv2 | Nuclei Hummingbird-V2 RV32IMAC core | 19.5k | Removed instruction/data memories for synthesis |
| `uberddr3` | AngeloJacobo/UberDDR3 | DDR3 SDRAM controller | 35.9k | none |
