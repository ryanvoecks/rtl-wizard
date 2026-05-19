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

from inspect_ai.agent import AgentState, agent
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.model._model import active_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall, ToolCallError
from inspect_ai.util import LimitExceededError, sandbox, store

from common.config import DesignConfig
from .mcp_connect import MCPService
from .mcp_servers import make_server

# Claude config
TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULT_TOOLS = ["Bash", "Read", "Write", "Edit"]

# MCP config
HOST_MCP_NAME = "rtl-wizard-host"
MCP_TOOLS = ["run_testbench"]


def _resolve_model() -> str:
    """Bare model name to pass to `claude --model`, sourced from Inspect's
    `--model` flag (or `INSPECT_EVAL_MODEL`) at solve time."""
    m = active_model()
    if m is None or m.name in ("none", ""):
        raise ValueError("model name is not set")
    return m.name


def _require_token() -> str:
    tok = os.environ.get(TOKEN_ENV_VAR)
    if not tok:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set. Run `claude setup-token` on the host, "
            f"then `export {TOKEN_ENV_VAR}=<token>` before `inspect eval`."
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
    design: DesignConfig,
    timeout: int = 1800,
    max_turns: int = 8,
):
    token = _require_token()

    async def execute(state: AgentState) -> AgentState:
        # Fetch model name and sandbox
        model = _resolve_model()
        sb = sandbox()

        # MCP server and tools config
        host_mcp = make_server(HOST_MCP_NAME, MCP_TOOLS, design)
        mcp_tool_names = [f"mcp__{HOST_MCP_NAME}__{tool}" for tool in MCP_TOOLS]
        allowed_tools = ",".join(DEFAULT_TOOLS + mcp_tool_names)

        # Start MCP server and run Claude Code CLI
        async with MCPService(host_mcp) as host_service:
            mcp_config = {
                "mcpServers": {
                    HOST_MCP_NAME: {"type": "sse", "url": host_service.url},
                }
            }
            await sb.write_file("/tmp/mcp.json", json.dumps(mcp_config))

            prompt = state.messages[-1].text
            sid = store().get("cc_session_id")

            cmd = [
                "claude", "-p",
                "--output-format", "stream-json", "--verbose",
                "--model", model,
                "--mcp-config", "/tmp/mcp.json", "--strict-mcp-config",
                "--allowedTools", allowed_tools,
                "--max-turns", str(max_turns),
                "--dangerously-skip-permissions",
            ]
            if sid:
                cmd += ["--resume", sid]

            result = await sb.exec(
                cmd,
                input=prompt,
                env={
                    TOKEN_ENV_VAR: token,
                    "IS_SANDBOX": "1",
                },
                timeout=timeout,
            )

        # Claude CLI logs MCP connection failures to stderr
        store().set("cc_stderr_tail", result.stderr[-4000:])

        # `claude -p` errors when it hits --max-turns, stdout contains stream
        events = _parse_stream_json(result.stdout)
        if not events:
            raise RuntimeError(f"claude failed (rc={result.returncode})")

        # The final `result` event carries the aggregate summary (result text,
        # session id, usage, cost, num_turns, subtype). Always last; present
        # both on success and on a controlled `--max-turns` stop.
        final = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if final is None:
            raise RuntimeError("claude transcript contained no `result` event")

        # `error_max_turns` is a graceful stop — log the partial trajectory and
        # record the reason instead of raising. Other error subtypes are real.
        max_turns_hit = final.get("subtype") == "error_max_turns"
        if final.get("is_error") and not max_turns_hit:
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
        # Inspect's StopReason literal has no "max_turns" — `model_length` is
        # the closest analog (a length-like external stop) and is what the
        # viewer surfaces in the output card.
        stop_reason = "model_length" if max_turns_hit else "stop"
        if last_assistant is not None:
            state.output = ModelOutput(
                model=model,
                choices=[
                    ChatCompletionChoice(
                        message=last_assistant, stop_reason=stop_reason
                    )
                ],
                usage=usage,
            )
        else:
            # Defensive: no assistant events seen. Fall back to the result text.
            state.output = ModelOutput.from_content(
                model=model, content=final.get("result", "")
            )
            state.output.usage = usage
            state.output.choices[0].stop_reason = stop_reason

        # Keep a few extras in `store()` that don't fit on ModelUsage but are
        # useful for downstream analysis (session resume, turn counts).
        store().set("cc_session_id", final.get("session_id"))
        store().set("cc_num_turns", final.get("num_turns"))
        store().set("cc_duration_ms", final.get("duration_ms"))
        store().set("cc_stop_subtype", final.get("subtype"))

        # Re-raise after state.messages / state.output are populated so the
        # transcript is preserved in the log; Inspect's solver wrapper records
        # the limit and ends the sample as usual.
        if limit_hit is not None:
            raise limit_hit
        return state

    return execute


@solver
def claude_code_solver() -> Solver:
    """Per-sample Solver wrapper around `claude_code_oauth`."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        design = state.metadata.get("design")
        agent_state = AgentState(messages=state.messages)
        agent_fn = claude_code_oauth(design=design)
        try:
            result = await agent_fn(agent_state)
        except LimitExceededError:
            # Preserve partial transcript
            state.messages = agent_state.messages
            state.output = agent_state.output
            raise

        state.messages = result.messages
        state.output = result.output
        return state

    return solve
