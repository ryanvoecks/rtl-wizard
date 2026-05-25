"""Per-session Claude Code workspace inside the shared `Container`. Multiple
ClaudeEnvs can run in parallel in one devcontainer, all backed by the same
OAUTH token (the point: share one Claude Code session across N parallel
`claude -p` calls). Each env is its own unix user with a private home dir;
unix permissions isolate one env's workdir from another's. The container
must already be running -- lifecycle is owned by `llm_eval/run.py`."""

from __future__ import annotations

import difflib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from container import Container, ExecResult

from common.config import DesignConfig

# RTL is staged under this prefix inside each env's workdir
SANDBOX_RTL_ROOT = "rtl"


@dataclass
class ClaudeEnv:
    """Scoped unix user + home dir inside the shared container."""

    container: Container
    user: str = field(default_factory=lambda: f"claude-{secrets.token_hex(6)}")

    @property
    def workdir(self) -> str:
        return f"/home/{self.user}"

    def __enter__(self) -> ClaudeEnv:
        # HOME_MODE=0700 prevents sibling envs seeing each others contents
        self._check(
            self.container.execute(
                [
                    "useradd",
                    "-m",
                    "-d",
                    self.workdir,
                    "-K",
                    "HOME_MODE=0700",
                    "-s",
                    "/bin/bash",
                    self.user,
                ],
                user="root",
            ),
            "useradd",
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        # -f -r: force-remove even if processes are still running
        self.container.execute(
            ["userdel", "-f", "-r", self.user],
            user="root",
        )

    def write_file(self, rel_path: str, content: str) -> None:
        """Write a file under the workdir, owned by the env's user."""
        path = PurePosixPath(self.workdir) / rel_path
        self._check(
            self.container.execute(
                ["mkdir", "-p", str(path.parent)],
                user=self.user,
            ),
            f"mkdir for {rel_path}",
        )
        self._check(
            self.container.execute(
                ["tee", str(path)],
                input=content,
                user=self.user,
            ),
            f"write_file({rel_path})",
        )

    def read_file(self, rel_path: str) -> str:
        """Read a file from the workdir. Returns '' on missing/error."""
        path = PurePosixPath(self.workdir) / rel_path
        return self.container.execute(
            ["cat", str(path)],
            user=self.user,
        ).stdout

    def run_claude(
        self,
        prompt: str,
        *,
        model: str,
        oauth_token: str,
        mcp_servers: dict | None = None,
        allowed_tools: tuple[str, ...] = (),
        max_turns: int = 8,
        resume_session: str | None = None,
        timeout: float = 1800.0,
    ) -> ExecResult:
        """Run `claude -p` as the env's user with `prompt` on stdin."""
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--dangerously-skip-permissions",
        ]
        if mcp_servers is not None:
            self.write_file("mcp.json", json.dumps({"mcpServers": mcp_servers}))
            cmd += [
                "--mcp-config",
                f"{self.workdir}/mcp.json",
                "--strict-mcp-config",
            ]
        if allowed_tools:
            cmd += ["--allowedTools", ",".join(allowed_tools)]
        if resume_session:
            cmd += ["--resume", resume_session]
        return self.container.execute(
            cmd,
            input=prompt,
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
                "IS_SANDBOX": "1",
            },
            user=self.user,
            workdir=self.workdir,
            timeout=timeout,
        )

    @staticmethod
    def _check(result: ExecResult, what: str) -> None:
        if result.returncode != 0:
            raise RuntimeError(f"{what} failed: {result.stderr.strip()}")


def build_diff_from_env(env: ClaudeEnv, design: DesignConfig) -> str:
    """Unified diff of the env's staged RTL vs `design`'s on-disk originals."""
    parts: list[str] = []
    for rel_file, abs_file in zip(design.rtl_files, design.rtl_abs_paths):
        original = abs_file.read_text()
        final = env.read_file(f"{SANDBOX_RTL_ROOT}/{rel_file}")
        parts.append(
            "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    final.splitlines(keepends=True),
                    fromfile=f"a/{rel_file}",
                    tofile=f"b/{rel_file}",
                )
            )
        )
    return "".join(parts)
