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
from collections.abc import Callable
from dataclasses import dataclass

from inspect_ai.agent import AgentState, agent
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.model._model import active_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall, ToolCallError
from inspect_ai.util import LimitExceededError, store
from mcp.server.fastmcp import FastMCP

from common.config import DesignConfig, TargetConfig

from .claude_env import SANDBOX_RTL_ROOT, ClaudeEnv, build_diff_from_env
from .container import TIMEOUT_RC, Container
from .mcp_servers import make_server

# Claude config
DEFAULT_TOOLS = ["Bash", "Read", "Write", "Edit"]
AGENT_TURNS = 50
AGENT_TIMEOUT = 3600

# MCP config - the host name the sandbox sees
HOST_MCP_NAME = "rtl-wizard-host"


@dataclass(frozen=True)
class SolverVariant:
    """Knobs that vary between `claude -p`-based solvers."""

    name: str
    mcp_tools: tuple[str, ...]
    max_turns: int
    timeout: int
    build_prompt: Callable[[TargetConfig], str]


def _top_sandbox(design: DesignConfig) -> str:
    top_file = next(
        (p for p in design.rtl_files if p.stem == design.top_module),
        design.rtl_files[0],
    )
    return f"{SANDBOX_RTL_ROOT}/{top_file}"


def _with_feedback_prompt(target: TargetConfig) -> str:
    design = target.design
    top_sandbox = _top_sandbox(design)
    return (
        f"There is an RTL design in `{SANDBOX_RTL_ROOT}/` (top module: "
        f"`{design.top_module}`, in `{top_sandbox}`). Your job is to "
        "increase the maximum clock frequency of the design by as much "
        " as possible.\n\n"
        "Constraints:\n"
        "- Preserve functional behaviour. You cannot read the "
        "testbench, but you can call the `run_testbench` MCP tool to "
        "run it against your current RTL - it returns the testbench's "
        "exit code and stdout so you can validate edits.\n"
        "- The design must remain synthesisable by yosys.\n"
        "- The area/power of the design should not increase by more than "
        "10%."
        f"- Edit the files in `{SANDBOX_RTL_ROOT}/` in place; do not "
        "rename them.\n\n"
        "To measure your progress, call the `synth_report` MCP "
        "tool: it synthesises your current RTL through ORFS and returns "
        "a post-synth logical-paths report - the worst register-to-"
        "register groups ranked by slack. Use it to find which paths to "
        "focus on. It will also give you an update on the area/power of "
        "the design."
    )


def _no_feedback_prompt(target: TargetConfig) -> str:
    design = target.design
    top_sandbox = _top_sandbox(design)
    return (
        f"There is an RTL design in `{SANDBOX_RTL_ROOT}/` (top module: "
        f"`{design.top_module}`, in `{top_sandbox}`). Your job is to "
        "increase the maximum clock frequency of the design by as much "
        "as possible.\n\n"
        "Constraints:\n"
        "- Preserve functional behaviour. You have no testbench and no "
        "synthesis tool available - your edits will be graded by a "
        "hidden testbench and by yosys synthesisability after you "
        "finish, so every change must be obviously safe.\n"
        "- The design must remain synthesisable by yosys.\n"
        "- The area/power of the design should not increase by more than "
        "10%.\n"
        f"- Edit the files in `{SANDBOX_RTL_ROOT}/` in place; do not "
        "rename them."
    )


WITH_FEEDBACK = SolverVariant(
    name="with-feedback",
    mcp_tools=("run_testbench", "synth_report"),
    max_turns=AGENT_TURNS,
    timeout=AGENT_TIMEOUT,
    build_prompt=_with_feedback_prompt,
)

NO_FEEDBACK = SolverVariant(
    name="no-feedback",
    mcp_tools=(),
    max_turns=AGENT_TURNS,
    timeout=AGENT_TIMEOUT,
    build_prompt=_no_feedback_prompt,
)


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
    Non-JSON lines are silently dropped, and with `allow_truncated_tail`, an incomplete
    final JSON line is also dropped.
    """
    lines = stdout.splitlines(keepends=True)
    events: list[dict] = []
    for lineno, raw in enumerate(lines, 1):
        is_last = lineno == len(lines)
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            # A killed process may have flushed a partial final JSON line.
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
    variant: SolverVariant,
):
    async def execute(state: AgentState) -> AgentState:
        model = _resolve_model()
        design = synth_target.design
        mcp_tool_list = list(variant.mcp_tools)
        mcp_tool_names = [f"mcp__{HOST_MCP_NAME}__{t}" for t in mcp_tool_list]
        allowed_tools = tuple(DEFAULT_TOOLS + mcp_tool_names)

        # MCP constructor needs user environment
        def mcp_factory(e: ClaudeEnv) -> FastMCP:
            return make_server(HOST_MCP_NAME, mcp_tool_list, synth_target, e)

        # Per-sample environment in a shared container
        with ClaudeEnv(container, mcp_factory) as env:
            # Stage RTL. Each sample gets a clean copy
            for rel_file, abs_file in zip(design.rtl_files, design.rtl_abs_paths):
                env.write_file(
                    f"{SANDBOX_RTL_ROOT}/{rel_file}",
                    abs_file.read_text(),
                )

            mcp_servers = {HOST_MCP_NAME: {"type": "sse", "url": env.url}}

            prompt = state.messages[-1].text
            sid = store().get("cc_session_id")
            result = await asyncio.to_thread(
                env.run_claude,
                prompt,
                model=model,
                mcp_servers=mcp_servers,
                allowed_tools=allowed_tools,
                max_turns=variant.max_turns,
                resume_session=sid,
                timeout=variant.timeout,
            )

            # Capture the final RTL state for scorers before env tear-down
            diff = await asyncio.to_thread(build_diff_from_env, env, design)
            store().set("rtl_diff", diff)

        # Claude CLI logs MCP connection failures to stderr
        store().set("cc_stderr_tail", result.stderr[-4000:])

        timed_out = result.returncode == TIMEOUT_RC

        # stdout contains stream, even on max turns and timeout errors
        events = _parse_stream_json(result.stdout, allow_truncated_tail=timed_out)
        if not events and not timed_out:
            raise RuntimeError(f"claude failed (rc={result.returncode})")

        # The final `result` event carries the summary. Always last.
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final is None and not timed_out:
            raise RuntimeError("claude transcript contained no `result` event")

        # End gracefully on `error_max_turns` and on timeout
        max_turns_hit = final is not None and final.get("subtype") == "error_max_turns"
        if final is not None and final.get("is_error") and not max_turns_hit:
            raise RuntimeError(f"claude reported error: {final.get('result')}")

        # Parse events, messages and usage
        parsed = _events_to_messages(events)
        state.messages.extend(parsed)
        usage = _build_usage(final) if final is not None else None

        # Viewer treats final response specially
        last_assistant = next(
            (m for m in reversed(parsed) if isinstance(m, ChatMessageAssistant)),
            None,
        )
        if last_assistant is None:
            if not timed_out:
                raise ValueError("could not find final response")
            last_assistant = ChatMessageAssistant(content="", model=model)

        # Final message and stop reason
        if timed_out:
            stop_reason = "unknown"
        elif max_turns_hit:
            stop_reason = "model_length"
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
        if final is not None:
            store().set("cc_session_id", final.get("session_id"))
            store().set("cc_num_turns", final.get("num_turns"))
            store().set("cc_duration_ms", final.get("duration_ms"))
            store().set("cc_stop_subtype", final.get("subtype"))
        store().set("cc_timed_out", timed_out)

        # Surface timeout / max-turns as an Inspect limit and system message
        if timed_out:
            msg = f"[Timed out after {variant.timeout}s]"
            state.messages.append(ChatMessageSystem(content=msg))
            raise LimitExceededError(
                type="time",
                value=variant.timeout,
                limit=variant.timeout,
                message=msg,
            )
        if max_turns_hit:
            msg = f"[Reached max turns of {variant.max_turns}]"
            state.messages.append(ChatMessageSystem(content=msg))
            raise LimitExceededError(
                type="message",
                value=variant.max_turns,
                limit=variant.max_turns,
                message=msg,
            )

        return state

    return execute


def _make_solver(container: Container, variant: SolverVariant) -> Solver:
    """Shared solver body parameterised by variant."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate  # claude -p drives generation; Inspect's generate is unused
        synth_target = state.metadata["synth_target"]
        assert isinstance(synth_target, TargetConfig)

        state.messages = [ChatMessageUser(content=variant.build_prompt(synth_target))]
        agent_state = AgentState(messages=state.messages)
        agent_fn = claude_code_oauth(
            synth_target=synth_target,
            container=container,
            variant=variant,
        )
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


@solver
def claude_code_solver(container: Container) -> Solver:
    """`claude -p` against the design RTL with MCP feedback tools
    (`run_testbench`, `synth_report`) available."""
    return _make_solver(container, WITH_FEEDBACK)


@solver
def claude_code_no_feedback_solver(container: Container) -> Solver:
    """`claude -p` against the design RTL with no MCP tools at all -- the
    agent edits blind and is graded once it finishes."""
    return _make_solver(container, NO_FEEDBACK)
