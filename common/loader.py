"""Hand-curated registry of every design we currently evaluate.

Each design is wired up inline: enough RTL discovery to find its sources,
its testbench runner, and one `DesignConfig` literal per variant. The
combined `all_designs` tree is what every downstream consumer reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pyslang

from common.config import (
    AES,
    DOUBLE_FPU,
    H264_DECODER,
    REED_SOLOMON,
    DesignConfig,
)

# benchmark -> name -> variant -> DesignConfig.
DesignTree = dict[str, dict[str, dict[str, DesignConfig]]]


def detect_top_module(rtl_files: Iterable[Path]) -> str:
    """Top module name across one or more Verilog/SystemVerilog files."""
    files = list(rtl_files)
    comp = pyslang.Compilation()
    for path in files:
        comp.addSyntaxTree(pyslang.SyntaxTree.fromFile(str(path)))
    tops = [t.name for t in comp.getRoot().topInstances]
    if len(tops) != 1:
        raise ValueError(
            f"expected exactly one top module across "
            f"{[str(p) for p in files]}, got {len(tops)}: {tops}"
        )
    return tops[0]


# ---------------------------------------------------------------------------
# secworks/aes
# ---------------------------------------------------------------------------

_AES_RTL_DIR = Path("src") / "rtl"

# tb_aes.v ends with either "*** All NN test cases completed successfully"
# (pass) or "*** NN tests completed - MM test cases did not complete
# successfully." (fail). The pass substring below is unique to the pass
# message -- "completed" sits directly after "test cases" only on pass.
_aes_abs_rtl_dir = AES / _AES_RTL_DIR
_aes_abs_files = sorted(_aes_abs_rtl_dir.glob("*.v"))
aes_reference = DesignConfig(
    benchmark="secworks",
    name="aes",
    variant="reference",
    root=AES,
    rtl_dir=_AES_RTL_DIR,
    rtl_files=tuple(f.relative_to(_aes_abs_rtl_dir) for f in _aes_abs_files),
    top_module=detect_top_module(_aes_abs_files),
    run_tb_cmd="cd toolruns && make top.sim && ./top.sim",
    tb_pass_str="test cases completed successfully",
    tb_timeout_s=420,
)


# ---------------------------------------------------------------------------
# klyone/opencores-ip double-precision FPU
# ---------------------------------------------------------------------------

_DOUBLE_FPU_RTL = (
    "fpu_double.v",
    "fpu_add.v",
    "fpu_sub.v",
    "fpu_mul.v",
    "fpu_div.v",
    "fpu_round.v",
    "fpu_exceptions.v",
)
_DOUBLE_FPU_TB = "fpu_TB.v"
_DOUBLE_FPU_TB_TOP = "fpu_tb"

# fpu_TB.v prints "Answer is correct" per passing case and "Error! out is
# incorrect" per failing case, with no overall summary. We tee the run
# log to a file, then emit a FPU_ALL_PASSED sentinel only if no error
# line appears anywhere, so the unified tb_pass_str check sees the
# sentinel iff every case passed.
_double_fpu_abs_files = tuple(DOUBLE_FPU / f for f in _DOUBLE_FPU_RTL)
_double_fpu_sources = " ".join((*_DOUBLE_FPU_RTL, _DOUBLE_FPU_TB))
double_fpu_reference = DesignConfig(
    benchmark="opencores",
    name="double_fpu",
    variant="reference",
    root=DOUBLE_FPU,
    rtl_dir=Path("."),
    rtl_files=tuple(Path(f) for f in _DOUBLE_FPU_RTL),
    top_module=detect_top_module(_double_fpu_abs_files),
    run_tb_cmd=(
        f"verilator --binary --timing --top-module {_DOUBLE_FPU_TB_TOP} "
        f"-Wno-fatal {_double_fpu_sources} && "
        f"obj_dir/V{_DOUBLE_FPU_TB_TOP} | tee fpu_sim.log && "
        f"! grep -q 'Error! out is incorrect' fpu_sim.log && "
        f"echo FPU_ALL_PASSED"
    ),
    tb_pass_str="FPU_ALL_PASSED",
    tb_timeout_s=900,
)


# ---------------------------------------------------------------------------
# klyone/opencores-ip Reed-Solomon codec generator
# (ecc_core_reed-solomon_codec_generator)
# ---------------------------------------------------------------------------

_REED_SOLOMON_RTL_DIR = Path("example") / "rtl"
# Decoder + encoder modules shipped in example/rtl. RsDecodeTop's clock
# port is `CLK` (uppercase); we pass that through `clock_ports` rather
# than wrapping the module to rename it.
_REED_SOLOMON_DECODER_RTL = (
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
# simReedSolomon.v writes all failure markers ("NG!!!!") to result.out
# rather than stdout, so the unified pass_str check needs help: after
# vvp $finishes, dump result.out for visibility, then emit an
# RS_ALL_PASSED sentinel iff result.out has no NG lines.
_reed_solomon_sources = " ".join(f"../rtl/{f}" for f in _REED_SOLOMON_DECODER_RTL)
reed_solomon_reference = DesignConfig(
    benchmark="opencores",
    name="reed_solomon",
    variant="reference",
    root=REED_SOLOMON,
    rtl_dir=_REED_SOLOMON_RTL_DIR,
    rtl_files=tuple(Path(f) for f in _REED_SOLOMON_DECODER_RTL),
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
    tb_timeout_s=240,
    clock_ports=("CLK",),
)


# ---------------------------------------------------------------------------
# klyone/opencores-ip H.264-AVC baseline decoder, deblocking-filter sub-top
# (video_controller_h.264-avc_baseline_decoder)
# ---------------------------------------------------------------------------

_H264_RTL_DIR = Path("src")
# Transitive closure of DF_top: DF_pipeline + DF_reg_ctrl + DF_mem_ctrl,
# plus the two single-port RAMs DF_top instantiates (a 35k-cell frame
# buffer + a 3k-cell tag buffer). nova_defines.v + timescale.v precede
# the modules so their `\`include` directives resolve.
_H264_DF_RTL = (
    "nova_defines.v",
    "timescale.v",
    "ram_async_1r_sync_1w.v",
    "ram_sync_1r_sync_1w.v",
    "DF_mem_ctrl.v",
    "DF_reg_ctrl.v",
    "DF_pipeline.v",
    "DF_top.v",
)


# No usable shipped TB for the DF_top sub-scope: the upstream
# src/nova_tb.v drives the full nova hierarchy and reads an absolute
# Windows path from Beha_BitStream_ram.v. Leaving run_tb_cmd unset
# routes through DesignConfig.run_tb's "no TB" branch.
df_top_reference = DesignConfig(
    benchmark="opencores",
    name="h264_df_top",
    variant="reference",
    root=H264_DECODER,
    rtl_dir=_H264_RTL_DIR,
    rtl_files=tuple(Path(f) for f in _H264_DF_RTL),
    top_module="DF_top",
)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

all_designs: DesignTree = {
    "secworks": {"aes": {"reference": aes_reference}},
    "opencores": {
        "double_fpu": {"reference": double_fpu_reference},
        "reed_solomon": {"reference": reed_solomon_reference},
        "h264_df_top": {"reference": df_top_reference},
    },
}
