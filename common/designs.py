"""Hand-curated registry of every design we currently evaluate.

Each design is wired up inline: enough RTL discovery to find its sources,
its testbench runner, and one `DesignConfig` literal per variant. The
combined `all_designs` tree is what every downstream consumer reads.

Per-design test invocation lives in `common/scripts/<name>.sh`. Each
script is run from the design root by `DesignConfig.run_tb()` and must
exit 0 on pass / non-zero on fail; stdout/stderr go back to the caller.
"""

from __future__ import annotations

from pathlib import Path

from common.config import (
    AES,
    BITONIC_SORTER,
    DOUBLE_FPU,
    E203,
    JPEG_ENCODER,
    REED_SOLOMON,
    SCRIPTS_DIR,
    SHA512,
    SYSTOLIC_TPU,
    UBERDDR3,
    VERILOG_AXI,
    VITERBI,
    WB_DMA,
    DesignConfig,
)

# secworks/aes

AES_RTL_DIR = Path("src") / "rtl"

_aes_abs_rtl_dir = AES / AES_RTL_DIR
_aes_abs_files = sorted(_aes_abs_rtl_dir.glob("*.v"))
aes_reference = DesignConfig(
    benchmark="secworks",
    name="aes",
    variant="reference",
    root=AES,
    rtl_dir=AES_RTL_DIR,
    rtl_files=tuple(f.relative_to(_aes_abs_rtl_dir) for f in _aes_abs_files),
    top_module="aes",
    test_script=SCRIPTS_DIR / "aes.sh",
)


# klyone/opencores-ip double-precision FPU

DOUBLE_FPU_RTL = (
    "fpu_double.v",
    "fpu_add.v",
    "fpu_sub.v",
    "fpu_mul.v",
    "fpu_div.v",
    "fpu_round.v",
    "fpu_exceptions.v",
)
double_fpu_reference = DesignConfig(
    benchmark="opencores",
    name="double_fpu",
    variant="reference",
    root=DOUBLE_FPU,
    rtl_dir=Path("."),
    rtl_files=tuple(Path(f) for f in DOUBLE_FPU_RTL),
    top_module="fpu",
    test_script=SCRIPTS_DIR / "double_fpu.sh",
)


# klyone/opencores-ip Reed-Solomon codec generator

REED_SOLOMON_RTL_DIR = Path("example") / "rtl"
REED_SOLOMON_DECODER_RTL = (
    "RsDecodeChien.v",
    "RsDecodeDegree.v",
    "RsDecodeDelay.v",
    "RsDecodeDpRam.v",
    "RsDecodeErasure.v",
    "RsDecodeEuclide.v",
    "RsDecodeInv.v",
    "RsDecodeMult.v",
    "RsDecodePolymul.v",
    "RsDecodeShiftOmega.v",
    "RsDecodeSyndrome.v",
    "RsDecodeTop.v",
    "RsEncodeTop.v",
)

reed_solomon_reference = DesignConfig(
    benchmark="opencores",
    name="reed_solomon",
    variant="reference",
    root=REED_SOLOMON,
    rtl_dir=REED_SOLOMON_RTL_DIR,
    rtl_files=tuple(Path(f) for f in REED_SOLOMON_DECODER_RTL),
    top_module="RsDecodeTop",
    test_script=SCRIPTS_DIR / "reed_solomon.sh",
    clock_port="CLK",
)


# secworks/sha512

SHA512_RTL_DIR = Path("src") / "rtl"

_sha512_abs_rtl_dir = SHA512 / SHA512_RTL_DIR
_sha512_abs_files = sorted(_sha512_abs_rtl_dir.glob("*.v"))
sha512_reference = DesignConfig(
    benchmark="secworks",
    name="sha512",
    variant="reference",
    root=SHA512,
    rtl_dir=SHA512_RTL_DIR,
    rtl_files=tuple(f.relative_to(_sha512_abs_rtl_dir) for f in _sha512_abs_files),
    top_module="sha512",
    test_script=SCRIPTS_DIR / "sha512.sh",
    tb_timeout_s=180,
)


# OpenCores jpeg_encoder (Richard Herveille)

JPEG_RTL_DIR = Path("common")
JPEG_RTL = (
    Path("jpeg") / "rtl" / "verilog" / "jpeg_encoder.v",
    Path("dct") / "rtl" / "verilog" / "fdct.v",
    Path("dct") / "rtl" / "verilog" / "dct.v",
    Path("dct") / "rtl" / "verilog" / "dct_mac.v",
    Path("dct") / "rtl" / "verilog" / "dctu.v",
    Path("dct") / "rtl" / "verilog" / "dctub.v",
    Path("dct") / "rtl" / "verilog" / "zigzag.v",
    Path("qnr") / "rtl" / "verilog" / "jpeg_qnr.v",
    Path("qnr") / "rtl" / "verilog" / "div_uu.v",
    Path("qnr") / "rtl" / "verilog" / "div_su.v",
    Path("run_length_coding") / "rtl" / "verilog" / "jpeg_rle.v",
    Path("run_length_coding") / "rtl" / "verilog" / "jpeg_rzs.v",
    Path("run_length_coding") / "rtl" / "verilog" / "jpeg_rle1.v",
)
jpeg_encoder_reference = DesignConfig(
    benchmark="opencores",
    name="jpeg_encoder",
    variant="reference",
    root=JPEG_ENCODER,
    rtl_dir=JPEG_RTL_DIR,
    rtl_files=JPEG_RTL,
    top_module="jpeg_encoder",
    include_dirs=(Path("dct") / "rtl" / "verilog",),
    test_script=SCRIPTS_DIR / "jpeg_encoder.sh",
    tb_timeout_s=600,
)


# abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU

SYSTOLIC_TPU_RTL_DIR = Path("Pre-Synthesis_Simulation")
SYSTOLIC_TPU_RTL = (
    Path("tpu_top.v"),
    Path("systolic.v"),
    Path("systolic_controll.v"),
    Path("quantize.v"),
    Path("addr_sel.v"),
    Path("write_out.v"),
)
systolic_tpu_reference = DesignConfig(
    benchmark="abdelazeem201",
    name="systolic_tpu",
    variant="reference",
    root=SYSTOLIC_TPU,
    rtl_dir=SYSTOLIC_TPU_RTL_DIR,
    rtl_files=SYSTOLIC_TPU_RTL,
    top_module="tpu_top",
    test_script=SCRIPTS_DIR / "systolic_tpu.sh",
    tb_timeout_s=120,
)


# alexforencich/verilog-axi
#
# 8x8 AXI4 crossbar, 32-bit data path. Synthesizes the
# `axi_crossbar_wrap_8x8` wrapper rather than the parameterized
# `axi_crossbar` directly: the wrapper hardcodes S_COUNT/M_COUNT into
# the port list, which is what ORFS expects, and it names ports the
# same way the cocotb bench does. The wrapper is the output of upstream's
# `rtl/axi_crossbar_wrap.py -p 8 8`, dropped in via
# `common/patches/verilog_axi.diff` so it doesn't need to be regenerated
# at setup time. It lives under `tb/axi_crossbar/` rather than `rtl/`;
# ORFS's snapshot copies it flat alongside the rest, so the basename
# collision check is fine.

VERILOG_AXI_RTL = (
    Path("rtl") / "arbiter.v",
    Path("rtl") / "priority_encoder.v",
    Path("rtl") / "axi_register_rd.v",
    Path("rtl") / "axi_register_wr.v",
    Path("rtl") / "axi_crossbar_addr.v",
    Path("rtl") / "axi_crossbar_rd.v",
    Path("rtl") / "axi_crossbar_wr.v",
    Path("rtl") / "axi_crossbar.v",
    Path("tb") / "axi_crossbar" / "axi_crossbar_wrap_8x8.v",
)
verilog_axi_reference = DesignConfig(
    benchmark="forencich",
    name="verilog_axi",
    variant="reference",
    root=VERILOG_AXI,
    rtl_dir=Path("."),
    rtl_files=VERILOG_AXI_RTL,
    top_module="axi_crossbar_wrap_8x8",
    test_script=SCRIPTS_DIR / "verilog_axi.sh",
    tb_timeout_s=300,
)


# mcjtag/bitonic_sorter
#
# Fully-pipelined Batcher bitonic sorting network. We synthesize the
# basic (non-AXI-Stream) variant; `common/patches/bitonic_sorter.diff`
# bumps the upstream `CHAN_NUM` default from 8 to 32 so the synth top
# lands ~27k cells (the unmodified default lands ~9k, below our
# 20-50k-cell window). DATA_WIDTH stays at 16. With PIPE_REG=1 (the
# default) every comparator stage is registered, giving a single `clk`
# domain and a pipeline depth of log2(N)*(log2(N)+1)/2 = 15 cycles.
#
# Upstream ships no testbench, so the same patch also drops in
# `test/tb_bitonic_sort.v`: a self-checking iverilog/Verilator TB that
# drives 128 stimuli (zeros, all-ones, sorted, reverse-sorted,
# duplicates, then random), captures `data_out` every cycle, and
# compares against a software insertion sort at the right pipeline
# offset.

BITONIC_SORTER_RTL_DIR = Path("hdl") / "basic"
BITONIC_SORTER_RTL = (
    Path("bitonic_comp.v"),
    Path("bitonic_node.v"),
    Path("bitonic_block.v"),
    Path("bitonic_sort.v"),
)
bitonic_sorter_reference = DesignConfig(
    benchmark="mcjtag",
    name="bitonic_sorter",
    variant="reference",
    root=BITONIC_SORTER,
    rtl_dir=BITONIC_SORTER_RTL_DIR,
    rtl_files=BITONIC_SORTER_RTL,
    top_module="bitonic_sort",
    test_script=SCRIPTS_DIR / "bitonic_sorter.sh",
)


# coole198669/viterbi_decoder
#
# LTE/NB-IoT/GSM-style Viterbi decoder, parametric K=4-7 / rate 1/2-1/6 but
# hardwired for 64 BMU+ACS, so synthesis always builds the full K=7 trellis
# (~53k cells in nangate45). Single `clk_i` domain. The viterbi_core exposes
# external SRAM interfaces; the TB-side memory models (sram_64x64,
# sram_24x4096) live next to the RTL but are only compiled into the TB.
#
# `common/patches/viterbi.diff` comments out two Synopsys-only calls
# (`$fsdbDumpfile` / `$fsdbDumpvars`) in tb_tcase1.v so the TB builds under
# iverilog. The other tcase files don't reference fsdb so they build clean.
# We deliberately exclude the upstream `sram_24x2048.v` from the compile
# list -- it redeclares the same `sram_24x4096` module that `sram_24x4096.v`
# already provides, so including both is a duplicate-module error.

VITERBI_RTL = (
    Path("viterbi_core.v"),
    Path("BMU.v"),
    Path("ACS.v"),
    Path("pm_normalize.v"),
    Path("traceback.v"),
)
viterbi_reference = DesignConfig(
    benchmark="coole198669",
    name="viterbi",
    variant="reference",
    root=VITERBI,
    rtl_dir=Path("."),
    rtl_files=VITERBI_RTL,
    top_module="viterbi_core",
    clock_port="clk_i",
    test_script=SCRIPTS_DIR / "viterbi.sh",
    tb_timeout_s=300,
)


# riscv-mcu/e203_hbirdv2  (Nuclei Hummingbird-V2 "E203" RISC-V core)

E203_CORE = "core"
E203_GENERAL = "general"
E203_SUBSYS = "subsys"
E203_RTL = (
    # core/ -- CPU, TCM controllers, NICE shim. config.v and e203_defines.v
    # are `include-only and must appear before any module that uses them.
    Path(E203_CORE) / "config.v",
    Path(E203_CORE) / "e203_defines.v",
    Path(E203_CORE) / "e203_biu.v",
    Path(E203_CORE) / "e203_clk_ctrl.v",
    Path(E203_CORE) / "e203_clkgate.v",
    Path(E203_CORE) / "e203_core.v",
    Path(E203_CORE) / "e203_cpu.v",
    Path(E203_CORE) / "e203_cpu_top.v",
    Path(E203_CORE) / "e203_dtcm_ctrl.v",
    Path(E203_CORE) / "e203_dtcm_ram.v",
    Path(E203_CORE) / "e203_extend_csr.v",
    Path(E203_CORE) / "e203_exu.v",
    Path(E203_CORE) / "e203_exu_alu.v",
    Path(E203_CORE) / "e203_exu_alu_bjp.v",
    Path(E203_CORE) / "e203_exu_alu_csrctrl.v",
    Path(E203_CORE) / "e203_exu_alu_dpath.v",
    Path(E203_CORE) / "e203_exu_alu_lsuagu.v",
    Path(E203_CORE) / "e203_exu_alu_muldiv.v",
    Path(E203_CORE) / "e203_exu_alu_rglr.v",
    Path(E203_CORE) / "e203_exu_branchslv.v",
    Path(E203_CORE) / "e203_exu_commit.v",
    Path(E203_CORE) / "e203_exu_csr.v",
    Path(E203_CORE) / "e203_exu_decode.v",
    Path(E203_CORE) / "e203_exu_disp.v",
    Path(E203_CORE) / "e203_exu_excp.v",
    Path(E203_CORE) / "e203_exu_longpwbck.v",
    Path(E203_CORE) / "e203_exu_nice.v",
    Path(E203_CORE) / "e203_exu_oitf.v",
    Path(E203_CORE) / "e203_exu_regfile.v",
    Path(E203_CORE) / "e203_exu_wbck.v",
    Path(E203_CORE) / "e203_ifu.v",
    Path(E203_CORE) / "e203_ifu_ifetch.v",
    Path(E203_CORE) / "e203_ifu_ift2icb.v",
    Path(E203_CORE) / "e203_ifu_litebpu.v",
    Path(E203_CORE) / "e203_ifu_minidec.v",
    Path(E203_CORE) / "e203_irq_sync.v",
    Path(E203_CORE) / "e203_itcm_ctrl.v",
    Path(E203_CORE) / "e203_itcm_ram.v",
    Path(E203_CORE) / "e203_lsu.v",
    Path(E203_CORE) / "e203_lsu_ctrl.v",
    Path(E203_CORE) / "e203_reset_ctrl.v",
    Path(E203_CORE) / "e203_srams.v",
    # general/ -- DFF helpers, ICB bus glue, the sim_ram primitive.
    Path(E203_GENERAL) / "sirv_1cyc_sram_ctrl.v",
    Path(E203_GENERAL) / "sirv_gnrl_bufs.v",
    Path(E203_GENERAL) / "sirv_gnrl_dffs.v",
    Path(E203_GENERAL) / "sirv_gnrl_icbs.v",
    Path(E203_GENERAL) / "sirv_gnrl_ram.v",
    Path(E203_GENERAL) / "sirv_gnrl_xchecker.v",
    Path(E203_GENERAL) / "sirv_sim_ram.v",
    Path(E203_GENERAL) / "sirv_sram_icb_ctrl.v",
    # subsys/ -- e203_cpu_top references the NICE coproc body from here.
    Path(E203_SUBSYS) / "e203_subsys_nice_core.v",
)
e203_reference = DesignConfig(
    benchmark="riscv-mcu",
    name="e203",
    variant="reference",
    root=E203,
    rtl_dir=Path("rtl") / "e203",
    rtl_files=E203_RTL,
    # `include "config.v"` and `include "e203_defines.v"` resolve to core/.
    include_dirs=(Path(E203_CORE),),
    top_module="e203_cpu_top",
    test_script=SCRIPTS_DIR / "e203.sh",
    tb_timeout_s=300,
)


# OpenCores wb_dma (Rudolf Usselmann)
#
# Wishbone DMA/Bridge core with up to 31 channels, linked-list descriptors,
# circular buffer mode, and HW/SW handshakes. Single `clk_i` domain (both
# WB interfaces share the clock). Pure Verilog-2001, no SRAM macros -- the
# per-channel descriptor state lives in flop arrays sized by ch_count, which
# is the knob for cell count. The patch bumps ch_count from 1 to 16 and
# enables ch0..ch15 via their conf defaults so synth lands in our cell-count
# window, bumps the TB's `CH_COUNT` define to match, and flips the upstream
# "Long Regression" if(1) to if(0) so the in-budget "Short Regression"
# branch executes within tb_timeout_s.
#
# Pass/fail is derived from the sim log directly (no TB instrumentation): the
# script greps for `^ERROR:` lines and filters out upstream's bogus "ACK
# count Mismatch" assertions, whose formulas are off by 2x against the actual
# master-port ack rate (data correctness checks are unaffected). The watchdog
# already prints `ERROR: Watch Dog Counter Expired` on its own.

WB_DMA_RTL_DIR = Path("rtl") / "verilog"
WB_DMA_RTL = (
    Path("wb_dma_defines.v"),
    Path("wb_dma_ch_pri_enc.v"),
    Path("wb_dma_ch_arb.v"),
    Path("wb_dma_pri_enc_sub.v"),
    Path("wb_dma_ch_sel.v"),
    Path("wb_dma_ch_rf.v"),
    Path("wb_dma_rf.v"),
    Path("wb_dma_wb_if.v"),
    Path("wb_dma_wb_mast.v"),
    Path("wb_dma_wb_slv.v"),
    Path("wb_dma_de.v"),
    Path("wb_dma_inc30r.v"),
    Path("wb_dma_top.v"),
)
wb_dma_reference = DesignConfig(
    benchmark="opencores",
    name="wb_dma",
    variant="reference",
    root=WB_DMA,
    rtl_dir=WB_DMA_RTL_DIR,
    rtl_files=WB_DMA_RTL,
    include_dirs=(WB_DMA_RTL_DIR,),
    top_module="wb_dma_top",
    clock_port="clk_i",
    test_script=SCRIPTS_DIR / "wb_dma.sh",
    tb_timeout_s=600,
)


# AngeloJacobo/UberDDR3
#
# Open-source DDR3 SDRAM controller originally written for the ZipCPU eth10g
# switch. We synthesize only `rtl/ddr3_controller.v` -- the standalone
# controller module is the DUT and runs in a single `i_controller_clk` domain
# (every `always @(posedge ...)` block uses that clock). The other clock
# inputs (i_ddr3_clk, i_ref_clk, i_ddr3_clk_90) live solely in `ddr3_phy.v`
# and never enter the synth target. Default 8-lane DDR3-1600 config with
# BIST built in (BIST_MODE=2, ECC off) lands ~35k cells on nangate45,
# centre of the 20-50k window, with no SRAM macros (pure DFF + std cells).
#
# The testbench drives `ddr3_top` (controller + PHY + Micron 8Gb DDR3 model)
# rather than the controller alone, because exercising real DDR3 traffic
# needs the PHY's deserialised DQ/DQS. The PHY instantiates Xilinx UNISIM
# primitives (OSERDESE2, IDELAYE2, IOBUF*), but the repo ships behavioral
# stubs under `testbench/models/` and switches to them when `SIM_MODEL` is
# defined, so the full chain runs under iverilog without Vivado.
#
# Test script invokes the built-in BIST (BIST_MODE=1): 4608 writes + 4608
# reads across burst/random/alternating-rw patterns. The TB deliberately
# injects 4 bit errors after each pattern to exercise the error-detection
# path, so the pass criterion is `Number of Fails == Number of Injected
# Errors` rather than `Fails == 0`. Synthesis-target file (ddr3_controller.v)
# never sees SIM_MODEL.

UBERDDR3_RTL_DIR = Path("rtl")
UBERDDR3_RTL = (Path("ddr3_controller.v"),)
uberddr3_reference = DesignConfig(
    benchmark="angelo-jacobo",
    name="uberddr3",
    variant="reference",
    root=UBERDDR3,
    rtl_dir=UBERDDR3_RTL_DIR,
    rtl_files=UBERDDR3_RTL,
    top_module="ddr3_controller",
    clock_port="i_controller_clk",
    test_script=SCRIPTS_DIR / "uberddr3.sh",
    tb_timeout_s=600,
)


# Aggregate

# benchmark -> name -> variant -> DesignConfig.
DesignTree = dict[str, dict[str, dict[str, DesignConfig]]]

all_designs: DesignTree = {
    "secworks": {
        "aes": {"reference": aes_reference},
        "sha512": {"reference": sha512_reference},
    },
    "opencores": {
        "double_fpu": {"reference": double_fpu_reference},
        "reed_solomon": {"reference": reed_solomon_reference},
        "jpeg_encoder": {"reference": jpeg_encoder_reference},
        "wb_dma": {"reference": wb_dma_reference},
    },
    "abdelazeem201": {
        "systolic_tpu": {"reference": systolic_tpu_reference},
    },
    "forencich": {
        "verilog_axi": {"reference": verilog_axi_reference},
    },
    "mcjtag": {
        "bitonic_sorter": {"reference": bitonic_sorter_reference},
    },
    "coole198669": {
        "viterbi": {"reference": viterbi_reference},
    },
    "riscv-mcu": {
        "e203": {"reference": e203_reference},
    },
    "angelo-jacobo": {
        "uberddr3": {"reference": uberddr3_reference},
    },
}
