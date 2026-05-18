"""Custom Claude Code agent driven by an OAUTH token, not an API key.

Shells out to `claude -p --output-format stream-json --verbose` inside the
sandbox to capture the full per-event transcript, then translates each event
into Inspect chat-message form so `state.messages` carries the whole agent
trajectory (assistant turns + tool calls + tool results) and the .eval log is
viewable in `inspect view` without further conversion.

`CLAUDE_CODE_OAUTH_TOKEN` is read from the Inspect process env and injected
per `sb.exec(env=...)` — never baked into the image or compose file.
ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL are deliberately NOT forwarded, since
Claude Code prefers an API key over the OAUTH token when both are present.
"""
import json
import os

from inspect_ai.agent import AgentState, agent, as_solver
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.tool import ToolCall, ToolCallError
from inspect_ai.util import LimitExceededError, sandbox, store

_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Default model the agent runs through `claude --model`. Exported here so the
# Task can declare it via `Task(model=...)` and Inspect's viewer labels the run
# with the actual model rather than whatever `INSPECT_EVAL_MODEL` is set to
# in .env (which targets the unrelated rtl-wizard benchmark).
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


def _require_token() -> str:
    tok = os.environ.get(_TOKEN_ENV_VAR)
    if not tok:
        raise RuntimeError(
            f"{_TOKEN_ENV_VAR} is not set. Run `claude setup-token` on the host, "
            f"then `export {_TOKEN_ENV_VAR}=<token>` before `inspect eval`."
        )
    return tok


def _parse_stream_json(stdout: str) -> list[dict]:
    """Parse line-delimited JSON events from `claude --output-format stream-json`."""
    events: list[dict] = []
    for lineno, raw in enumerate(stdout.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude stdout line {lineno} is not valid JSON ({e}). "
                f"Preview (first 200 chars): {line[:200]!r}"
            ) from e
    return events


def _tool_result_text(content) -> str:
    """Flatten a tool_result `content` field (str | list[block]) to plain text.

    Anthropic-style tool_result blocks can carry either a string or a list of
    content blocks (text/image/etc.). The viewer expects plain text on
    ChatMessageTool, so we concatenate text parts and stringify the rest."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block))
        return "".join(parts)
    return str(content)


def _events_to_messages(events: list[dict]) -> list[ChatMessage]:
    """Convert stream-json events into Inspect ChatMessage objects.

    `claude -p --output-format stream-json` emits one event *per content block*,
    not per assistant message — a tool-using turn typically arrives as three
    events sharing one `message.id`: a `thinking` block, a `text` block, then a
    `tool_use` block. Treating each event as its own message produces a blank
    `ChatMessageAssistant` for the thinking block (it has neither text nor
    tool_use). We aggregate by `message.id` so one logical turn becomes one
    message with text + tool_calls combined.

    `user` event -> one ChatMessageTool per tool_result block (parallel tool
    calls in a single turn produce multiple tool_result blocks).

    `system` / `result` events are not messages; they're handled separately."""
    messages: list[ChatMessage] = []
    # Track the in-flight assistant message by id so subsequent events with the
    # same id append to it rather than producing a fresh (often-empty) message.
    asst_by_id: dict[str, ChatMessageAssistant] = {}
    for event in events:
        etype = event.get("type")
        if etype == "assistant":
            msg = event.get("message") or {}
            msg_id = msg.get("id")
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            function=block.get("name", ""),
                            arguments=block.get("input") or {},
                        )
                    )
                # `thinking` and any other future block types are intentionally
                # dropped — they carry no chat-visible content.
            existing = asst_by_id.get(msg_id) if msg_id else None
            if existing is not None:
                if text_parts:
                    existing.content = (existing.content or "") + "".join(text_parts)
                if tool_calls:
                    existing.tool_calls = (existing.tool_calls or []) + tool_calls
            else:
                new_msg = ChatMessageAssistant(
                    id=msg_id,
                    content="".join(text_parts),
                    tool_calls=tool_calls or None,
                    model=msg.get("model"),
                )
                if msg_id:
                    asst_by_id[msg_id] = new_msg
                messages.append(new_msg)
        elif etype == "user":
            msg = event.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _tool_result_text(block.get("content"))
                error = (
                    ToolCallError(type="unknown", message=text)
                    if block.get("is_error")
                    else None
                )
                messages.append(
                    ChatMessageTool(
                        content=text,
                        tool_call_id=block.get("tool_use_id"),
                        error=error,
                    )
                )
    return messages


def _build_usage(final: dict) -> ModelUsage:
    u = final.get("usage") or {}
    input_t = int(u.get("input_tokens", 0) or 0)
    output_t = int(u.get("output_tokens", 0) or 0)
    return ModelUsage(
        input_tokens=input_t,
        output_tokens=output_t,
        total_tokens=input_t + output_t,
        input_tokens_cache_write=u.get("cache_creation_input_tokens"),
        input_tokens_cache_read=u.get("cache_read_input_tokens"),
        total_cost=final.get("total_cost_usd"),
    )


@agent
def claude_code_oauth(
    mcp_config: dict | None = None,
    model: str = DEFAULT_CLAUDE_MODEL,
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

        # The final `result` event carries the aggregate summary (result text,
        # session id, usage, cost, num_turns). Always last in a successful run.
        final = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if final is None:
            raise RuntimeError("claude transcript contained no `result` event")
        if final.get("is_error"):
            raise RuntimeError(f"claude reported error: {final.get('result')}")

        # Append one-by-one so a message_limit firing mid-transcript preserves
        # everything we've recorded so far. `ChatMessageList.extend` is atomic:
        # it checks the *projected* total before adding any items, so a single
        # `extend` call that would overflow the limit drops the entire batch.
        parsed = _events_to_messages(events)
        limit_hit: LimitExceededError | None = None
        for m in parsed:
            try:
                state.messages.append(m)
            except LimitExceededError as e:
                limit_hit = e
                break

        usage = _build_usage(final)
        last_assistant = next(
            (m for m in reversed(parsed) if isinstance(m, ChatMessageAssistant)),
            None,
        )
        if last_assistant is not None:
            state.output = ModelOutput(
                model=model,
                choices=[
                    ChatCompletionChoice(message=last_assistant, stop_reason="stop")
                ],
                usage=usage,
            )
        else:
            # Defensive: no assistant events seen. Fall back to the result text.
            state.output = ModelOutput.from_content(
                model=model, content=final.get("result", "")
            )
            state.output.usage = usage

        # Keep a few extras in `store()` that don't fit on ModelUsage but are
        # useful for downstream analysis (session resume, turn counts).
        store().set("cc_session_id", final.get("session_id"))
        store().set("cc_num_turns", final.get("num_turns"))
        store().set("cc_duration_ms", final.get("duration_ms"))

        # Re-raise after state.messages / state.output are populated so the
        # transcript is preserved in the log; Inspect's solver wrapper records
        # the limit and ends the sample as usual.
        if limit_hit is not None:
            raise limit_hit
        return state

    return execute


def claude_code_solver(model: str = DEFAULT_CLAUDE_MODEL):
    """Adapt the OAUTH agent into a Task.solver slot."""
    return as_solver(claude_code_oauth(model=model))
