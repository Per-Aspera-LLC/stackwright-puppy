"""Callback registration for telemetry_ndjson plugin.

Subscribes to raft-puppy's agent-runtime hooks and emits OtterEvent NDJSON
via writer.emit(). Zero-cost when STACKWRIGHT_TELEMETRY_NDJSON is unset —
every handler short-circuits after writer.is_enabled().

## Hook selection notes

### ``invoke_agent`` core hook — currently dead code

The ``invoke_agent`` callback hook defined in ``code_puppy/callbacks.py``
is **never triggered** anywhere in raft-puppy: no call to
``_trigger_callbacks("invoke_agent", ...)`` exists in the codebase.
Specifically, ``_invoke_agent_impl`` in
``code_puppy/tools/subagent_invocation.py`` does NOT fire it.

# TODO: A follow-up task should either wire ``_trigger_callbacks("invoke_agent",
# ...)`` inside ``_invoke_agent_impl``, or remove the hook from callbacks.py
# entirely as dead code.

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

        if tool_name in SUBAGENT_TOOLS:
            args_dict = tool_args_raw if isinstance(tool_args_raw, dict) else {}
            target = args_dict.get("agent_name") or "unknown"
            model = args_dict.get("model_name")
            writer.emit(
                AgentInvokeStartEvent(
                    ts=now_iso(),
                    seq=next_seq(),
                    targetOtter=str(target),
                    model=str(model) if model else None,
                )
            )
            return  # Do NOT also emit ToolCallEvent for sub-agent dispatches.

        writer.emit(
            ToolCallEvent(
                ts=now_iso(),
                seq=next_seq(),
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
                    success=error is None,
                    durationSec=duration_sec,
                    model=str(model) if model else None,
                )
            )

            _responses_flag = os.environ.get(
                "STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES"
            )
            if response_text and _responses_flag in ("1", "true", "yes", "on"):
                writer.emit(
                    AgentResponseEvent(
                        ts=now_iso(),
                        seq=next_seq(),
                        text=str(response_text),
                    )
                )
            return  # Skip generic tool_complete for sub-agent dispatches.

        writer.emit(
            ToolCompleteEvent(
                ts=now_iso(),
                seq=next_seq(),
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
    """Emit AgentInvokeCompleteEvent (and optionally AgentResponseEvent) when done."""
    if not writer.is_enabled():
        return
    try:
        # positional order: agent_name, model_name, session_id, success,
        #                   error, response_text, metadata
        agent_name: str = (
            kwargs.get("agent_name") or (args[0] if args else None) or "unknown"
        )
        model_name: Any = args[1] if len(args) > 1 else kwargs.get("model_name")
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


async def _on_stream_event(*args: Any, **kwargs: Any) -> None:
    """Emit ThinkingEvent or ReasoningDumpEvent from streaming parts.

    Per-token deltas (objects with content_delta) are silently skipped —
    we don't accumulate per-token in Phase 3. Part-wrapper events are
    unwrapped to their inner part before duck-typing. Ambiguous events
    that don't match known Thinking/Reasoning type names are logged at
    DEBUG and skipped (never emit raw_log per ADR-002 §3).
    """
    if not writer.is_enabled():
        return
    try:
        event_data: Any = args[1] if len(args) > 1 else kwargs.get("event_data")

        # Per-token deltas — skip, no accumulation in Phase 3
        if hasattr(event_data, "content_delta"):
            return

        # Unwrap PartStartEvent / PartEndEvent to access the inner part
        inner = event_data
        if hasattr(event_data, "part"):
            inner = event_data.part

        type_name = type(inner).__name__

        # Extract text from the most common attribute names
        text: str | None = None
        for attr in ("content", "text"):
            val = getattr(inner, attr, None)
            if isinstance(val, str) and val:
                text = val
                break

        if text is None:
            logger.debug(
                "telemetry_ndjson: stream_event %s has no extractable text — skipping",
                type_name,
            )
            return

        if "Thinking" in type_name:
            writer.emit(ThinkingEvent(ts=now_iso(), seq=next_seq(), text=text))
        elif "Reasoning" in type_name:
            writer.emit(ReasoningDumpEvent(ts=now_iso(), seq=next_seq(), text=text))
        else:
            logger.debug(
                "telemetry_ndjson: stream_event type %s is ambiguous — skipping",
                type_name,
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
                FileReadEvent(ts=now_iso(), seq=next_seq(), path=str(file_path))
            )
        elif op in ("write", "edit", "delete"):
            writer.emit(
                FileWriteEvent(ts=now_iso(), seq=next_seq(), path=str(file_path))
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
