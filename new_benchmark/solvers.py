"""Custom Claude Code agent driven by an OAUTH token, not an API key.

The agent shells out to `claude -p` inside the sandbox, parses its JSON output,
and stashes session/usage/cost in `store()` to recover the observability we
otherwise lose by going outside Inspect's native ModelEvents.

Token handling
--------------
The host's `CLAUDE_CODE_OAUTH_TOKEN` (generated once via `claude setup-token`)
is read from the Inspect process environment at factory-call time, and injected
into the sandbox per `sb.exec(..., env={...})`. It is deliberately *not* set in
compose.yaml, the Dockerfile, or as a container env var — that keeps it out of
image layers and `docker inspect`. It does land in `/proc/<pid>/environ` inside
the sandbox during the run, which is fine for a throwaway eval sandbox but
worth knowing if the task under test could exfiltrate.

ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL are explicitly NOT forwarded — Claude
Code prefers an API key over the OAUTH token when both are present.
"""
import json
import os

from inspect_ai.agent import AgentState, agent, as_solver
from inspect_ai.model import ModelOutput
from inspect_ai.util import sandbox, store

_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _require_token() -> str:
    tok = os.environ.get(_TOKEN_ENV_VAR)
    if not tok:
        raise RuntimeError(
            f"{_TOKEN_ENV_VAR} is not set. Run `claude setup-token` on the host, "
            f"then `export {_TOKEN_ENV_VAR}=<token>` before `inspect eval`."
        )
    return tok


@agent
def claude_code_oauth(
    mcp_config: dict | None = None,
    model: str = "claude-sonnet-4-5",
    allowed_tools: str = "Bash,Read,Write,Edit",
    timeout_s: int = 1800,
):
    token = _require_token()
    mcp_config = mcp_config if mcp_config is not None else {"mcpServers": {}}

    async def execute(state: AgentState) -> AgentState:
        sb = sandbox()
        await sb.write_file("/tmp/mcp.json", json.dumps(mcp_config))

        prompt = state.messages[-1].text
        sid = store().get("cc_session_id")

        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--model", model,
            "--mcp-config", "/tmp/mcp.json", "--strict-mcp-config",
            "--allowedTools", allowed_tools,
            "--dangerously-skip-permissions",
        ]
        if sid:
            cmd += ["--resume", sid]

        result = await sb.exec(
            cmd,
            input=prompt,
            env={
                _TOKEN_ENV_VAR: token,
                "IS_SANDBOX": "1",
            },
            timeout=timeout_s,
        )
        if not result.success:
            raise RuntimeError(f"claude failed (rc={result.returncode}): {result.stderr}")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude returned non-JSON stdout (first 2KB):\n{result.stdout[:2048]}"
            ) from e

        if data.get("is_error"):
            raise RuntimeError(f"claude reported error: {data.get('result')}")

        store().set("cc_session_id", data.get("session_id"))
        store().set("cc_usage", data.get("usage"))
        store().set("cc_cost_usd", data.get("total_cost_usd"))
        store().set("cc_num_turns", data.get("num_turns"))

        state.output = ModelOutput.from_content(
            model="claude-code", content=data["result"]
        )
        state.messages.append(state.output.message)
        return state

    return execute


def claude_code_solver():
    """Adapt the OAUTH agent into a Task.solver slot."""
    return as_solver(claude_code_oauth())
