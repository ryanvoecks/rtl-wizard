"""Custom Claude Code agent driven by an interactive OAUTH login.

Spins up a per-sample `ClaudeEnv` (fresh unix user + workdir) inside the
long-lived sandbox container, stages the design's RTL into it, runs
`claude -p` as that user, and captures the resulting RTL diff into
`store()` for the scorers to consume. Each container does its own
`claude auth login` once (in `Container.__enter__`) so OAUTH credentials
aren't shared across containers (TOS: one login per "device").
"""

import asyncio
import json

from inspect_ai.agent import AgentState, agent
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.model._model import active_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall, ToolCallError
from inspect_ai.util import LimitExceededError, store

from common.config import TargetConfig

from .claude_env import SANDBOX_RTL_ROOT, ClaudeEnv, build_diff_from_env
from .container import TIMEOUT_RC, Container
from .mcp_servers import make_server

# Claude config
DEFAULT_TOOLS = ["Bash", "Read", "Write", "Edit"]
AGENT_TURNS = 30
AGENT_TIMEOUT = 1800

# MCP config
HOST_MCP_NAME = "rtl-wizard-host"
MCP_TOOLS = ["run_testbench", "synth_timing_report"]


def _resolve_model() -> str:
    """Bare model name to pass to `claude --model`, sourced from Inspect's
    `--model` flag (or `INSPECT_EVAL_MODEL`) at solve time."""
    m = active_model()
    if m is None or m.name in ("none", ""):
        raise ValueError("model name is not set")
    return m.name


def _parse_stream_json(
    stdout: str, *, allow_truncated_tail: bool = False
) -> list[dict]:
    """Parse line-delimited JSON events from `claude --output-format stream-json`.

    With `allow_truncated_tail`, an incomplete final line (e.g. from a SIGKILL
    mid-write on timeout) is silently dropped instead of raising.
    """
    lines = stdout.splitlines(keepends=True)
    events: list[dict] = []
    for lineno, raw in enumerate(lines, 1):
        is_last = lineno == len(lines)
        line = raw.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            # A killed process may have flushed a partial final line.
            if allow_truncated_tail and is_last and not raw.endswith("\n"):
                break
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
    synth_target: TargetConfig,
    container: Container,
    timeout: int = AGENT_TIMEOUT,
    max_turns: int = AGENT_TURNS,
):
    async def execute(state: AgentState) -> AgentState:
        model = _resolve_model()
        design = synth_target.design
        mcp_tool_names = [f"mcp__{HOST_MCP_NAME}__{tool}" for tool in MCP_TOOLS]
        allowed_tools = tuple(DEFAULT_TOOLS + mcp_tool_names)

        # MCP constructor needs user environment
        mcp_factory = (
            lambda e: make_server(HOST_MCP_NAME, MCP_TOOLS, synth_target, e),
        )

        # Per-sample environment in a shared container
        with ClaudeEnv(container, mcp_factory) as env:
            # Stage RTL. Each sample gets a clean copy
            for rel_file, abs_file in zip(design.rtl_files, design.rtl_abs_paths):
                env.write_file(
                    f"{SANDBOX_RTL_ROOT}/{rel_file}",
                    abs_file.read_text(),
                )

            prompt = state.messages[-1].text
            sid = store().get("cc_session_id")
            result = await asyncio.to_thread(
                env.run_claude,
                prompt,
                model=model,
                mcp_servers={
                    HOST_MCP_NAME: {"type": "sse", "url": env.url},
                },
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                resume_session=sid,
                timeout=timeout,
            )

            # Capture the final RTL state for scorers before env tear-down
            diff = await asyncio.to_thread(build_diff_from_env, env, design)
            store().set("rtl_diff", diff)

        # Claude CLI logs MCP connection failures to stderr
        store().set("cc_stderr_tail", result.stderr[-4000:])

        timed_out = result.returncode == TIMEOUT_RC

        # stdout contains stream, even on max turns and timeout errors
        events = _parse_stream_json(result.stdout, allow_truncated_tail=timed_out)
        if not events:
            raise RuntimeError(f"claude failed (rc={result.returncode})")

        # The final `result` event carries the summary. Always last.
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final is None:
            raise RuntimeError("claude transcript contained no `result` event")

        # End gracefully on `error_max_turns` and on timeout
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
        if timed_out:
            stop_reason = "time"
        elif max_turns_hit:
            stop_reason = "messages"
        else:
            stop_reason = "stop"
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
        store().set("cc_timed_out", timed_out)

        # Surface timeout / max-turns as an Inspect limit and system message
        if timed_out:
            msg = f"[Timed out after {timeout}s]"
            state.messages.append(ChatMessageSystem(content=msg))
            raise LimitExceededError(
                type="time", value=timeout, limit=timeout, message=msg
            )
        if max_turns_hit:
            msg = f"[Reached max turns of {max_turns}]"
            state.messages.append(ChatMessageSystem(content=msg))
            raise LimitExceededError(
                type="message", value=max_turns, limit=max_turns, message=msg
            )

        return state

    return execute


@solver
def claude_code_solver(container: Container) -> Solver:
    """Per-sample Solver wrapper around `claude_code_oauth`."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        synth_target = state.metadata["synth_target"]
        assert isinstance(synth_target, TargetConfig)
        agent_state = AgentState(messages=state.messages)
        agent_fn = claude_code_oauth(synth_target=synth_target, container=container)
        try:
            result = await agent_fn(agent_state)
        except LimitExceededError:
            state.messages = agent_state.messages
            state.output = agent_state.output
            raise
        state.messages = result.messages
        state.output = result.output
        return state

    return solve
