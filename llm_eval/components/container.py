"""Single Docker container for the Claude Code agent - one devcontainer for the
whole eval. Async context manager: `__aenter__` builds the container only if it
doesn't already exist (so the OAUTH login persists across runs) and brings up
the eval-wide MCPService; `__aexit__` stops the MCPService and the container
but keeps the container record around for next time. Use `teardown()` to fully
remove the container (forcing a rebuild + re-auth on the next entry).
`execute` shells into it via `docker compose exec` and is intended to be called
inside the `async with` block."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from common.config import LLM_EVAL

from .mcp_connect import MCPService, discover_host_ip

# Setup config
SANDBOX_DIR = LLM_EVAL / "sandbox"
COMPOSE_FILE = SANDBOX_DIR / "compose.yaml"
PROJECT = "rtl-wizard"
SERVICE = "solver"
OAUTH_HOME = "/opt/claude-oauth"

# Conventional GNU `timeout` exit code
TIMEOUT_RC = 124


@dataclass
class ExecResult:
    """Result of a single `docker compose exec` invocation."""

    returncode: int
    stdout: str
    stderr: str


class Container:
    """Async context manager wrapping `docker compose` for the sandbox service
    plus the eval-wide `MCPService` (so the uvicorn server lives for the entire
    eval and `sse_starlette`'s process-global shutdown flag never fires
    mid-eval)."""

    def __init__(self) -> None:
        self.mcp = MCPService()

    async def __aenter__(self) -> Container:
        """Build the container if missing, otherwise just restart the existing one."""
        self._compose("create")
        self._compose("start")

        # /dev/shm is a tmpfs remounted fresh on every container start
        self.execute(["chmod", "700", "/dev/shm"], user="root")

        # Use tinyproxy filter to block everything except anthropic and the MCP server.
        # ClaudeEnv sets HTTPS_PROXY/HTTP_PROXY to http://127.0.0.1:8888 so all of
        # claude's outbound traffic gets filtered here
        host_ip = discover_host_ip()
        self.execute(
            ["tee", "/etc/tinyproxy/filter"],
            input=f"^.*\\.anthropic\\.com$\n^{host_ip}$\n",
            user="root",
        )
        self.execute(["tinyproxy"], user="root")

        # OAUTH login: only the first time.
        self.execute(["mkdir", "-p", OAUTH_HOME], user="root")
        if not self._oauth_ready():
            subprocess.run(
                [
                    *self._compose_prefix(),
                    "exec",
                    "--user",
                    "root",
                    "--env",
                    f"HOME={OAUTH_HOME}",
                    SERVICE,
                    "claude",
                    "auth",
                    "login",
                ],
                check=True,
            )

        # Make the OAUTH dir traversable+readable by env users
        self.execute(["chmod", "-R", "a+rX", OAUTH_HOME], user="root")

        # Bring up the shared MCP host inside the eval's asyncio loop.
        await self.mcp.__aenter__()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Tear down the MCP host and stop the container."""
        await self.mcp.__aexit__()
        self._compose("stop")

    def teardown(self) -> None:
        """Remove the container entirely. Forces a rebuild."""
        self._compose("down")

    def _oauth_ready(self) -> bool:
        """True if a prior `claude auth login` left credentials in OAUTH_HOME."""
        return (
            self.execute(
                ["test", "-d", f"{OAUTH_HOME}/.claude"],
                user="root",
            ).returncode
            == 0
        )

    def execute(
        self,
        cmd: list[str],
        *,
        input: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        workdir: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """`docker compose exec` a command in the running container."""
        # -T disables TTY allocation so this works from non-interactive callers.
        args: list[str] = [*self._compose_prefix(), "exec", "-T"]
        if user is not None:
            args += ["--user", user]
        if workdir is not None:
            args += ["--workdir", workdir]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        args += [SERVICE, *cmd]
        try:
            proc = subprocess.run(
                args,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            # Recover partial output buffered up to the kill
            return ExecResult(
                returncode=TIMEOUT_RC,
                stdout=str(e.stdout or ""),
                stderr=str(e.stderr or ""),
            )
        return ExecResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def _compose(self, *args: str) -> None:
        result = subprocess.run(
            [*self._compose_prefix(), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker compose {args[0]} failed: {result.stderr.strip()}"
            )

    @staticmethod
    def _compose_prefix() -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(COMPOSE_FILE),
        ]
