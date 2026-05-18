"""Custom Claude Code agent driven by an OAUTH token, not an API key.

The agent shells out to `claude -p` inside the sandbox with
`--output-format stream-json --verbose` so we capture the full per-event
transcript (init / assistant turns / tool uses / tool results / final result),
not just the summary returned by `--output-format json`. The raw JSONL is
stashed in `store()` under `cc_transcript_jsonl` so the scorer can persist it
to disk alongside the diff.

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


def _parse_stream_json(stdout: str) -> list[dict]:
    """Parse line-delimited JSON events from `claude --output-format stream-json`.

    Lines that fail to parse are skipped silently — they're typically warnings
    or progress text the CLI emits outside the JSON stream, not events we'd
    fail the run over.
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


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
            "--output-format", "stream-json", "--verbose",
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
            raise RuntimeError(
                f"claude failed (rc={result.returncode}): {result.stderr[-2000:]}"
            )

        events = _parse_stream_json(result.stdout)
        if not events:
            raise RuntimeError(
                f"claude produced no JSON events. First 2KB of stdout:\n{result.stdout[:2048]}"
            )

        # Stash the raw JSONL so the scorer can write it to outputs/<ts>/<id>/
        # transcript.jsonl. We keep parsing here (instead of just dumping stdout)
        # to drop blank lines / non-JSON noise that would make replay tools
        # choke later.
        store().set(
            "cc_transcript_jsonl",
            "\n".join(json.dumps(e) for e in events) + "\n",
        )

        # The final `result` event carries the same summary fields as the
        # one-shot `--output-format json` mode (result text, session id, usage,
        # cost, num_turns). It is always the last event in a successful run.
        final = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if final is None:
            raise RuntimeError("claude transcript contained no `result` event")
        if final.get("is_error"):
            raise RuntimeError(f"claude reported error: {final.get('result')}")

        store().set("cc_session_id", final.get("session_id"))
        store().set("cc_usage", final.get("usage"))
        store().set("cc_cost_usd", final.get("total_cost_usd"))
        store().set("cc_num_turns", final.get("num_turns"))

        state.output = ModelOutput.from_content(
            model="claude-code", content=final["result"]
        )
        state.messages.append(state.output.message)
        return state

    return execute


def claude_code_solver():
    """Adapt the OAUTH agent into a Task.solver slot."""
    return as_solver(claude_code_oauth())
