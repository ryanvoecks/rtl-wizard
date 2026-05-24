"""Single long-lived Docker container for the Claude Code agent - one devcontainer for
the whole eval. Stateless wrapper around `docker compose` on the fixed PROJECT/SERVICE;
the runtime-only `shared` network is injected via a generated override file."""

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
    """Lifecycle wrapper around `docker compose` for the sandbox service."""

    def create(self) -> None:
        """`docker compose create` (builds the image if missing)."""
        _write_override()
        self._compose("create")

    def start(self) -> None:
        """`docker compose start` the created container."""
        _write_override()
        self._compose("start")

    def execute(
        self,
        cmd: list[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """`docker compose exec` a command in the running container."""
        _write_override()
        # -T disables TTY allocation so this works from non-interactive callers.
        proc = subprocess.run(
            [*self._compose_prefix(), "exec", "-T", SERVICE, *cmd],
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

    def stop(self) -> None:
        """`docker compose stop` (stops container)."""
        _write_override()
        self._compose("stop")

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
