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
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from mcp.server.fastmcp import FastMCP

from common.config import DesignConfig

from .container import OAUTH_TOKEN_ENV, Container, ExecResult

# RTL is staged under this prefix inside each env's workdir
SANDBOX_RTL_ROOT = "rtl"


@dataclass(frozen=True)
class ClaudeResult:
    """`run_claude` output. `line_elapsed_s[i]` is the cumulative wall seconds
    at the end of line `i` of `stdout`."""

    returncode: int
    stdout: str
    stderr: str
    line_elapsed_s: tuple[float, ...]


def _parse_script_timing(typescript: str, timing: str) -> tuple[float, ...]:
    """Cumulative elapsed seconds at the end of each `\\n` in `typescript`."""
    body = typescript
    times: list[float] = []
    if body.startswith("Script started on"):
        nl = body.find("\n")
        if nl >= 0:
            times.append(0.0)
            body = body[nl + 1 :]

    body_bytes = body.encode("utf-8", errors="replace")
    cum = 0.0
    pos = 0
    for raw in timing.splitlines():
        parts = raw.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            delay = float(parts[0])
            nbytes = int(parts[1])
        except ValueError:
            continue
        cum += delay
        end = min(pos + nbytes, len(body_bytes))
        i = pos
        while True:
            j = body_bytes.find(b"\n", i, end)
            if j == -1:
                break
            times.append(cum)
            i = j + 1
        pos = end
    return tuple(times)


@dataclass
class ClaudeEnv:
    """Scoped unix user + home dir inside the shared container, optionally
    bound to a per-env FastMCP that's mounted on the container's shared MCP
    host for the env's lifetime."""

    container: Container
    mcp_factory: Callable[[ClaudeEnv], FastMCP] | None = None
    user: str = field(default_factory=lambda: f"claude-{secrets.token_hex(6)}")
    url: str = field(default="", init=False)

    @property
    def workdir(self) -> str:
        return f"/home/{self.user}"

    @property
    def tmpdir(self) -> str:
        """Per-env $TMPDIR to avoid sibling communication."""
        return f"{self.workdir}/tmp"

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
        )
        self._check(
            self.container.execute(
                ["mkdir", "-p", self.tmpdir],
                user=self.user,
            ),
        )
        if self.mcp_factory is not None:
            self.url = self.container.mcp.mount(self.user, self.mcp_factory(self))
        return self

    def __exit__(self, *_: object) -> None:
        # Unmount this env's FastMCP from the eval-wide MCP host
        if self.url:
            self.container.mcp.unmount(self.user)
            self.url = ""
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
        )
        self._check(
            self.container.execute(
                ["tee", str(path)],
                input=content,
                user=self.user,
            ),
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
        mcp_servers: dict | None = None,
        allowed_tools: tuple[str, ...] = (),
        max_turns: int = 8,
        resume_session: str | None = None,
        timeout: float = 1800.0,
    ) -> ClaudeResult:
        """Run `claude -p` as the env's user."""
        claude_args = [
            "claude",
            "-p",
            prompt,
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
            claude_args += [
                "--mcp-config",
                f"{self.workdir}/mcp.json",
                "--strict-mcp-config",
            ]
        if allowed_tools:
            claude_args += ["--allowedTools", ",".join(allowed_tools)]
        if resume_session:
            claude_args += ["--resume", resume_session]

        # `script` wraps claude in a PTY so Node line-flushes stdout, and
        # writes the typescript to a file inside the container so timeouts
        # don't lose events. `-T` adds timing logs.
        log_rel = "claude.log"
        timing_rel = "claude.timing"
        log_abs = f"{self.workdir}/{log_rel}"
        timing_abs = f"{self.workdir}/{timing_rel}"
        shell_cmd = " ".join(shlex.quote(a) for a in claude_args)
        cmd = ["script", "-q", "-f", "-T", timing_abs, "-c", shell_cmd, log_abs]

        raw = self.container.execute(
            cmd,
            # Isolated sandbox. Forward OAUTH token generated inside container.
            env={
                "IS_SANDBOX": "1",
                "TMPDIR": self.tmpdir,
                "HTTPS_PROXY": "http://127.0.0.1:8888",
                "HTTP_PROXY": "http://127.0.0.1:8888",
                OAUTH_TOKEN_ENV: self.container.oauth_token,
            },
            user=self.user,
            workdir=self.workdir,
            timeout=timeout,
        )

        stdout = self.read_file(log_rel)
        timing = self.read_file(timing_rel)
        return ClaudeResult(
            returncode=raw.returncode,
            stdout=stdout,
            stderr=raw.stderr,
            line_elapsed_s=_parse_script_timing(stdout, timing),
        )

    @staticmethod
    def _check(result: ExecResult) -> None:
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())


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
