"""Callback registration for telemetry_ndjson plugin.

Subscribes to raft-puppy's agent-runtime hooks and emits OtterEvent NDJSON
via writer.emit(). Zero-cost when STACKWRIGHT_TELEMETRY_NDJSON is unset —
every handler short-circuits after writer.is_enabled().

## Hook selection notes

### ``invoke_agent`` core hook — upstream proposal, not used here

The ``invoke_agent`` callback hook defined upstream in
``code_puppy/callbacks.py`` is **never triggered** anywhere in the codebase
(no ``_trigger_callbacks("invoke_agent", ...)`` call exists). The same is
true of ``agent_exception``.

We considered wiring ``_trigger_callbacks("invoke_agent", ...)`` into
``_invoke_agent_impl`` in ``code_puppy/tools/subagent_invocation.py``, but
deliberately chose **not to**: it would be a core edit (forbidden by the
golden rule in ``AGENTS.md`` and tracked in ``CHANGES_FROM_UPSTREAM.md``),
and the ``pre_tool_call`` / ``post_tool_call`` interception of the
``invoke_agent`` / ``invoke_agent_with_model`` tool names that this plugin
already uses is strictly less invasive and gives equivalent observability.

If upstream ever accepts the proposed wire-up, plugins can switch from the
translation path below to direct ``invoke_agent`` subscription with no loss
of fidelity (and the benefit of receiving the resolved ``session_id``
directly instead of having to dig it out of the tool result). The full
proposal — including suggested signature, call-site, and tests — lives at
``docs/proposals/wire-invoke-agent-hook.md``.

We deliberately do NOT subscribe to ``invoke_agent`` here.

### ``agent_run_start`` / ``agent_run_end`` — top-level agent only

``agent_run_start`` and ``agent_run_end`` (fired from
``code_puppy/agents/_runtime.py``) only fire for the **top-level** agent run.
They do NOT fire for sub-agents launched via ``invoke_agent`` /
``invoke_agent_with_model``.

### Sub-agent telemetry — plugin-side translation

Sub-agent telemetry is synthesised here via ``pre_tool_call`` /
``post_tool_call``: when ``tool_name in SUBAGENT_TOOLS``, the generic
``tool_call`` / ``tool_complete`` events are suppressed and replaced by
``agent_invoke_start`` / ``agent_invoke_complete`` events.  This matches the
variant foreman emits on the Pro side per ADR-002.  ``targetOtter`` carries
the sub-agent name; ``model`` carries any per-call model override.

### ``STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES`` env var

When set to ``1`` / ``true`` / ``yes`` / ``on`` (default **off**), the plugin
additionally emits ``AgentResponseEvent`` carrying the sub-agent's ``.response``
text after each ``agent_invoke_complete``.  Off by default because sub-agent
responses can be very large and noisy in NDJSON logs.

### ``file_permission`` — sync handler

``file_permission`` handler is a SYNC function — the hook is triggered via
``_trigger_callbacks_sync``, so an async handler would need ``asyncio.run()``,
which breaks when a running loop already exists.  Sync is cleaner and correct.

### Defensive ``(*args, **kwargs)`` signatures

All handlers use ``(*args, **kwargs)`` defensive signatures for forward-compat
against upstream signature drift on rebase (per frontend_emitter pattern).

Structural twin: code_puppy/plugins/frontend_emitter/register_callbacks.py
Bead: code_puppy-vbt
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.plugins.telemetry_ndjson import writer
from code_puppy.plugins.telemetry_ndjson.otter_event import (
    AgentInvokeCompleteEvent,
    AgentInvokeStartEvent,
    AgentResponseEvent,
    FileReadEvent,
    FileWriteEvent,
    ReasoningDumpEvent,
    ShellCommandEvent,
    ThinkingEvent,
    TokenUpdateEvent,
    ToolCallEvent,
    ToolCompleteEvent,
    next_seq,
    now_iso,
)

logger = logging.getLogger(__name__)

# Tool names that represent sub-agent dispatches — translated to
# agent_invoke_start / agent_invoke_complete instead of tool_call / tool_complete.
SUBAGENT_TOOLS: frozenset[str] = frozenset({"invoke_agent", "invoke_agent_with_model"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_successful_result(result: Any) -> bool:
    """Mirror frontend_emitter._is_successful_result.

    Result dicts with an "error" key or "success": False map to False.
    Everything else (including None) maps to True.
    """
    if result is None:
        return True
    if isinstance(result, dict):
        if result.get("error"):
            return False
        if result.get("success") is False:
            return False
        return True
    if isinstance(result, bool):
        return result
    return True


def _current_otter() -> str | None:
    """Return the currently active sub-agent's name, or None if at top-level.

    Used to populate the ``otter`` field on emitted events — i.e. the otter
    that *emitted* the event (the caller / current runtime context). Reads
    from ``code_puppy.tools.subagent_context``'s ContextVar, which raft-puppy
    core sets via ``with subagent_context(agent_name):`` around every
    sub-agent invocation in ``code_puppy/tools/subagent_invocation.py``.
    ContextVars propagate through async tasks, so this is the correct way to
    discover sub-agent identity inside ``pre_tool_call`` / ``post_tool_call``
    handlers that receive no explicit ``context`` argument from core.

    Returns ``None`` at the top level. Top-level events leave the ``otter``
    field unset, which (with ``exclude_none=True`` on the writer) omits the
    key entirely from NDJSON output.
    """
    try:
        from code_puppy.tools.subagent_context import get_subagent_name

        return get_subagent_name()
    except Exception:
        return None


# Thinking/reasoning accumulator: (session_id, part_index) → {text, kind}
_part_accumulator: dict[tuple[str | None, int], dict[str, str]] = {}
_accum_lock = threading.Lock()

# Monotonic session counter; shared by foreman + per-subagent TokenUpdateEvent paths.
_cumulative_output_tokens: int = 0
_cumulative_lock = threading.Lock()


def _bump_and_emit_token_update(output_tokens: int, otter: str | None = None) -> None:
    """Bump monotonic token counter and emit TokenUpdateEvent. Shared by foreman + subagent paths."""
    global _cumulative_output_tokens  # noqa: PLW0603
    with _cumulative_lock:
        _cumulative_output_tokens += int(output_tokens)
        used = _cumulative_output_tokens
    try:
        from code_puppy.config import get_protected_token_count

        total: int = int(get_protected_token_count() or 200_000)
    except Exception:
        total = 200_000
    total = total or 200_000
    writer.emit(
        TokenUpdateEvent(
            ts=now_iso(),
            seq=next_seq(),
            otter=otter,
            used=used,
            total=total,
            percentUsed=used / total * 100.0,
        )
    )


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------


async def _on_pre_tool_call(*args: Any, **kwargs: Any) -> None:
    """Emit ToolCallEvent before a tool executes.

    For sub-agent dispatch tools (``invoke_agent`` / ``invoke_agent_with_model``)
    emits ``AgentInvokeStartEvent`` instead and suppresses the generic
    ``ToolCallEvent`` to avoid double-counting on the Pro side.
    """
    if not writer.is_enabled():
        return
    try:
        tool_name: str = (
            kwargs.get("tool_name") or (args[0] if args else None) or "unknown"
        )
        tool_args_raw: Any = args[1] if len(args) > 1 else kwargs.get("tool_args")

        # Task 4: normalize {"raw": ""} → None (MCP no-args sentinel from pydantic_patches.py)
        if isinstance(tool_args_raw, dict) and tool_args_raw == {"raw": ""}:
            tool_args_raw = None

        if tool_name in SUBAGENT_TOOLS:
            args_dict = tool_args_raw if isinstance(tool_args_raw, dict) else {}
            target = args_dict.get("agent_name") or "unknown"
            model = args_dict.get("model_name")
            writer.emit(
                AgentInvokeStartEvent(
                    ts=now_iso(),
                    seq=next_seq(),
                    targetOtter=str(target),
                    otter=_current_otter(),
                    model=str(model) if model else None,
                )
            )
            return  # Do NOT also emit ToolCallEvent for sub-agent dispatches.

        writer.emit(
            ToolCallEvent(
                ts=now_iso(),
                seq=next_seq(),
                otter=_current_otter(),
                toolName=str(tool_name),
                args=tool_args_raw if isinstance(tool_args_raw, dict) else None,
            )
        )
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_pre_tool_call failed: %s", e)


async def _on_post_tool_call(*args: Any, **kwargs: Any) -> None:
    """Emit ToolCompleteEvent after a tool finishes (flat shape, no nested payload).

    For sub-agent dispatch tools (``invoke_agent`` / ``invoke_agent_with_model``)
    emits ``AgentInvokeCompleteEvent`` instead and suppresses the generic
    ``ToolCompleteEvent`` to avoid double-counting on the Pro side.  Optionally
    also emits ``AgentResponseEvent`` when
    ``STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES`` is truthy.
    """
    if not writer.is_enabled():
        return
    try:
        tool_name: str = (
            kwargs.get("tool_name") or (args[0] if args else None) or "unknown"
        )
        # positional order: tool_name, tool_args, result, duration_ms, context
        result: Any = args[2] if len(args) > 2 else kwargs.get("result")
        duration_ms_raw: Any = args[3] if len(args) > 3 else kwargs.get("duration_ms")
        duration_ms: float | None = (
            float(duration_ms_raw) if duration_ms_raw is not None else None
        )

        if tool_name in SUBAGENT_TOOLS:
            # pydantic-ai's _call_tool serializes AgentInvokeOutput to a JSON string
            # before returning. Deserialize so _get(result, "response") works.
            # (beads swp-9pc0)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, ValueError):
                    pass  # leave as str; _get returns None gracefully

            # result is an AgentInvokeOutput-like object or dict — handle both.
            # NOTE: this branch ends with an explicit `return`, so the rebound
            # `result` does not flow into _is_successful_result below.
            def _get(obj: Any, key: str, default: Any = None) -> Any:
                if obj is None:
                    return default
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)

            target = _get(result, "agent_name") or "unknown"
            error = _get(result, "error")
            response_text = _get(result, "response")
            model = _get(result, "model_name")

            duration_sec: float | None = (
                (duration_ms / 1000.0) if duration_ms is not None else None
            )

            writer.emit(
                AgentInvokeCompleteEvent(
                    ts=now_iso(),
                    seq=next_seq(),
                    targetOtter=str(target),
                    otter=_current_otter(),
                    success=error is None,
                    durationSec=duration_sec,
                    model=str(model) if model else None,
                )
            )

            # Per-sub-agent token telemetry (Option A → B fallback).
            # A: SubAgentConsoleManager.token_count (live-counted by subagent_stream_handler).
            # B: estimate from response text length (~chars/2.5, same heuristic as elsewhere).
            _sub_tokens: int = 0
            try:
                _sub_sid = _get(result, "session_id")
                if _sub_sid:
                    from code_puppy.messaging.subagent_console import (
                        SubAgentConsoleManager,
                    )

                    _sub_state = SubAgentConsoleManager.get_instance().get_agent_state(
                        str(_sub_sid)
                    )
                    if _sub_state and _sub_state.token_count > 0:
                        _sub_tokens = int(_sub_state.token_count)
            except Exception as _te:
                logger.debug("telemetry_ndjson: subagent token lookup: %s", _te)
            if _sub_tokens == 0 and response_text:
                _sub_tokens = max(1, int(len(str(response_text)) / 2.5))
            if _sub_tokens > 0:
                try:
                    _bump_and_emit_token_update(_sub_tokens, otter=str(target))
                except Exception as _te:
                    logger.warning("telemetry_ndjson: subagent token emit: %s", _te)

            _responses_flag = os.environ.get(
                "STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES"
            )
            if response_text and _responses_flag in ("1", "true", "yes", "on"):
                writer.emit(
                    AgentResponseEvent(
                        ts=now_iso(),
                        seq=next_seq(),
                        otter=str(target),
                        text=str(response_text),
                    )
                )
            return  # Skip generic tool_complete for sub-agent dispatches.

        writer.emit(
            ToolCompleteEvent(
                ts=now_iso(),
                seq=next_seq(),
                otter=_current_otter(),
                toolName=str(tool_name),
                success=_is_successful_result(result),
                durationMs=duration_ms,
            )
        )
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_post_tool_call failed: %s", e)


async def _on_agent_run_start(*args: Any, **kwargs: Any) -> None:
    """Emit AgentInvokeStartEvent when a sub-agent run begins."""
    if not writer.is_enabled():
        return
    try:
        # positional order: agent_name, model_name, session_id
        agent_name: str = (
            kwargs.get("agent_name") or (args[0] if args else None) or "unknown"
        )
        model_name: Any = args[1] if len(args) > 1 else kwargs.get("model_name")
        writer.emit(
            AgentInvokeStartEvent(
                ts=now_iso(),
                seq=next_seq(),
                targetOtter=str(agent_name),
                model=str(model_name) if model_name else None,
            )
        )
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_agent_run_start failed: %s", e)


async def _on_agent_run_end(*args: Any, **kwargs: Any) -> None:
    """Emit AgentInvokeCompleteEvent, optional AgentResponseEvent, and TokenUpdateEvent."""
    if not writer.is_enabled():
        return

    # Unpack early so both try blocks below can reference these without re-deriving.
    agent_name: str = (
        kwargs.get("agent_name") or (args[0] if args else None) or "unknown"
    )
    model_name: Any = args[1] if len(args) > 1 else kwargs.get("model_name")

    try:
        # positional order: agent_name, model_name, session_id, success,
        #                   error, response_text, metadata
        success_raw: Any = args[3] if len(args) > 3 else kwargs.get("success", True)
        response_text: Any = args[5] if len(args) > 5 else kwargs.get("response_text")
        metadata: Any = args[6] if len(args) > 6 else kwargs.get("metadata")

        # Extract duration from metadata if the upstream supplies it.
        # None is the honest signal when timing is unavailable — fabricating
        # 0.0 would poison downstream timing aggregations.
        duration_sec: float | None = None
        if isinstance(metadata, dict):
            if "duration_sec" in metadata:
                duration_sec = float(metadata["duration_sec"])
            elif "duration_ms" in metadata:
                duration_sec = float(metadata["duration_ms"]) / 1000.0

        writer.emit(
            AgentInvokeCompleteEvent(
                ts=now_iso(),
                seq=next_seq(),
                targetOtter=str(agent_name),
                durationSec=duration_sec,
                success=bool(success_raw) if success_raw is not None else True,
                model=str(model_name) if model_name else None,
            )
        )

        if response_text:
            writer.emit(
                AgentResponseEvent(
                    ts=now_iso(),
                    seq=next_seq(),
                    text=str(response_text),
                )
            )
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_agent_run_end failed: %s", e)

    # Foreman token telemetry — best-effort; separate try to never suppress above.
    try:
        from code_puppy.agents.run_stats import AgentRunStats

        cycle_tokens = AgentRunStats.get_last_cycle_stats().get("output_tokens", 0) or 0
        if cycle_tokens > 0:
            _bump_and_emit_token_update(cycle_tokens, otter=str(agent_name))
    except Exception as e:
        logger.warning("telemetry_ndjson: token update failed: %s", e)


async def _on_stream_event(*args: Any, **kwargs: Any) -> None:
    """Accumulate thinking/reasoning deltas; emit ThinkingEvent/ReasoningDumpEvent on part_end.

    part_start → seed accumulator for ThinkingPart/ReasoningPart (others skipped).
    part_delta → append content_delta to slot. part_end → pop and emit full text.
    Non-dict event_data silently ignored. Never emits raw_log per ADR-002 §3.
    """
    if not writer.is_enabled():
        return
    try:
        event_type: str = args[0] if args else kwargs.get("event_type", "")
        event_data: Any = args[1] if len(args) > 1 else kwargs.get("event_data")
        agent_session_id: Any = (
            args[2] if len(args) > 2 else kwargs.get("agent_session_id")
        )
        emitter_otter = str(agent_session_id) if agent_session_id else _current_otter()

        if not isinstance(event_data, dict):
            # Legacy raw pydantic-ai objects: not handled in Phase 4
            return

        key: tuple[str | None, int] = (
            str(agent_session_id) if agent_session_id else None,
            int(event_data.get("index", -1)),
        )

        if event_type == "part_start":
            part_type: str = event_data.get("part_type", "")
            if "Thinking" in part_type:
                kind: str = "thinking"
            elif "Reasoning" in part_type:
                kind = "reasoning"
            else:
                return  # ToolCallPart / TextPart / etc — skip
            seed: str = event_data.get("content") or ""
            with _accum_lock:
                _part_accumulator[key] = {"text": seed, "kind": kind}

        elif event_type == "part_delta":
            delta_type: str = event_data.get("delta_type", "")
            is_thinking_delta = "Thinking" in delta_type or "Reasoning" in delta_type
            content_delta: str = event_data.get("content_delta") or ""
            if not content_delta:
                return
            with _accum_lock:
                slot = _part_accumulator.get(key)
                if slot is not None:
                    slot["text"] += content_delta
                elif is_thinking_delta:
                    # Delta arrived before start (rare) — create opportunistically
                    opp_kind = "reasoning" if "Reasoning" in delta_type else "thinking"
                    _part_accumulator[key] = {"text": content_delta, "kind": opp_kind}

        elif event_type == "part_end":
            with _accum_lock:
                slot = _part_accumulator.pop(key, None)
            if not slot or not slot.get("text"):
                return  # Empty or no accumulator entry — skip
            text: str = slot["text"]
            if slot["kind"] == "thinking":
                writer.emit(
                    ThinkingEvent(
                        ts=now_iso(), seq=next_seq(), otter=emitter_otter, text=text
                    )
                )
            else:
                writer.emit(
                    ReasoningDumpEvent(
                        ts=now_iso(), seq=next_seq(), otter=emitter_otter, text=text
                    )
                )
        else:
            logger.debug(
                "telemetry_ndjson: unknown stream event_type %r — skipping", event_type
            )
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_stream_event failed: %s", e)


async def _on_run_shell_command(*args: Any, **kwargs: Any) -> None:
    """Emit ShellCommandEvent before a shell command runs. Never blocks."""
    if not writer.is_enabled():
        return
    try:
        # positional order: context, command, cwd, timeout
        command: Any = args[1] if len(args) > 1 else kwargs.get("command", "")
        timeout_raw: Any = args[3] if len(args) > 3 else kwargs.get("timeout")
        writer.emit(
            ShellCommandEvent(
                ts=now_iso(),
                seq=next_seq(),
                otter=_current_otter(),
                command=str(command) if command else "",
                timeoutSec=int(timeout_raw) if timeout_raw is not None else None,
            )
        )
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_run_shell_command failed: %s", e)
    # Returning None — observe only, never block


def _on_file_permission(*args: Any, **kwargs: Any) -> bool:
    """Observe file operations and emit telemetry. Always grants permission.

    SYNC function — file_permission fires via _trigger_callbacks_sync and
    an async handler would require asyncio.run(), which breaks inside a
    running event loop. Sync is correct here.

    Operation mapping:
        "read"                    → FileReadEvent
        "write" | "edit" | "delete" → FileWriteEvent
        anything else             → debug log + skip (no raw_log per ADR-002)
    """
    if not writer.is_enabled():
        return True
    try:
        # positional order: context, file_path, operation, preview,
        #                   message_group, operation_data
        file_path: Any = args[1] if len(args) > 1 else kwargs.get("file_path", "")
        operation: Any = args[2] if len(args) > 2 else kwargs.get("operation", "")
        op = str(operation) if operation else ""

        if op == "read":
            writer.emit(
                FileReadEvent(
                    ts=now_iso(),
                    seq=next_seq(),
                    otter=_current_otter(),
                    path=str(file_path),
                )
            )
        elif op in ("write", "edit", "delete"):
            writer.emit(
                FileWriteEvent(
                    ts=now_iso(),
                    seq=next_seq(),
                    otter=_current_otter(),
                    path=str(file_path),
                )
            )
        else:
            logger.debug("telemetry_ndjson: unknown file operation %r — skipping", op)
    except Exception as e:
        logger.warning("telemetry_ndjson: _on_file_permission failed: %s", e)
    return True  # Always grant — we observe, never gate


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_callback("pre_tool_call", _on_pre_tool_call)
register_callback("post_tool_call", _on_post_tool_call)
register_callback("agent_run_start", _on_agent_run_start)
register_callback("agent_run_end", _on_agent_run_end)
register_callback("stream_event", _on_stream_event)
register_callback("run_shell_command", _on_run_shell_command)
register_callback("file_permission", _on_file_permission)

logger.info(
    "telemetry_ndjson: hooks registered (writer enabled: %s)", writer.is_enabled()
)
