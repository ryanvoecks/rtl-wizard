import shutil
import subprocess
import tempfile
from pathlib import Path

from util import eda_env

IVERILOG_BIN = shutil.which("iverilog") or "iverilog"
VVP_BIN = shutil.which("vvp") or "vvp"
OUTPUT_LIMIT = 20_000
COMPILE_TIMEOUT = 60
SIM_TIMEOUT = 120


def iverilog_sim(verilog_paths: list[str]) -> str:
    """Compile the given Verilog/SystemVerilog files with iverilog and run the
    resulting simulation with vvp, returning the testbench output.
    """
    if not verilog_paths:
        return "error: no verilog files provided"

    resolved: list[str] = []
    for p in verilog_paths:
        src = Path(p).expanduser()
        if not src.is_file():
            return f"error: file not found: {src}"
        resolved.append(str(src.resolve()))

    with tempfile.TemporaryDirectory() as tmp:
        simv = str(Path(tmp) / "simv")

        compile_result = subprocess.run(
            [IVERILOG_BIN, "-g2012", "-o", simv, *resolved],
            env=eda_env(),
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
        )
        if compile_result.returncode != 0:
            out = (compile_result.stdout + compile_result.stderr).strip()
            header = f"[iverilog rc={compile_result.returncode}]\n"
            if len(out) > OUTPUT_LIMIT:
                out = out[-OUTPUT_LIMIT:]
                header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
            return header + out

        sim_result = subprocess.run(
            [VVP_BIN, simv],
            env=eda_env(),
            capture_output=True,
            text=True,
            timeout=SIM_TIMEOUT,
        )

    out = (sim_result.stdout + sim_result.stderr).strip()
    header = f"[vvp rc={sim_result.returncode}]\n"
    if len(out) > OUTPUT_LIMIT:
        out = out[-OUTPUT_LIMIT:]
        header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
    return header + out
