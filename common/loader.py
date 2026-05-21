"""Hand-curated registry of every design we currently evaluate.

Each design is wired up inline: enough RTL discovery to find its sources,
its testbench runner, and one `DesignConfig` literal per variant. The
combined `all_designs` tree is what every downstream consumer reads.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

import pyslang

from common.config import (
    AES,
    DOUBLE_FPU,
    H264_DECODER,
    PATCHES_DIR,
    REED_SOLOMON,
    DesignConfig,
    Result,
)


def _ensure_patched(repo_root: Path, patch_path: Path) -> None:
    """Apply `patch_path` to `repo_root` with `patch -p1`. Idempotent: if
    the patch reverses cleanly (already applied), do nothing."""
    probe = subprocess.run(
        ["patch", "-p1", "--dry-run", "-R", "-s", "-i", str(patch_path)],
        cwd=repo_root,
        capture_output=True,
    )
    if probe.returncode == 0:
        return
    res = subprocess.run(
        ["patch", "-p1", "--no-backup-if-mismatch", "-i", str(patch_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"patch {patch_path.name} failed in {repo_root}:\n"
            f"{res.stdout}\n{res.stderr}"
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
_AES_BUILD_TIMEOUT_S = 120
_AES_RUN_TIMEOUT_S = 300


def _run_aes_tb(repo_root: Path) -> Result:
    """Build and run secworks/aes's tb_aes.v under <repo_root>/toolruns.

    tb_aes.v emits "*** All NN test cases completed successfully" on pass and
    "*** NN tests completed - MM test cases did not complete successfully." on
    fail. The two phrases overlap on "test cases ... successfully", so the
    fail substring is checked first."""
    workdir = repo_root / "toolruns"
    try:
        build = subprocess.run(
            ["make", "top.sim"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_AES_BUILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"build timed out after {e.timeout}s\n{e.stdout or ''}", 124
    if build.returncode != 0:
        return (
            f"# build failed (rc={build.returncode})\n"
            f"{build.stdout}\n--- stderr ---\n{build.stderr}",
            build.returncode,
        )
    try:
        run = subprocess.run(
            ["./top.sim"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_AES_RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"sim timed out after {e.timeout}s\n{e.stdout or ''}", 124

    stdout = run.stdout
    if "did not complete successfully" in stdout:
        return stdout, 1
    if "test cases completed successfully" in stdout:
        return stdout, 0
    return stdout, 1


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
    run_tb=_run_aes_tb,
)


# ---------------------------------------------------------------------------
# klyone/opencores-ip double-precision FPU
# ---------------------------------------------------------------------------

_DOUBLE_FPU_BUILD_TIMEOUT_S = 300
_DOUBLE_FPU_RUN_TIMEOUT_S = 600
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


def _run_double_fpu_tb(repo_root: Path) -> Result:
    """Build and run the David Lundgren double-precision FPU testbench.

    The TB (`fpu_TB.v`) prints "Error! out is incorrect" for any failing
    case and ends with $finish. A clean run has zero error lines."""
    sources = [repo_root / f for f in _DOUBLE_FPU_RTL] + [repo_root / _DOUBLE_FPU_TB]
    try:
        build = subprocess.run(
            [
                "verilator",
                "--binary",
                "--timing",
                "--top-module",
                _DOUBLE_FPU_TB_TOP,
                "-Wno-fatal",
                *[str(p) for p in sources],
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_DOUBLE_FPU_BUILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"build timed out after {e.timeout}s\n{e.stdout or ''}", 124
    if build.returncode != 0:
        return (
            f"# build failed (rc={build.returncode})\n"
            f"{build.stdout}\n--- stderr ---\n{build.stderr}",
            build.returncode,
        )
    binary = repo_root / "obj_dir" / f"V{_DOUBLE_FPU_TB_TOP}"
    try:
        run = subprocess.run(
            [str(binary)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_DOUBLE_FPU_RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"sim timed out after {e.timeout}s\n{e.stdout or ''}", 124

    stdout = run.stdout
    if "Error! out is incorrect" in stdout:
        return stdout, 1
    if "Answer is correct" in stdout:
        return stdout, 0
    return stdout, 1


_double_fpu_abs_files = tuple(DOUBLE_FPU / f for f in _DOUBLE_FPU_RTL)
double_fpu_reference = DesignConfig(
    benchmark="opencores",
    name="double_fpu",
    variant="reference",
    root=DOUBLE_FPU,
    rtl_dir=Path("."),
    rtl_files=tuple(Path(f) for f in _DOUBLE_FPU_RTL),
    top_module=detect_top_module(_double_fpu_abs_files),
    run_tb=_run_double_fpu_tb,
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
_REED_SOLOMON_BUILD_TIMEOUT_S = 120
_REED_SOLOMON_RUN_TIMEOUT_S = 120


def _run_reed_solomon_tb(repo_root: Path) -> Result:
    """Build and run `example/sim/simReedSolomon.v` under iverilog. It
    self-checks against the shipped RsEnc*.hex / RsDec*.hex test vectors
    in the same dir. Passes when stdout contains 'successful'."""
    sim_dir = repo_root / "example" / "sim"
    rtl_dir = repo_root / "example" / "rtl"
    sources = [sim_dir / "simReedSolomon.v"] + [
        rtl_dir / f for f in _REED_SOLOMON_DECODER_RTL
    ]
    try:
        build = subprocess.run(
            ["iverilog", "-o", "simReedSolomon.vvp", *[str(p) for p in sources]],
            cwd=sim_dir,
            capture_output=True,
            text=True,
            timeout=_REED_SOLOMON_BUILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"build timed out after {e.timeout}s\n{e.stdout or ''}", 124
    if build.returncode != 0:
        return (
            f"# build failed (rc={build.returncode})\n"
            f"{build.stdout}\n--- stderr ---\n{build.stderr}",
            build.returncode,
        )
    try:
        run = subprocess.run(
            ["vvp", "simReedSolomon.vvp"],
            cwd=sim_dir,
            capture_output=True,
            text=True,
            timeout=_REED_SOLOMON_RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return f"sim timed out after {e.timeout}s\n{e.stdout or ''}", 124
    return run.stdout, run.returncode


reed_solomon_reference = DesignConfig(
    benchmark="opencores",
    name="reed_solomon",
    variant="reference",
    root=REED_SOLOMON,
    rtl_dir=_REED_SOLOMON_RTL_DIR,
    rtl_files=tuple(Path(f) for f in _REED_SOLOMON_DECODER_RTL),
    top_module="RsDecodeTop",
    run_tb=_run_reed_solomon_tb,
    clock_ports=("CLK",),
)


# ---------------------------------------------------------------------------
# klyone/opencores-ip H.264-AVC baseline decoder, deblocking-filter sub-top
# (video_controller_h.264-avc_baseline_decoder)
# ---------------------------------------------------------------------------

_H264_RTL_DIR = Path("src")
# Transitive closure of DF_top: DF_pipeline + DF_reg_ctrl + DF_mem_ctrl,
# plus the two single-port RAMs DF_top instantiates (a 35k-cell frame
# buffer + a 3k-cell tag buffer). Those RAM files contain an `initial`
# sim-only param-check that calls `$finish` which yosys 0.64 rejects --
# the patch strips those two lines. nova_defines.v + timescale.v
# precede the modules so their `\`include` directives resolve.
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


def _run_h264_tb(repo_root: Path) -> Result:
    """The upstream `src/nova_tb.v` exercises the full `nova` decoder
    (not the `DF_top` sub-scope we synthesize). It also expects a
    Windows-path bitstream in Beha_BitStream_ram.v. There's no shipped
    TB for this sub-scope; we report that explicitly."""
    _ = repo_root
    return (
        "No usable shipped testbench for the DF_top sub-scope. "
        "The upstream nova_tb.v drives the full nova hierarchy and reads "
        "an absolute Windows path that isn't shipped here.",
        2,
    )


_ensure_patched(H264_DECODER, PATCHES_DIR / "h264_decoder.diff")

df_top_reference = DesignConfig(
    benchmark="opencores",
    name="h264_df_top",
    variant="reference",
    root=H264_DECODER,
    rtl_dir=_H264_RTL_DIR,
    rtl_files=tuple(Path(f) for f in _H264_DF_RTL),
    top_module="DF_top",
    run_tb=_run_h264_tb,
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
