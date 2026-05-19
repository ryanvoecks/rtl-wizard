"""Per-sample host-side MCP server exposing `run_testbench` to the agent.

The agent runs inside a Docker sandbox and cannot see the testbench code, but
it should still be able to check whether its RTL edits pass the golden tests.
We start a small FastMCP server in-process on the host for each sample, bound
to an ephemeral port; the sandbox reaches it on a Docker bridge network the
two containers share.

The single tool, `run_testbench`, calls the existing `evaluate_testbench`
from `scorers.py`, which copies `design.root` to a host tempdir, applies the
agent's current diff, and runs `design.run_tb` there. Only the testbench's
exit code and stdout cross back into the sandbox; the testbench sources never
do.

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
from mcp.server.transport_security import TransportSecuritySettings

from common.config import DesignConfig
from scorers import build_diff_from_sandbox, evaluate_testbench

# Matches the rtl_wizard MCP server's truncation budget so the agent's
# context isn't blown out by a chatty testbench.
OUTPUT_LIMIT = 20_000

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


async def start_run_testbench_server(
    design: DesignConfig,
) -> tuple[str, Callable[[], Awaitable[None]]]:
    """Start an SSE MCP server bound to this sample.

    Returns `(url, stop)`. `url` is what the agent's MCP client should
    connect to, reachable from the sandbox container over a shared Docker
    bridge network (see `_discover_shared_network`). `stop` is an awaitable
    that shuts the server down and reaps its task.

    The current Inspect `sandbox()` contextvar is captured by
    `asyncio.create_task`, so calls into `build_diff_from_sandbox` from the
    tool body still resolve to the right sample's sandbox.
    """
    _, host_ip = _discover_shared_network()
    # FastMCP defaults to DNS-rebinding protection that only allow-lists
    # 127.0.0.1/localhost/[::1]. The sandbox reaches us at
    # `host.docker.internal:<port>`, so without an explicit allow-list the
    # SSE handshake is rejected before any tool is registered — and the
    # rejection is silent on the CLI side (no stderr, no `mcp__*` tool ever
    # appears). The server only lives for the duration of one sample on a
    # kernel-assigned port, so we just disable the check.
    mcp = FastMCP(
        "rtl-wizard-host",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    @mcp.tool()
    async def run_testbench() -> str:
        """Run the design's hidden testbench against your current RTL.

        Reads your current files out of the sandbox, applies them on top of
        a host-side copy of the design, runs the upstream testbench, and
        returns its exit code and stdout. The testbench sources themselves
        are never sent into your environment. A return code of 0 means the
        testbench passed.
        """
        try:
            diff = await build_diff_from_sandbox(design)
            log, rc = await asyncio.to_thread(evaluate_testbench, design, diff)
        except Exception as e:
            return f"[tool error] {type(e).__name__}: {e}"

        header = f"[rc={rc}]\n"
        if len(log) > OUTPUT_LIMIT:
            log = log[-OUTPUT_LIMIT:]
            header += f"[output truncated to last {OUTPUT_LIMIT} chars]\n"
        return header + log

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
