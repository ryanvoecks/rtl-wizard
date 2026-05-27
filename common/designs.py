"""Registry of every design we currently evaluate."""

from __future__ import annotations

import sys
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
# 8x8 AXI4 crossbar, 32-bit data path. Auto-generated wrapper added in patch

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


def resolve_design(name: str) -> DesignConfig:
    """Look up a `DesignConfig` by its variable name in this module."""
    obj = getattr(sys.modules[__name__], name, None)
    if not isinstance(obj, DesignConfig):
        available = sorted(
            n
            for n, v in vars(sys.modules[__name__]).items()
            if isinstance(v, DesignConfig)
        )
        raise ValueError(
            f"No DesignConfig named {name!r} in common.designs. "
            f"Available: {', '.join(available)}"
        )
    return obj
