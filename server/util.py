"""Shared helpers for the rtl-wizard MCP server."""

import os


def eda_env() -> dict[str, str]:
    """Environment for subprocesses using EDA tools.

    The inspect-tool-support base image sets LD_LIBRARY_PATH to its bundled
    Python venv libs, which breaks EDA tools that link against system libs.
    PyInstaller bootstrappers stash the original value in LD_LIBRARY_PATH_ORIG
    when they take over the variable; if that stash exists, restore from it,
    otherwise drop LD_LIBRARY_PATH entirely so tools runs against system libs.
    """
    env = os.environ.copy()
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env
