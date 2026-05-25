"""Host the per-sample MCP server (defined in `mcp_servers.py`) for the
sandbox to reach over a shared Docker bridge network. In a devcontainer
setup `host.docker.internal` from a sibling sandbox resolves to the Docker
VM gateway, not us, so we bind to our own IP on the shared network and
inject that network name into the sandbox compose config via `tasks.py`."""

import asyncio
import functools
import socket
import subprocess

import uvicorn
from mcp.server.fastmcp import FastMCP

# How long to wait for uvicorn to bind the listener before giving up.
STARTUP_TIMEOUT = 5.0


@functools.cache
def discover_host_ip() -> str:
    """Return our IP on a Docker network shared with sibling containers.
    Falls back to `host.docker.internal` when we can't introspect our own
    container (e.g. running outside Docker)."""
    hostname = socket.gethostname()
    try:
        out = subprocess.check_output(
            [
                "docker",
                "inspect",
                hostname,
                "--format",
                "{{range $v := .NetworkSettings.Networks}}"
                "{{if $v.IPAddress}}{{$v.IPAddress}}\n{{end}}"
                "{{end}}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        out = ""

    for line in out.splitlines():
        if line.strip():
            return line.strip()
    return "host.docker.internal"


class MCPService:
    """Async context manager that serves a FastMCP over SSE on a
    kernel-assigned port for the duration of the `async with` block."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.url: str = ""

    async def __aenter__(self) -> "MCPService":
        host_ip = discover_host_ip()
        config = uvicorn.Config(
            self._mcp.sse_app(),
            host="0.0.0.0",
            port=0,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        # uvicorn binds inside serve(); wait until it's actually listening
        # before we can read back the kernel-assigned port.
        deadline = asyncio.get_event_loop().time() + STARTUP_TIMEOUT
        while not self._server.started:
            if self._task.done():
                raise RuntimeError(
                    f"uvicorn exited before binding: {self._task.exception()}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("uvicorn failed to start within timeout")
            await asyncio.sleep(0.02)

        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://{host_ip}:{port}/sse"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None and self._task is not None
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._server.force_exit = True
            await self._task
