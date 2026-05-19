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
bridge network and attach the sandbox to that same network via compose's
`networks:` block (see `_discover_shared_network`).
"""
import asyncio
import os
import socket
import subprocess
from typing import Awaitable, Callable

import uvicorn
from mcp.server.fastmcp import FastMCP

# How long to wait for uvicorn to bind the listener before giving up.
_STARTUP_TIMEOUT_S = 5.0

# Env vars used to plumb network info into the docker compose file so the
# sandbox container attaches to a network we (the devcontainer) live on.
_NETWORK_ENV = "RTL_WIZARD_SHARED_NETWORK"
_HOST_IP_ENV = "RTL_WIZARD_HOST_IP"


def _discover_shared_network() -> tuple[str, str]:
    """Find a Docker network we sit on and our IP on it, so we can publish
    an MCP URL that a sibling sandbox container can reach.

    Returns `(network_name, host_ip)`. When we can't introspect our own
    container (e.g. running outside Docker), falls back to the always-present
    `bridge` network and `host.docker.internal`, which works on Docker
    Desktop's standard `extra_hosts: host-gateway` mapping.

    Sets `RTL_WIZARD_SHARED_NETWORK` / `RTL_WIZARD_HOST_IP` in the env so the
    compose file (which is read by Inspect when launching the sandbox) sees
    the same values without having to redo the work.
    """
    cached_net = os.environ.get(_NETWORK_ENV)
    cached_ip = os.environ.get(_HOST_IP_ENV)
    if cached_net and cached_ip:
        return cached_net, cached_ip

    fallback = ("bridge", "host.docker.internal")

    hostname = socket.gethostname()
    try:
        out = subprocess.check_output(
            [
                "docker", "inspect", hostname,
                "--format",
                # one "<network>\t<ip>" line per network, skipping empties
                "{{range $k, $v := .NetworkSettings.Networks}}"
                "{{if $v.IPAddress}}{{$k}}\t{{$v.IPAddress}}\n{{end}}"
                "{{end}}",
            ],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        out = ""

    entries = [
        tuple(line.split("\t", 1))
        for line in out.splitlines()
        if "\t" in line
    ]
    network, ip = entries[0] if entries else fallback
    os.environ[_NETWORK_ENV] = network
    os.environ[_HOST_IP_ENV] = ip
    return network, ip


# Eagerly populate the env vars so they're set before Inspect renders the
# compose file (which references `${RTL_WIZARD_SHARED_NETWORK}`).
_discover_shared_network()


async def serve(mcp: FastMCP) -> tuple[str, Callable[[], Awaitable[None]]]:
    """Serve a FastMCP instance over SSE on a kernel-assigned port.

    Returns `(url, stop)`. `url` is what the agent's MCP client should
    connect to, reachable from the sandbox container over a shared Docker
    bridge network (see `_discover_shared_network`). `stop` is an awaitable
    that shuts the server down and reaps its task.

    The current Inspect `sandbox()` contextvar is captured by
    `asyncio.create_task`, so calls into sandbox-aware tool bodies still
    resolve to the right sample's sandbox.
    """
    _, host_ip = _discover_shared_network()

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
    deadline = asyncio.get_event_loop().time() + _STARTUP_TIMEOUT_S
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
