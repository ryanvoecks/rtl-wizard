"""Host one eval-wide uvicorn server with per-sample FastMCPs mounted at
unique sub-paths. Single-server is load-bearing: `sse_starlette`'s
`AppStatus.should_exit` is process-global, so a per-sample teardown would
broadcast shutdown to every other sample's in-flight SSE responses. We bind
on the Docker network IP since `host.docker.internal` doesn't resolve to us
from sibling sandboxes."""

import asyncio
import functools
import socket
import subprocess
import threading

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

# How long to wait for uvicorn to bind the listener before giving up.
STARTUP_TIMEOUT = 5.0


class SuppressAbortResponse:
    """ASGI middleware that suppresses the spurious traceback uvicorn logs
    when an SSE response is aborted mid-stream by a client disconnect."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        broken = False

        async def wrapped_send(message):
            nonlocal started, broken
            if broken:
                return
            if message["type"] == "http.response.start":
                if started:
                    broken = True
                    return
                started = True
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception:
            if not started:
                raise


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
    """Long-lived uvicorn server with mountable per-env FastMCP sub-apps. One
    instance per eval, owned by `Container`."""

    def __init__(self) -> None:
        self._app = Starlette(routes=[])
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._base_url: str = ""
        self._lock = threading.Lock()  # Mount/unmounts must be serialised

    async def __aenter__(self) -> "MCPService":
        host_ip = discover_host_ip()
        config = uvicorn.Config(
            self._app,
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
        self._base_url = f"http://{host_ip}:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None and self._task is not None
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._server.force_exit = True
            await self._task

    def mount(self, env_id: str, mcp: FastMCP) -> str:
        """Mount `mcp` at `/<env_id>` and return its SSE URL. Lets every
        sample share one uvicorn so `sse_starlette`'s global shutdown flag
        doesn't fire mid-eval."""
        if not self._base_url:
            raise RuntimeError("MCPService is not started")
        mount_path = f"/{env_id}"
        sub_app = SuppressAbortResponse(mcp.sse_app())
        with self._lock:
            self._app.routes[:] = [
                r
                for r in self._app.routes
                if not (isinstance(r, Mount) and r.path == mount_path)
            ]
            self._app.routes.append(Mount(mount_path, app=sub_app))
        return f"{self._base_url}{mount_path}/sse"

    def unmount(self, env_id: str) -> None:
        mount_path = f"/{env_id}"
        with self._lock:
            self._app.routes[:] = [
                r
                for r in self._app.routes
                if not (isinstance(r, Mount) and r.path == mount_path)
            ]
