"""Single Docker container for the Claude Code agent - one devcontainer for the
whole eval. Context manager: `__enter__` creates+starts the container, `__exit__`
removes it. `execute` shells into it via `docker compose exec` and is intended
to be called inside the `with` block. The runtime-only `shared` network is
injected via a generated override file."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import yaml
from components.mcp_connect import discover_shared_network

from common.config import LLM_EVAL

SANDBOX_DIR = LLM_EVAL / "sandbox"
COMPOSE_FILE = SANDBOX_DIR / "compose.yaml"
COMPOSE_OVERRIDE = SANDBOX_DIR / "compose.override.yaml"  # gitignored
PROJECT = "rtl-wizard"
SERVICE = "solver"
OAUTH_HOME = "/opt/claude-oauth"


@dataclass
class ExecResult:
    """Result of a single `docker compose exec` invocation."""

    returncode: int
    stdout: str
    stderr: str


def _write_override() -> None:
    """Write the runtime-only patches to `compose.override.yaml`. Idempotent."""
    network, _ = discover_shared_network()
    override = {"networks": {"shared": {"name": network}}}
    COMPOSE_OVERRIDE.write_text(yaml.safe_dump(override))


class Container:
    """Context manager wrapper around `docker compose` for the sandbox service."""

    def __enter__(self) -> Container:
        """Build (if needed), create, and start the container."""
        _write_override()
        self._compose("create")
        self._compose("start")

        # /dev/shm is a tmpfs remounted fresh on every container start
        self.execute(["chmod", "700", "/dev/shm"], user="root")

        # Use tinyproxy filter to block everything except anthropic and the MCP server.
        # ClaudeEnv sets HTTPS_PROXY/HTTP_PROXY to http://127.0.0.1:8888 so all of
        # claude's outbound traffic gets filtered here
        _, host_ip = discover_shared_network()
        self.execute(
            ["tee", "/etc/tinyproxy/filter"],
            input=f"^.*\\.anthropic\\.com$\n^{host_ip}$\n",
            user="root",
        )
        self.execute(["tinyproxy"], user="root")

        # Run interactive Claude OAUTH login once per container
        self.execute(["mkdir", "-p", OAUTH_HOME], user="root")
        subprocess.run(
            [
                *self._compose_prefix(), "exec",
                "--user", "root",
                "--env", f"HOME={OAUTH_HOME}",
                SERVICE,
                "claude", "auth", "login",
            ],
            check=True,
        )

        # Make the oauth dir traversable+readable by env users
        self.execute(["chmod", "-R", "a+rX", OAUTH_HOME], user="root")
        return self

    def __exit__(self, *_exc: object) -> None:
        """`docker compose down` -- stops and removes the container."""
        self._compose("down")

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
        proc = subprocess.run(
            args,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
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
            "-f",
            str(COMPOSE_OVERRIDE),
        ]
