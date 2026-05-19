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
from mcp_connect import MCPService
from mcp_servers import make_server

_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# MCP namespace the per-sample host server registers under — the Claude CLI
# exposes a FastMCP server's tools as `mcp__<server-name>__<tool>`.
_HOST_MCP_NAME = "rtl-wizard-host"
_RUN_TB_TOOL = f"mcp__{_HOST_MCP_NAME}__run_testbench"

# Fallback if Inspect resolves no model (e.g. neither `--model` nor
# `INSPECT_EVAL_MODEL` is set). With the repo's .env in play the latter is
# always set, so this is mostly a defensive default.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


def _resolve_claude_model() -> str:
    """Bare model name to pass to `claude --model`, sourced from Inspect's
    `--model` flag (or `INSPECT_EVAL_MODEL`) at solve time.

    Inspect prefixes model strings with a provider (e.g. `none/claude-sonnet-4-5`,
    `anthropic/claude-sonnet-4-5`). Claude Code's CLI wants the bare name only.
    `active_model().name` already returns the post-prefix portion."""
    m = active_model()
    if m is None or m.name in ("none", ""):
        return DEFAULT_CLAUDE_MODEL
    return m.name


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
    design: DesignConfig,
    mcp_config: dict | None = None,
    allowed_tools: str = f"Bash,Read,Write,Edit,{_RUN_TB_TOOL}",
    timeout_s: int = 1800,
    max_turns: int = 8,
):
    token = _require_token()
    base_mcp_config = mcp_config if mcp_config is not None else {"mcpServers": {}}

    async def execute(state: AgentState) -> AgentState:
        # Pull the model from Inspect's active model at solve time, so the
        # `--model` CLI flag (or INSPECT_EVAL_MODEL) drives both the viewer
        # label and the actual `claude --model` invocation. Use `none/<name>`
        # in the flag (e.g. `--model none/claude-sonnet-4-5`) to keep Inspect
        # from trying to instantiate an API client — the agent shells out.
        model = _resolve_claude_model()
        sb = sandbox()

        # Per-sample host-side MCP server: the agent reaches `run_testbench`
        # over SSE at host.docker.internal:<port>. The testbench evaluator
        # itself runs on the host, so testbench sources never enter the
        # sandbox.
        host_mcp = make_server(_HOST_MCP_NAME, ["run_testbench"], design)

        async with MCPService(host_mcp) as host_service:
            sample_mcp_config = {
                "mcpServers": {
                    **base_mcp_config.get("mcpServers", {}),
                    _HOST_MCP_NAME: {"type": "sse", "url": host_service.url},
                }
            }
            await sb.write_file("/tmp/mcp.json", json.dumps(sample_mcp_config))

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
                    _TOKEN_ENV_VAR: token,
                    "IS_SANDBOX": "1",
                },
                timeout=timeout_s,
            )

        # Claude CLI logs MCP connection failures to stderr (the stream-json
        # transcript on stdout doesn't mention them). Stash the stderr tail so
        # eval logs surface "MCP server unreachable" / "tool not registered"
        # diagnostics without needing a fresh run.
        if result.stderr:
            store().set("cc_stderr_tail", result.stderr[-4000:])

        # `claude -p` exits non-zero when it hits --max-turns, but stdout still
        # carries the full stream-json transcript including the terminating
        # `result` event. Parse stdout first and only treat the run as a hard
        # failure if stdout is unusable.
        events = _parse_stream_json(result.stdout) if result.stdout else []
        if not events:
            stderr_tail = (result.stderr or "")[-2000:]
            raise RuntimeError(
                f"claude failed (rc={result.returncode}) with no parseable "
                f"stdout. stderr: {stderr_tail}"
            )

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
    """Per-sample Solver wrapper around `claude_code_oauth`.

    Pulls the sample's `DesignConfig` out of `TaskState.metadata` and threads
    it into the agent so the host-side MCP server can be scoped to this
    sample. Bypasses `as_solver` because that wrapper strips everything from
    TaskState except the message list.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        design = state.metadata.get("design")
        if design is None:
            raise RuntimeError(
                "claude_code_solver requires `state.metadata['design']` "
                "(set in tasks.py:_build_sample)"
            )

        agent_state = AgentState(messages=state.messages)
        agent_fn = claude_code_oauth(design=design)
        try:
            result = await agent_fn(agent_state)
        except LimitExceededError:
            # The agent populates state.messages/output before re-raising so
            # the partial transcript is preserved. Mirror that into TaskState
            # and let the limit propagate.
            state.messages = agent_state.messages
            if agent_state.output is not None:
                state.output = agent_state.output
            raise

        state.messages = result.messages
        if result.output is not None:
            state.output = result.output
        return state

    return solve
