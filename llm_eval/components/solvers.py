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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from inspect_ai._util.working import sample_working_time
from inspect_ai.agent import AgentState, agent
from inspect_ai.event import ModelEvent, ToolEvent
from inspect_ai.log import transcript
from inspect_ai.log._samples import (
    set_active_sample_total_cost,
    set_active_sample_total_tokens,
)
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.model._model import (
    active_model,
    model_usage_context_var,
    sample_model_usage_context_var,
    sample_total_cost,
    sample_total_tokens,
    set_model_usage,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall, ToolCallError
from inspect_ai.util import LimitExceededError, store
from inspect_ai.util._limit import (
    check_cost_limit,
    check_token_limit,
    record_model_cost,
    record_model_usage,
)
from mcp.server.fastmcp import FastMCP

from common.config import TargetConfig
from components.claude_env import SANDBOX_RTL_ROOT, ClaudeEnv, build_diff_from_env
from components.container import TIMEOUT_RC, current_container
from components.mcp_servers import OUTPUT_LIMIT, _synth_and_report, make_server
from components.scorers import evaluate_testbench

# Claude config
DEFAULT_TOOLS = ["Bash", "Read", "Write", "Edit"]
AGENT_TURNS = 3
AGENT_TIMEOUT = 30
ONE_ROUND_TURNS = 20
ONE_ROUND_TIMEOUT = 1200
ITERATIVE_ROUNDS = 3

# MCP config - the host name the sandbox sees
HOST_MCP_NAME = "rtl-wizard-host"


@dataclass(frozen=True)
class SolverVariant:
    """Knobs that vary between `claude -p`-based solvers."""

    name: str
    mcp_tools: tuple[str, ...]
    max_turns: int
    timeout: int
    instructions: str
    include_initial_report: bool = False
    edit_rounds: int = 1


def _resolve_model() -> str:
    """Bare model name to pass to `claude --model`, sourced from Inspect's
    `--model` flag (or `INSPECT_EVAL_MODEL`) at solve time."""
    m = active_model()
    if m is None or m.name in ("none", ""):
        raise ValueError("model name is not set")
    return m.name


def _parse_stream_json(
    stdout: str,
    line_elapsed_s: tuple[float, ...],
    *,
    allow_truncated_tail: bool = False,
) -> list[tuple[dict, float]]:
    """Parse line-delimited JSON events from `claude --output-format stream-json`."""
    # Split on \n only so line indexes line up with line_elapsed_s.
    lines = stdout.split("\n")
    out: list[tuple[dict, float]] = []
    n = len(lines)
    final_t = line_elapsed_s[-1] if line_elapsed_s else 0.0
    for lineno, raw in enumerate(lines, 1):
        is_last = lineno == n
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            # A killed process may have flushed a partial final JSON line.
            if allow_truncated_tail and is_last and not stdout.endswith("\n"):
                break
            raise RuntimeError(
                f"claude stdout line {lineno} is not valid JSON ({e}). "
                f"Preview (first 200 chars): {line[:200]!r}"
            ) from e
        idx = lineno - 1
        t = line_elapsed_s[idx] if idx < len(line_elapsed_s) else final_t
        out.append((event, t))
    return out


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


def _events_to_messages(
    events_with_times: list[tuple[dict, float]],
    *,
    model: str,
    round_start_working: float,
    round_start_wall: datetime,
    prior_messages: list[ChatMessage],
) -> list[ChatMessage]:
    """Convert stream-json events into Inspect ChatMessage objects. Different behaviour
    for assistant/user/tool messages."""
    messages: list[ChatMessage] = []
    asst_by_id: dict[str, ChatMessageAssistant] = {}
    asst_event_by_id: dict[str, ModelEvent] = {}
    asst_start_by_id: dict[str, float] = {}
    tool_use_time: dict[str, float] = {}
    tool_use_call: dict[str, ToolCall] = {}
    prev_time = 0.0
    for event, t in events_with_times:
        etype = event.get("type")
        msg = event.get("message") or {}
        if etype == "assistant":
            msg_id = msg.get("id") or ""
            new = _merge_or_make_assistant(msg, asst_by_id)
            if new is not None:
                start_t = prev_time
                asst_start_by_id[msg_id] = start_t
                input_snapshot = list(prior_messages) + list(messages)
                messages.append(new)
                mev = ModelEvent(
                    model=model,
                    input=input_snapshot,
                    tools=[],
                    tool_choice="auto",
                    config=GenerateConfig(),
                    output=ModelOutput(
                        model=model,
                        choices=[
                            ChatCompletionChoice(
                                message=new,
                                stop_reason=(
                                    "tool_calls" if new.tool_calls else "stop"
                                ),
                            )
                        ],
                    ),
                    working_start=round_start_working + start_t,
                    working_time=max(0.0, t - start_t),
                    timestamp=round_start_wall + timedelta(seconds=start_t),
                    completed=round_start_wall + timedelta(seconds=t),
                )
                asst_event_by_id[msg_id] = mev
                transcript()._event(mev)
            else:
                existing = asst_event_by_id.get(msg_id)
                if existing is not None:
                    start_t = asst_start_by_id.get(msg_id, prev_time)
                    existing.working_time = max(0.0, t - start_t)
                    existing.completed = round_start_wall + timedelta(seconds=t)
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tu_id = block.get("id", "")
                    tool_use_time[tu_id] = t
                    tool_use_call[tu_id] = ToolCall(
                        id=tu_id,
                        function=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
            prev_time = t
        elif etype == "user":
            for tm in _tool_results_to_messages(msg):
                messages.append(tm)
                tu_id = tm.tool_call_id or ""
                use_t = tool_use_time.get(tu_id, prev_time)
                call = tool_use_call.get(tu_id)
                transcript()._event(
                    ToolEvent(
                        id=tu_id,
                        function=call.function if call else "",
                        arguments=call.arguments if call else {},
                        result=tm.text,
                        error=tm.error,
                        working_start=round_start_working + use_t,
                        working_time=max(0.0, t - use_t),
                        timestamp=round_start_wall + timedelta(seconds=use_t),
                        completed=round_start_wall + timedelta(seconds=t),
                        message_id=tm.id,
                    )
                )
            prev_time = t
        else:
            prev_time = t
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


def _accumulate(key: str, value: int | float | None) -> None:
    """Sum `value` into `store()[key]` across rounds."""
    if value is None:
        return
    prev = store().get(key) or 0
    store().set(key, prev + value)


def _record_round_usage(model: str, usage: ModelUsage) -> None:
    """Plumb CLI-reported usage into Inspect's sample/eval rollups so tokens
    and cost show in the dashboard."""
    set_model_usage(model, usage, sample_model_usage_context_var.get(None))
    set_model_usage(model, usage, model_usage_context_var.get(None))
    record_model_usage(usage)
    set_active_sample_total_tokens(sample_total_tokens())
    check_token_limit()

    if usage.total_cost is not None:
        record_model_cost(usage.total_cost)
        set_active_sample_total_cost(sample_total_cost())
        check_cost_limit()


def _sum_usages(usages: list[ModelUsage]) -> ModelUsage | None:
    """Aggregate per-round ModelUsage objects."""
    if not usages:
        return None

    def _sum_int(field: str) -> int | None:
        vals = [getattr(u, field) for u in usages if getattr(u, field) is not None]
        return sum(vals) if vals else None

    def _sum_float(field: str) -> float | None:
        vals = [getattr(u, field) for u in usages if getattr(u, field) is not None]
        return sum(vals) if vals else None

    input_t = sum(u.input_tokens for u in usages)
    output_t = sum(u.output_tokens for u in usages)
    return ModelUsage(
        input_tokens=input_t,
        output_tokens=output_t,
        total_tokens=input_t + output_t,
        input_tokens_cache_write=_sum_int("input_tokens_cache_write"),
        input_tokens_cache_read=_sum_int("input_tokens_cache_read"),
        total_cost=_sum_float("total_cost"),
    )


def _build_feedback(synth_target: TargetConfig, diff: str) -> str:
    """Run the testbench and a post-synth report and format the results
    as a feedback message."""
    design = synth_target.design

    tb_log, tb_rc = evaluate_testbench(design, diff)
    if len(tb_log) > OUTPUT_LIMIT:
        tb_log = (
            tb_log[-OUTPUT_LIMIT:]
            + f"\n[testbench output truncated to last {OUTPUT_LIMIT} chars]"
        )

    try:
        synth_rpt = _synth_and_report(synth_target, diff)
    except Exception as e:
        synth_rpt = f"[synth error] {type(e).__name__}: {e}"
    if len(synth_rpt) > OUTPUT_LIMIT:
        synth_rpt = (
            synth_rpt[-OUTPUT_LIMIT:]
            + f"\n[synth report truncated to last {OUTPUT_LIMIT} chars]"
        )

    return (
        "Your current RTL was checked against the hidden testbench and "
        "synthesised through ORFS.\n\n"
        f"Testbench [rc={tb_rc}]:\n```\n{tb_log}\n```\n\n"
        f"Post-synth logical-paths report:\n```\n{synth_rpt}\n```\n\n"
        "Continue optimising the design."
    )


@agent
def claude_code_oauth(
    synth_target: TargetConfig,
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

        rounds = variant.edit_rounds
        finals: list[dict] = []
        timed_out = False
        max_turns_hit = False

        # Per-sample environment in a shared container
        with ClaudeEnv(current_container(), mcp_factory) as env:
            # Stage RTL. Each sample gets a clean copy
            for rel_file, abs_file in zip(design.rtl_files, design.rtl_abs_paths):
                env.write_file(
                    f"{SANDBOX_RTL_ROOT}/{rel_file}",
                    abs_file.read_text(),
                )

            mcp_servers = {HOST_MCP_NAME: {"type": "sse", "url": env.url}}
            prompt = state.messages[-1].text

            for round_idx in range(rounds):
                # Feedback prompts after the first round get logged as user messages
                if round_idx > 0:
                    state.messages.append(ChatMessageUser(content=prompt))

                sid = store().get("cc_session_id")
                # Anchor for back-dating per-event timing into the transcript.
                round_start_working = sample_working_time()
                round_start_wall = datetime.now(timezone.utc)
                prior_messages = list(state.messages)
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
                store().set("cc_stderr_tail", result.stderr[-4000:])
                store().set("cc_stdout_tail", result.stdout[-4000:])

                timed_out = result.returncode == TIMEOUT_RC
                events = _parse_stream_json(
                    result.stdout,
                    result.line_elapsed_s,
                    allow_truncated_tail=timed_out,
                )
                if not events and not timed_out:
                    raise RuntimeError(
                        f"claude failed (rc={result.returncode}); "
                        f"stdout tail: {result.stdout[-2000:]!r}; "
                        f"stderr tail: {result.stderr[-2000:]!r}"
                    )

                final = next(
                    (e for e, _ in reversed(events) if e.get("type") == "result"),
                    None,
                )
                if final is None and not timed_out:
                    raise RuntimeError("claude transcript contained no `result` event")

                max_turns_hit = (
                    final is not None and final.get("subtype") == "error_max_turns"
                )
                if final is not None and final.get("is_error") and not max_turns_hit:
                    raise RuntimeError(f"claude reported error: {final.get('result')}")

                state.messages.extend(
                    _events_to_messages(
                        events,
                        model=model,
                        round_start_working=round_start_working,
                        round_start_wall=round_start_wall,
                        prior_messages=prior_messages,
                    )
                )
                if final is not None:
                    finals.append(final)
                    _record_round_usage(model, _build_usage(final))
                    store().set("cc_session_id", final.get("session_id"))
                    _accumulate("cc_num_turns", final.get("num_turns"))
                    _accumulate("cc_duration_ms", final.get("duration_ms"))
                    _accumulate("cc_duration_api_ms", final.get("duration_api_ms"))
                    store().set("cc_stop_subtype", final.get("subtype"))
                    store().set("cc_total_cost_usd", sample_total_cost())

                if timed_out:
                    state.messages.append(
                        ChatMessageSystem(
                            content=f"[Round {round_idx + 1}/{rounds} timed out "
                            f"after {variant.timeout}s]\n"
                        )
                    )
                elif max_turns_hit:
                    state.messages.append(
                        ChatMessageSystem(
                            content=f"[Round {round_idx + 1}/{rounds} reached max "
                            f"turns of {variant.max_turns}]\n"
                        )
                    )

                if round_idx < rounds - 1:
                    cur_diff = await asyncio.to_thread(build_diff_from_env, env, design)
                    prompt = await asyncio.to_thread(
                        _build_feedback, synth_target, cur_diff
                    )

            # Capture the final RTL state for scorers before env tear-down
            diff = await asyncio.to_thread(build_diff_from_env, env, design)
            store().set("rtl_diff", diff)

        # Viewer treats final response specially
        last_assistant = next(
            (
                m
                for m in reversed(state.messages)
                if isinstance(m, ChatMessageAssistant)
            ),
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
        usage = _sum_usages([_build_usage(f) for f in finals])
        state.output = ModelOutput(
            model=model,
            choices=[
                ChatCompletionChoice(message=last_assistant, stop_reason=stop_reason)
            ],
            usage=usage,
        )

        store().set("cc_timed_out", timed_out)

        # Surface the final round's timeout / max-turns as an Inspect limit.
        # The per-round system message is appended inside the loop above.
        if timed_out:
            raise LimitExceededError(
                type="time",
                value=variant.timeout,
                limit=variant.timeout,
                message=f"[Timed out after {variant.timeout}s]",
            )
        if max_turns_hit:
            raise LimitExceededError(
                type="message",
                value=variant.max_turns,
                limit=variant.max_turns,
                message=f"[Reached max turns of {variant.max_turns}]",
            )

        return state

    return execute


def _make_solver(variant: SolverVariant) -> Solver:
    """Shared solver body parameterised by variant."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate  # `claude -p` drives generation. Inspect's generate is unused
        synth_target = state.metadata["synth_target"]
        assert isinstance(synth_target, TargetConfig)

        # Append solver's instructions to task prompt
        task_prompt = state.messages[-1].text
        prompt = f"{task_prompt}\n\n{variant.instructions}"
        if variant.include_initial_report:
            report = await asyncio.to_thread(_synth_and_report, synth_target)
            prompt += (
                "\n\nA post-synth logical-paths report for the unmodified "
                "design follows. It ranks register-to-register path groups "
                "by slack and includes the area/power totals.\n\n"
                f"```\n{report}\n```"
            )
        state.messages = [ChatMessageUser(content=prompt)]
        agent_state = AgentState(messages=state.messages)
        agent_fn = claude_code_oauth(
            synth_target=synth_target,
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
def claude_code_agentic_solver() -> Solver:
    """Full agentic mode with functional and synthesis MCP tools."""

    instructions = (
        "You have two MCP tools to verify your work as you go:\n"
        "- `run_testbench`: runs the hidden testbench against your current "
        "RTL and returns its exit code plus stdout. Use it to confirm "
        "functional correctness after each edit.\n"
        "- `synth_report`: synthesises your current RTL through ORFS and "
        "returns a post-synth logical-paths report - the worst register-to-"
        "register groups ranked by slack - plus an area/power summary. Use "
        "it to find which paths to focus on."
    )

    agent_config = SolverVariant(
        name="agentic",
        mcp_tools=("run_testbench", "synth_report"),
        max_turns=AGENT_TURNS,
        timeout=AGENT_TIMEOUT,
        instructions=instructions,
    )

    return _make_solver(agent_config)


@solver
def claude_code_no_feedback_solver() -> Solver:
    """No feedback baseline. No tools and no PPA info in prompt."""

    instructions = (
        "You have no testbench or synthesis tools available. Your edits "
        "will be graded by a hidden testbench and by yosys synthesisability "
        "after you finish, so every change must be obviously safe."
    )

    no_feedback_config = SolverVariant(
        name="no-feedback",
        mcp_tools=(),
        max_turns=ONE_ROUND_TURNS,
        timeout=ONE_ROUND_TIMEOUT,
        instructions=instructions,
    )

    return _make_solver(no_feedback_config)


@solver
def claude_code_single_feedback_solver() -> Solver:
    """One-shot baseline. No tools, but a post-synth PPA report is included
    in the initial prompt."""

    instructions = (
        "You have no testbench or synthesis tools available. Your edits "
        "will be graded by a hidden testbench and by yosys synthesisability "
        "after you finish, so every change must be obviously safe."
    )

    single_feedback_config = SolverVariant(
        name="single-feedback",
        mcp_tools=(),
        max_turns=ONE_ROUND_TURNS,
        timeout=ONE_ROUND_TIMEOUT,
        instructions=instructions,
        include_initial_report=True,
    )

    return _make_solver(single_feedback_config)


@solver
def claude_code_iterative_solver() -> Solver:
    """Iterative mode with a fixed number of feedback rounds. After each round the
    testbench and synth are run against the current RTL and results are fed back."""

    instructions = (
        "You have no tools to call yourself. After each of your responses, "
        "your current RTL will be automatically checked against the hidden "
        "testbench and synthesised through ORFS, and the results returned to "
        "you for the next round. Use each round to make targeted edits."
    )

    iterative_config = SolverVariant(
        name="iterative",
        mcp_tools=(),
        max_turns=ONE_ROUND_TURNS,
        timeout=ONE_ROUND_TIMEOUT,
        instructions=instructions,
        edit_rounds=ITERATIVE_ROUNDS,
        include_initial_report=True,
    )

    return _make_solver(iterative_config)
