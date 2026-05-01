import shutil
import subprocess
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("yosys-runner")

YOSYS_BIN = shutil.which("yosys") or "yosys"
OUTPUT_LIMIT = 20_000


@mcp.tool()
def yosys_synth(verilog_path: str, top: str | None = None) -> str:
    """Synthesize a Verilog/SystemVerilog file with yosys and return cell stats.

    Use this to check that RTL is synthesizable and to read the area/cell stats
    reported at the end. The cell count and breakdown are a direct proxy for
    area; fewer sequential cells and shallower logic usually mean better
    performance and power too. Call after simulation passes and iterate on the
    RTL to bring the cell/wire counts down while keeping the testbench green.

    Args:
        verilog_path: absolute or cwd-relative path to the .v / .sv file.
        top: top-module name. Defaults to the filename stem.

    Returns the final `stat` block (wire/cell counts). On synthesis failure,
    returns the tail of the yosys log so the error is visible.
    """
    src = Path(verilog_path).expanduser()
    if not src.is_file():
        return f"error: file not found: {src}"

    top_name = top or src.stem
    script = (
        f"read_verilog -sv {src}; "
        f"hierarchy -check -top {top_name}; "
        "proc; opt; fsm; opt; memory; opt; "
        "techmap; opt; "
        "stat"
    )

    # TODO: Should we really pop the path if there's no original? Original is a sign of PyInstaller
    env = os.environ.copy()
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    result = subprocess.run(
        [YOSYS_BIN, "-p", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    out = (result.stdout + result.stderr).strip()
    header = f"[yosys rc={result.returncode} top={top_name}]\n"

    if result.returncode != 0:
        if len(out) > OUTPUT_LIMIT:
            out = out[-OUTPUT_LIMIT:]
            header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
        return header + out

    stats = _extract_stats(result.stdout)
    if stats is None:
        return header + "error: could not locate stat block in yosys output\n" + out[-2000:]
    return header + stats


def _extract_stats(stdout: str) -> str | None:
    """Return every per-module and design-hierarchy `stat` block, trimmed of yosys's post-script log lines."""
    lines = stdout.splitlines()

    start = None
    for i, line in enumerate(lines):
        if "Printing statistics" in line:
            start = i + 1
            break
    if start is None:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("===") and stripped.endswith("==="):
                start = i
                break
    if start is None:
        return None

    kept: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("End of script") or stripped.startswith("CPU:"):
            break
        kept.append(line)

    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()

    if not kept:
        return None
    return "\n".join(kept)


if __name__ == "__main__":
    mcp.run()
