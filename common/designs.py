"""Hand-curated registry of every design we currently evaluate.

Each design is wired up inline: enough RTL discovery to find its sources,
its testbench runner, and one `DesignConfig` literal per variant. The
combined `all_designs` tree is what every downstream consumer reads.
"""

from __future__ import annotations

from pathlib import Path

from common.config import (
    AES,
    DOUBLE_FPU,
    REED_SOLOMON,
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


# Aggregate

# benchmark -> name -> variant -> DesignConfig.
DesignTree = dict[str, dict[str, dict[str, DesignConfig]]]

all_designs: DesignTree = {
    "secworks": {"aes": {"reference": aes_reference}},
    "opencores": {
        "double_fpu": {"reference": double_fpu_reference},
        "reed_solomon": {"reference": reed_solomon_reference},
    },
}
