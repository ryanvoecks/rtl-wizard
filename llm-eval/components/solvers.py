"""Custom Claude Code agent driven by an OAUTH token, not an API key.

Shells out to `claude -p --output-format stream-json` in the sandbox and
translates each event into Inspect chat-message form so the .eval log is
viewable in `inspect view`. `CLAUDE_CODE_OAUTH_TOKEN` is injected per
`sb.exec(env=...)`; ANTHROPIC_API_KEY is *not* forwarded (CLI prefers it).
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
from inspect_ai.util import sandbox, store

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
    """Flatten a tool_result `content` field to plain text."""
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


def _parse_assistant_blocks(blocks: list) -> tuple[str, list[ToolCall]]:
    """Split an assistant message's content blocks into (text, tool_calls)."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in blocks:
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
    return "".join(text_parts), tool_calls


def _merge_or_make_assistant(
    msg: dict, asst_by_id: dict[str, ChatMessageAssistant]
) -> ChatMessageAssistant | None:
    """Build a ChatMessageAssistant for this event, or merge it into the
    in-flight message sharing its `message.id`."""
    text, tool_calls = _parse_assistant_blocks(msg.get("content") or [])
    msg_id = msg.get("id")
    existing = asst_by_id.get(msg_id) if msg_id else None
    if existing is not None:
        if text:
            prev = existing.content if isinstance(existing.content, str) else ""
            existing.content = prev + text
        if tool_calls:
            existing.tool_calls = (existing.tool_calls or []) + tool_calls
        return None
    new_msg = ChatMessageAssistant(
        id=msg_id,
        content=text,
        tool_calls=tool_calls or None,
        model=msg.get("model"),
    )
    if msg_id:
        asst_by_id[msg_id] = new_msg
    return new_msg


def _tool_results_to_messages(msg: dict) -> list[ChatMessageTool]:
    """One ChatMessageTool per tool_result block in a `user` event (parallel
    tool calls in a single turn produce multiple tool_result blocks)."""
    content = msg.get("content")
    blocks = content if isinstance(content, list) else []
    out: list[ChatMessageTool] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = _tool_result_text(block.get("content"))
        error = (
            ToolCallError(type="unknown", message=text)
            if block.get("is_error")
            else None
        )
        out.append(
            ChatMessageTool(
                content=text,
                tool_call_id=block.get("tool_use_id"),
                error=error,
            )
        )
    return out


def _events_to_messages(events: list[dict]) -> list[ChatMessage]:
    """Convert stream-json events into Inspect ChatMessage objects."""
    messages: list[ChatMessage] = []
    asst_by_id: dict[str, ChatMessageAssistant] = {}
    for event in events:
        etype = event.get("type")
        msg = event.get("message") or {}
        if etype == "assistant":
            new = _merge_or_make_assistant(msg, asst_by_id)
            if new is not None:
                messages.append(new)
        elif etype == "user":
            messages.extend(_tool_results_to_messages(msg))
    return messages


def _build_usage(final: dict) -> ModelUsage:
    usage = final.get("usage") or {}
    input_t = int(usage.get("input_tokens", 0))
    output_t = int(usage.get("output_tokens", 0))
    return ModelUsage(
        input_tokens=input_t,
        output_tokens=output_t,
        total_tokens=input_t + output_t,
        input_tokens_cache_write=usage.get("cache_creation_input_tokens"),
        input_tokens_cache_read=usage.get("cache_read_input_tokens"),
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
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                model,
                "--mcp-config",
                "/tmp/mcp.json",
                "--strict-mcp-config",
                "--allowedTools",
                allowed_tools,
                "--max-turns",
                str(max_turns),
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

        # The final `result` event carries the summary. Always last.
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final is None:
            raise RuntimeError("claude transcript contained no `result` event")

        # End gracefully on `error_max_turns`
        max_turns_hit = final.get("subtype") == "error_max_turns"
        if final.get("is_error") and not max_turns_hit:
            raise RuntimeError(f"claude reported error: {final.get('result')}")

        # Parse events, messages and usage
        parsed = _events_to_messages(events)
        state.messages.extend(parsed)
        usage = _build_usage(final)

        # Viewer treats final response specially
        last_assistant = next(
            (m for m in reversed(parsed) if isinstance(m, ChatMessageAssistant)),
            None,
        )
        if last_assistant is None:
            raise ValueError("could not find final response")

        # Final message and stop reason
        stop_reason = "model_length" if max_turns_hit else "stop"
        state.output = ModelOutput(
            model=model,
            choices=[
                ChatCompletionChoice(message=last_assistant, stop_reason=stop_reason)
            ],
            usage=usage,
        )

        # Additional useful fields
        store().set("cc_session_id", final.get("session_id"))
        store().set("cc_num_turns", final.get("num_turns"))
        store().set("cc_duration_ms", final.get("duration_ms"))
        store().set("cc_stop_subtype", final.get("subtype"))

        return state

    return execute


@solver
def claude_code_solver() -> Solver:
    """Per-sample Solver wrapper around `claude_code_oauth`."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        design = state.metadata["design"]
        assert isinstance(design, DesignConfig)
        agent_state = AgentState(messages=state.messages)
        agent_fn = claude_code_oauth(design=design)
        result = await agent_fn(agent_state)
        state.messages = result.messages
        state.output = result.output
        return state

    return solve
