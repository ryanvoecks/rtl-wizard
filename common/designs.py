"""Hand-curated registry of every design we currently evaluate.

Each design is wired up inline: enough RTL discovery to find its sources,
its testbench runner, and one `DesignConfig` literal per variant. The
combined `all_designs` tree is what every downstream consumer reads.
"""

from __future__ import annotations

from pathlib import Path

from common.config import (
    AES,
    BITONIC_SORTER,
    DOUBLE_FPU,
    E203,
    JPEG_ENCODER,
    MSHR_CACHE,
    R22SDF,
    REED_SOLOMON,
    SHA512,
    SYSTOLIC_TPU,
    VERILOG_AXI,
    VITERBI,
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
    run_tb_cmd="cd toolruns && make top.sim && ./top.sim",
    tb_pass_str="test cases completed successfully",
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
DOUBLE_FPU_TB = "fpu_TB.v"
DOUBLE_FPU_TB_TOP = "fpu_tb"

_double_fpu_sources = " ".join((*DOUBLE_FPU_RTL, DOUBLE_FPU_TB))
double_fpu_reference = DesignConfig(
    benchmark="opencores",
    name="double_fpu",
    variant="reference",
    root=DOUBLE_FPU,
    rtl_dir=Path("."),
    rtl_files=tuple(Path(f) for f in DOUBLE_FPU_RTL),
    top_module="fpu",
    run_tb_cmd=(
        f"verilator --binary --timing --top-module {DOUBLE_FPU_TB_TOP} "
        f"-Wno-fatal {_double_fpu_sources} && "
        f"obj_dir/V{DOUBLE_FPU_TB_TOP} | tee fpu_sim.log && "
        f"! grep -q 'Error! out is incorrect' fpu_sim.log && "
        f"echo FPU_ALL_PASSED"
    ),
    tb_pass_str="FPU_ALL_PASSED",
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

_reed_solomon_sources = " ".join(f"../rtl/{f}" for f in REED_SOLOMON_DECODER_RTL)
reed_solomon_reference = DesignConfig(
    benchmark="opencores",
    name="reed_solomon",
    variant="reference",
    root=REED_SOLOMON,
    rtl_dir=REED_SOLOMON_RTL_DIR,
    rtl_files=tuple(Path(f) for f in REED_SOLOMON_DECODER_RTL),
    top_module="RsDecodeTop",
    run_tb_cmd=(
        "cd example/sim && "
        f"iverilog -o simReedSolomon.vvp simReedSolomon.v {_reed_solomon_sources} && "
        "vvp simReedSolomon.vvp && "
        "cat result.out && "
        "! grep -q NG result.out && "
        "echo RS_ALL_PASSED"
    ),
    tb_pass_str="RS_ALL_PASSED",
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
    run_tb_cmd="cd toolruns && make top.sim && ./top.sim",
    tb_pass_str="test cases completed successfully",
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
_jpeg_tb = "common/jpeg/bench/verilog/bench_top.v"
_jpeg_rtl_sources = " ".join(str(JPEG_RTL_DIR / f) for f in JPEG_RTL)
_jpeg_incdirs = "-I common/qnr/bench/verilog -I common/dct/rtl/verilog"
jpeg_encoder_reference = DesignConfig(
    benchmark="opencores",
    name="jpeg_encoder",
    variant="reference",
    root=JPEG_ENCODER,
    rtl_dir=JPEG_RTL_DIR,
    rtl_files=JPEG_RTL,
    top_module="jpeg_encoder",
    include_dirs=(Path("dct") / "rtl" / "verilog",),
    run_tb_cmd=(
        f"iverilog -o jpeg.sim {_jpeg_incdirs} {_jpeg_tb} {_jpeg_rtl_sources} && "
        "vvp -n jpeg.sim | tee jpeg_sim.log && "
        "grep -qE 'Total errors:[[:space:]]+0$' jpeg_sim.log && "
        "echo JPEG_ALL_PASSED"
    ),
    tb_pass_str="JPEG_ALL_PASSED",
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
SYSTOLIC_TPU_TB_SOURCES = (
    "test_tpu.v tpu_top.v systolic.v systolic_controll.v "
    "quantize.v addr_sel.v write_out.v sram_16x128b.v sram_256x32b.v"
)
systolic_tpu_reference = DesignConfig(
    benchmark="abdelazeem201",
    name="systolic_tpu",
    variant="reference",
    root=SYSTOLIC_TPU,
    rtl_dir=SYSTOLIC_TPU_RTL_DIR,
    rtl_files=SYSTOLIC_TPU_RTL,
    top_module="tpu_top",
    run_tb_cmd=(
        f"cd {SYSTOLIC_TPU_RTL_DIR} && "
        f"iverilog -g2012 -o tpu.sim {SYSTOLIC_TPU_TB_SOURCES} && "
        "vvp tpu.sim"
    ),
    tb_pass_str="TPU_ALL_PASSED",
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
#
# Verification is the upstream cocotb regression (33 cases: writes,
# reads, stress); see Makefile in `tb/axi_crossbar/`. The cocotb stack
# is installed system-wide in the devcontainer image (see
# `.devcontainer/Dockerfile`), pinned to <2.0 because 2.x breaks a
# pre-reset X-coerce path the bench relies on.

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
    run_tb_cmd=("PARAM_S_COUNT=8 PARAM_M_COUNT=8 make -C tb/axi_crossbar"),
    tb_pass_str="FAIL=0 SKIP=0",
    tb_timeout_s=300,
)


# nanamake/r22sdf
#
# Radix-2^2 single-path delay feedback pipelined FFT. We synthesize the
# 128-point / 16-bit basic configuration (no twiddle compression). At
# nangate45 this lands ~26.4k cells -- comfortably in the 20-50k window.
# Single `clock` domain (async-reset only), pure Verilog, no memory
# macros (the SDF delay buffers synthesize as shift registers).
#
# The repo's testbench (sim/fft_128_tc/TB128.v) drives two stimulus
# vectors and `$fdisplay`s the bit-reversed outputs to output4.txt and
# output5.txt. The committed copies of those files are the golden
# reference; `common/patches/r22sdf.diff` snapshots them as
# output4_golden.txt / output5_golden.txt before the TB runs and
# overwrites them. Pass/fail is a diff against the snapshot. The
# committed reference files have CRLF line endings (Windows-authored
# repo), so the patch normalizes the goldens to LF -- and we strip \r
# from the TB's freshly-written output before diffing. The conversion
# is done with `sed` writing to a sibling file rather than bash process
# substitution, since `DesignConfig.run_tb()` runs the command under
# /bin/sh.
#
# The sim directory is named `fft_128_tc` (twiddle-compressed) but
# TB128.v depends only on FFT's external interface, so swapping in
# `SdfUnit.v` + the full `Twiddle128.v` table in place of `SdfUnit_TC.v`
# + `TwiddleConvert8.v` produces matching outputs while keeping the
# basic, more easily-readable RTL as the synth target.

R22SDF_RTL_DIR = Path("verilog")
R22SDF_RTL = (
    Path("FFT128.v"),
    Path("SdfUnit.v"),
    Path("SdfUnit2.v"),
    Path("Butterfly.v"),
    Path("DelayBuffer.v"),
    Path("Multiply.v"),
    Path("Twiddle128.v"),
)
_r22sdf_rtl_sources = " ".join(f"../../verilog/{f.name}" for f in R22SDF_RTL)
r22sdf_reference = DesignConfig(
    benchmark="nanamake",
    name="r22sdf",
    variant="reference",
    root=R22SDF,
    rtl_dir=R22SDF_RTL_DIR,
    rtl_files=R22SDF_RTL,
    top_module="FFT",
    clock_port="clock",
    run_tb_cmd=(
        "cd sim/fft_128_tc && "
        f"iverilog -o tb128.vvp {_r22sdf_rtl_sources} TB128.v && "
        "vvp tb128.vvp && "
        "sed -i 's/\\r$//' output4.txt output5.txt && "
        "diff output4.txt output4_golden.txt && "
        "diff output5.txt output5_golden.txt && "
        "echo R22SDF_ALL_PASSED"
    ),
    tb_pass_str="R22SDF_ALL_PASSED",
    tb_timeout_s=120,
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
# offset. Pass string: `BITONIC_ALL_PASSED`.

BITONIC_SORTER_RTL_DIR = Path("hdl") / "basic"
BITONIC_SORTER_RTL = (
    Path("bitonic_comp.v"),
    Path("bitonic_node.v"),
    Path("bitonic_block.v"),
    Path("bitonic_sort.v"),
)
_bitonic_sorter_rtl_sources = " ".join(
    str(BITONIC_SORTER_RTL_DIR / f) for f in BITONIC_SORTER_RTL
)
bitonic_sorter_reference = DesignConfig(
    benchmark="mcjtag",
    name="bitonic_sorter",
    variant="reference",
    root=BITONIC_SORTER,
    rtl_dir=BITONIC_SORTER_RTL_DIR,
    rtl_files=BITONIC_SORTER_RTL,
    top_module="bitonic_sort",
    run_tb_cmd=(
        f"iverilog -g2012 -o test/tb.vvp test/tb_bitonic_sort.v "
        f"{_bitonic_sorter_rtl_sources} && "
        "vvp test/tb.vvp"
    ),
    tb_pass_str="BITONIC_ALL_PASSED",
    tb_timeout_s=60,
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
# iverilog. We deliberately exclude the upstream `sram_24x2048.v` from the
# compile list -- it redeclares the same `sram_24x4096` module that
# `sram_24x4096.v` already provides, so including both is a duplicate-module
# error.
#
# tb_tcase1 runs the K=7 rate-1/2 case (polynomials 0o117/0o155, 192-bit
# frame x100) and dumps the decoded bitstream to case1_tv_data_out.txt; we
# diff that against the shipped golden `testvectors/case1_tv/output.txt`
# (19,200 lines, exact match) to gate pass/fail.

VITERBI_RTL = (
    Path("viterbi_core.v"),
    Path("BMU.v"),
    Path("ACS.v"),
    Path("pm_normalize.v"),
    Path("traceback.v"),
)
_viterbi_rtl_sources = " ".join(str(f) for f in VITERBI_RTL)
_viterbi_tb_extra_sources = "sram_64x64.v sram_24x4096.v tb_tcase1.v"
viterbi_reference = DesignConfig(
    benchmark="coole198669",
    name="viterbi",
    variant="reference",
    root=VITERBI,
    rtl_dir=Path("."),
    rtl_files=VITERBI_RTL,
    top_module="viterbi_core",
    clock_port="clk_i",
    run_tb_cmd=(
        f"iverilog -g2005 -o sim.vvp {_viterbi_rtl_sources} "
        f"{_viterbi_tb_extra_sources} && "
        "vvp -n sim.vvp > sim.log && "
        # golden output has a trailing space on every line; -w ignores it.
        "diff -wq case1_tv_data_out.txt testvectors/case1_tv/output.txt && "
        "echo VITERBI_ALL_PASSED"
    ),
    tb_pass_str="VITERBI_ALL_PASSED",
    tb_timeout_s=120,
)


# brownie-crumble/mshr-cache-verification
#
# Non-blocking L1 cache with 2-way set-associative LRU, 4-entry coalescing
# MSHRs, and a latency-modelled backing memory. Pure Verilog, single `clk`
# domain (the `rst` input is async-reset only, not a second clock). Upstream
# ships defaults of NUM_SETS=4, NUM_WAYS=2, BLOCK_SIZE=8 -- the resulting
# datapath is ~1k cells, well below our 20-50k-cell window. The patch widens
# `BLOCK_SIZE` to 384 bits (the cache, MSHR data buffers, coalescing buffers
# and backing-mem `mem_model` all parameterize off the same `define`), which
# scales the storage-dominated cell count to ~28k while leaving the cache
# geometry (4 sets x 2 ways x 4 MSHRs) and every testbench address untouched.
#
# The testbench drives six self-checking scenarios (write-then-read, cold
# miss/fill/hit, MSHR-full on a 5th outstanding miss, miss coalescing on a
# repeated address, and LRU eviction) and prints `ALL TESTS PASSED` iff its
# internal `fail_count` stays at 0. iverilog 2012 builds and runs the whole
# `defines.v + cache.v + mem_model.v + testbench.v` set in one shot -- no
# memh files, no scripts, no toolchain.
#
# CTS quirk: the cache's wide-register arrays (cache_data + MSHR write/coal
# buffers, each NUM_SETS*NUM_WAYS*BLOCK_SIZE bits) push the hold-buffer count
# past OpenROAD's default `max_buffer_percent` of 20 during repair_timing in
# the calibration's sparse 1000-um die. The `pre_cts_tcl` hook redefines
# `repair_timing_helper` to call `repair_timing -max_buffer_percent 80` so
# all hold violations can be repaired; with that, calibration closes with
# WNS/TNS/hold_violation_count all 0.

_MSHR_CACHE_PRE_CTS_TCL = """\
# Storage-heavy designs (wide-register arrays) exceed repair_timing's default
# 20% hold-buffer cap when the calibration die is sparse. Raise it to 80%.
rename repair_timing_helper repair_timing_helper_orig
proc repair_timing_helper {} {
  log_cmd repair_timing -max_buffer_percent 80 -verbose
}
"""

MSHR_CACHE_RTL = (
    # defines.v is `define-only and is `included by cache.v; yosys parses it
    # cleanly (it contributes no modules) and we copy it into inputs/rtl/
    # alongside cache.v so the include resolves without a separate +incdir.
    Path("defines.v"),
    Path("cache.v"),
)
mshr_cache_reference = DesignConfig(
    benchmark="brownie-crumble",
    name="mshr_cache",
    variant="reference",
    root=MSHR_CACHE,
    rtl_dir=Path("."),
    rtl_files=MSHR_CACHE_RTL,
    top_module="cache",
    run_tb_cmd=(
        "iverilog -g2012 -o sim.vvp defines.v cache.v mem_model.v testbench.v && "
        "vvp -n sim.vvp"
    ),
    tb_pass_str="ALL TESTS PASSED",
    tb_timeout_s=60,
    pre_cts_tcl=_MSHR_CACHE_PRE_CTS_TCL,
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
    # The upstream vsim Makefile drives: copy RTL+TB into install/ ->
    # iverilog compile (with a `define iverilog injected into install/tb/
    # tb_top.v by run.makefile's sed) -> vvp on the pre-built test hex.
    # rv32ui-p-add is the cheapest representative; the iverilog compile
    # dominates wall time (~30s), the actual run is a few seconds.
    run_tb_cmd=(
        "cd vsim && make install >/dev/null && make compile >/dev/null && "
        "make run_test TESTNAME=rv32ui-p-add DUMPWAVE=0"
    ),
    tb_pass_str="TEST_PASS",
    tb_timeout_s=180,
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
    },
    "abdelazeem201": {
        "systolic_tpu": {"reference": systolic_tpu_reference},
    },
    "forencich": {
        "verilog_axi": {"reference": verilog_axi_reference},
    },
    "nanamake": {
        "r22sdf": {"reference": r22sdf_reference},
    },
    "mcjtag": {
        "bitonic_sorter": {"reference": bitonic_sorter_reference},
    },
    "coole198669": {
        "viterbi": {"reference": viterbi_reference},
    },
    "brownie-crumble": {
        "mshr_cache": {"reference": mshr_cache_reference},
    },
    "riscv-mcu": {
        "e203": {"reference": e203_reference},
    },
}
