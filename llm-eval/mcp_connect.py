"""Network glue for hosting a per-sample MCP server reachable from the sandbox.

The agent runs inside a Docker sandbox and can't see host filesystem state,
but it should still be able to invoke host-side tools (e.g. running the
hidden testbench). We start a small FastMCP server in-process on the host
for each sample, bound to an ephemeral port; the sandbox reaches it on a
Docker bridge network the two containers share.

Server contents are defined in `mcp_servers.py`; this module just handles
network discovery and the uvicorn lifecycle.

Networking note: in a devcontainer (docker-in-docker) setup,
`host.docker.internal` from a sibling sandbox container does NOT resolve to
the devcontainer — it resolves to the Docker VM gateway, which isn't where
this server lives. We instead bind to the devcontainer's own IP on a Docker
bridge network and attach the sandbox to that same network (see
`discover_shared_network`; the network name gets injected into the compose
config by `tasks.py`).
"""
import asyncio
import functools
import socket
import subprocess
from typing import Awaitable, Callable

import uvicorn
from mcp.server.fastmcp import FastMCP

# How long to wait for uvicorn to bind the listener before giving up.
STARTUP_TIMEOUT = 5.0


@functools.cache
def discover_shared_network() -> tuple[str, str]:
    """Return `(network_name, host_ip)` for a Docker network we share with
    sibling containers. Falls back to `bridge` / `host.docker.internal` when
    we can't introspect our own container (e.g. running outside Docker)."""
    fallback = ("bridge", "host.docker.internal")

    hostname = socket.gethostname()
    try:
        out = subprocess.check_output(
            [
                "docker", "inspect", hostname,
                "--format",
                "{{range $k, $v := .NetworkSettings.Networks}}"
                "{{if $v.IPAddress}}{{$k}}\t{{$v.IPAddress}}\n{{end}}"
                "{{end}}",
            ],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        out = ""

    for line in out.splitlines():
        net, sep, ip = line.partition("\t")
        if sep:
            return net, ip
    return fallback


async def serve(mcp: FastMCP) -> tuple[str, Callable[[], Awaitable[None]]]:
    """Serve a FastMCP instance over SSE on a kernel-assigned port.

    Returns `(url, stop)`. `url` is what the agent's MCP client should
    connect to, reachable from the sandbox container over a shared Docker
    bridge network (see `discover_shared_network`). `stop` is an awaitable
    that shuts the server down and reaps its task.

    The current Inspect `sandbox()` contextvar is captured by
    `asyncio.create_task`, so calls into sandbox-aware tool bodies still
    resolve to the right sample's sandbox.
    """
    _, host_ip = discover_shared_network()

    config = uvicorn.Config(
        mcp.sse_app(),
        host="0.0.0.0",
        port=0,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    # uvicorn binds inside serve(); wait until it's actually listening before
    # we can read back the kernel-assigned port.
    deadline = asyncio.get_event_loop().time() + STARTUP_TIMEOUT
    while not server.started:
        if serve_task.done():
            raise RuntimeError(
                f"uvicorn exited before binding: {serve_task.exception()}"
            )
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError("uvicorn failed to start within timeout")
        await asyncio.sleep(0.02)

    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://{host_ip}:{port}/sse"

    async def stop() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except asyncio.TimeoutError:
            server.force_exit = True
            await serve_task

    return url, stop
