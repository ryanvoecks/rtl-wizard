import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from util import eda_env

VERILATOR_BIN = shutil.which("verilator") or "verilator"
OUTPUT_LIMIT = 20_000
COMPILE_TIMEOUT = 180
SIM_TIMEOUT = 120

# Top-module hint: by convention RTLLM testbenches declare `module testbench`,
# so prefer that as the elaboration root if present in the input set. Without
# the hint Verilator errors out when the inputs contain multiple unconnected
# roots (DUT + tb).
_MODULE_RE = re.compile(r"^\s*module\s+(\w+)\b", re.MULTILINE)


def _pick_top(paths: list[str]) -> str | None:
    for p in paths:
        try:
            text = Path(p).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for m in _MODULE_RE.finditer(text):
            if m.group(1) == "testbench":
                return "testbench"
    return None


def verilator_sim(verilog_paths: list[str]) -> str:
    """Compile the given Verilog/SystemVerilog files with Verilator's
    `--binary` mode and run the resulting executable, returning the
    testbench output.
    """
    if not verilog_paths:
        return "error: no verilog files provided"

    resolved: list[str] = []
    for p in verilog_paths:
        src = Path(p).expanduser()
        if not src.is_file():
            return f"error: file not found: {src}"
        resolved.append(str(src.resolve()))

    top = _pick_top(resolved)

    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            VERILATOR_BIN,
            "--binary",
            "--timing",
            "-Wno-fatal",
            "-j", "0",
            "-o", "simv",
        ]
        if top:
            cmd += ["--top-module", top]
        cmd += resolved

        compile_result = subprocess.run(
            cmd,
            env=eda_env(),
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
            cwd=tmp,
        )
        if compile_result.returncode != 0:
            out = (compile_result.stdout + compile_result.stderr).strip()
            header = f"[verilator rc={compile_result.returncode}]\n"
            if len(out) > OUTPUT_LIMIT:
                out = out[-OUTPUT_LIMIT:]
                header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
            return header + out

        simv = Path(tmp) / "obj_dir" / "simv"
        sim_result = subprocess.run(
            [str(simv)],
            env=eda_env(),
            capture_output=True,
            text=True,
            timeout=SIM_TIMEOUT,
            cwd=tmp,
        )

    out = (sim_result.stdout + sim_result.stderr).strip()
    header = f"[simv rc={sim_result.returncode}]\n"
    if len(out) > OUTPUT_LIMIT:
        out = out[-OUTPUT_LIMIT:]
        header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
    return header + out
