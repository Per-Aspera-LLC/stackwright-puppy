"""Regression tests for telemetry_ndjson sub-agent attribution.

Validates that ``otter`` (the emitting otter — i.e. the caller / current
runtime context) is correctly stamped on telemetry events when callback
handlers fire inside a sub-agent context (via ``subagent_context``), and
correctly omitted (None → absent from JSON) when at the top level.

For ``AgentInvokeStartEvent`` and ``AgentInvokeCompleteEvent``, both fields
are validated:
- ``targetOtter`` — the callee being invoked (always present on those events)
- ``otter`` — the caller / nesting parent (present only when nested; None at
  the top level and omitted from JSON via ``exclude_none=True``)

Handlers under test
-------------------
* ``_on_pre_tool_call``     → emits ``ToolCallEvent`` (generic) or
                              ``AgentInvokeStartEvent`` (sub-agent tool)
* ``_on_file_permission``   → emits ``FileReadEvent``   (sync handler)
* ``_on_run_shell_command`` → emits ``ShellCommandEvent``

Pattern
-------
- Both ``writer.emit`` *and* ``writer.is_enabled`` are monkeypatched so tests
  need no real NDJSON file and the is_enabled() short-circuit cannot bail.
- ``subagent_context`` is used as a plain ``with`` block to set the ContextVar.
  ``asyncio.run()`` copies the calling context (Python 3.7+), so ContextVars
  set before the call are visible inside the async handlers. ✓
- Handlers are called with positional args matching the documented hook order
  (same convention as test_callbacks.py / test_subagent_response_capture.py).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from code_puppy.tools.subagent_context import subagent_context


# ---------------------------------------------------------------------------
# 1. ToolCallEvent — otter stamped when fired inside sub-agent context
# ---------------------------------------------------------------------------


def test_pre_tool_call_inside_subagent_stamps_otter():
    """_on_pre_tool_call inside subagent_context(\"designer-otter\") stamps otter.

    Positional args: (tool_name, tool_args)
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted: list = []
    with (
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.emit",
            side_effect=emitted.append,
        ),
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.is_enabled",
            return_value=True,
        ),
        subagent_context("designer-otter"),
    ):
        asyncio.run(rc._on_pre_tool_call("cp_list_files", {"directory": "."}))

    tool_call_events = [e for e in emitted if e.type == "tool_call"]
    assert len(tool_call_events) == 1, (
        f"Expected 1 ToolCallEvent, got {len(tool_call_events)}. "
        f"All emitted types: {[e.type for e in emitted]}"
    )
    assert tool_call_events[0].otter == "designer-otter", (
        f"Expected otter='designer-otter', got {tool_call_events[0].otter!r}"
    )


# ---------------------------------------------------------------------------
# 2. ToolCallEvent — otter is None (and absent from JSON) at top level
# ---------------------------------------------------------------------------


def test_pre_tool_call_outside_subagent_omits_otter():
    """_on_pre_tool_call outside any sub-agent context emits otter=None.

    The field must be absent from JSON output because writer calls
    model_dump_json(by_alias=True, exclude_none=True).

    Positional args: (tool_name, tool_args)
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted: list = []
    with (
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.emit",
            side_effect=emitted.append,
        ),
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.is_enabled",
            return_value=True,
        ),
    ):
        # No subagent_context — top level
        asyncio.run(rc._on_pre_tool_call("cp_read_file", {"file_path": "/tmp/foo"}))

    tool_call_events = [e for e in emitted if e.type == "tool_call"]
    assert len(tool_call_events) == 1, (
        f"Expected 1 ToolCallEvent, got {len(tool_call_events)}. "
        f"All emitted types: {[e.type for e in emitted]}"
    )
    assert tool_call_events[0].otter is None, (
        f"Expected otter is None at top-level, got {tool_call_events[0].otter!r}"
    )
    # The field must be absent from the serialized JSON (exclude_none=True)
    json_str = tool_call_events[0].model_dump_json(by_alias=True, exclude_none=True)
    assert "otter" not in json_str, (
        f"otter must be absent from JSON for top-level events; got: {json_str}"
    )


# ---------------------------------------------------------------------------
# 3. FileReadEvent — otter stamped when fired inside sub-agent context
# ---------------------------------------------------------------------------


def test_file_permission_inside_subagent_stamps_otter():
    """_on_file_permission inside subagent_context(\"builder-otter\") stamps otter.

    _on_file_permission is a SYNC handler (file_permission fires via
    _trigger_callbacks_sync), so no asyncio.run() is needed.

    Positional args: (context, file_path, operation)
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted: list = []
    with (
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.emit",
            side_effect=emitted.append,
        ),
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.is_enabled",
            return_value=True,
        ),
        subagent_context("builder-otter"),
    ):
        rc._on_file_permission(None, "/workspace/main.py", "read")

    file_read_events = [e for e in emitted if e.type == "file_read"]
    assert len(file_read_events) == 1, (
        f"Expected 1 FileReadEvent, got {len(file_read_events)}. "
        f"All emitted types: {[e.type for e in emitted]}"
    )
    assert file_read_events[0].otter == "builder-otter", (
        f"Expected otter='builder-otter', got {file_read_events[0].otter!r}"
    )


# ---------------------------------------------------------------------------
# 4. ShellCommandEvent — otter stamped when fired inside sub-agent context
# ---------------------------------------------------------------------------


def test_run_shell_command_inside_subagent_stamps_otter():
    """_on_run_shell_command inside subagent_context(\"runner-otter\") stamps otter.

    Positional args: (context, command, cwd, timeout)
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted: list = []
    with (
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.emit",
            side_effect=emitted.append,
        ),
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.is_enabled",
            return_value=True,
        ),
        subagent_context("runner-otter"),
    ):
        asyncio.run(rc._on_run_shell_command(None, "pytest -v", None, 60))

    shell_events = [e for e in emitted if e.type == "shell_command"]
    assert len(shell_events) == 1, (
        f"Expected 1 ShellCommandEvent, got {len(shell_events)}. "
        f"All emitted types: {[e.type for e in emitted]}"
    )
    assert shell_events[0].otter == "runner-otter", (
        f"Expected otter='runner-otter', got {shell_events[0].otter!r}"
    )


# ---------------------------------------------------------------------------
# 5. AgentInvokeStartEvent — top-level: only targetOtter set, otter is None
# ---------------------------------------------------------------------------


def test_agent_invoke_start_top_level_stamps_only_target_otter():
    """At the top level, AgentInvokeStartEvent has targetOtter but no otter.

    When _on_pre_tool_call fires with tool_name=\"invoke_agent\" outside any
    subagent_context, the emitted AgentInvokeStartEvent carries:
    - targetOtter = \"designer-otter\"  (the callee being invoked)
    - otter = None                     (no caller sub-agent — top level)

    otter=None means the key is omitted from JSON via exclude_none=True.

    Positional args: (tool_name, tool_args)
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted: list = []
    with (
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.emit",
            side_effect=emitted.append,
        ),
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.is_enabled",
            return_value=True,
        ),
    ):
        # No subagent_context — top level
        asyncio.run(
            rc._on_pre_tool_call("invoke_agent", {"agent_name": "designer-otter"})
        )

    invoke_start_events = [e for e in emitted if e.type == "agent_invoke_start"]
    assert len(invoke_start_events) == 1, (
        f"Expected 1 AgentInvokeStartEvent, got {len(invoke_start_events)}. "
        f"All emitted types: {[e.type for e in emitted]}"
    )
    event = invoke_start_events[0]
    assert event.targetOtter == "designer-otter", (
        f"Expected targetOtter='designer-otter', got {event.targetOtter!r}"
    )
    assert event.otter is None, f"Expected otter=None at top level, got {event.otter!r}"
    # otter must be absent from the serialized JSON (exclude_none=True)
    json_str = event.model_dump_json(by_alias=True, exclude_none=True)
    assert "otter" not in json_str or '"targetOtter"' in json_str, (
        # targetOtter IS expected in JSON; the otter key (the emitter field)
        # must not appear when None.  Check the raw exclusion:
        f"Top-level AgentInvokeStartEvent should omit otter from JSON; got: {json_str}"
    )


# ---------------------------------------------------------------------------
# 6. AgentInvokeStartEvent — nested: both targetOtter AND otter are set
# ---------------------------------------------------------------------------


def test_agent_invoke_start_nested_stamps_both():
    """Nested invocation: AgentInvokeStartEvent carries both targetOtter and otter.

    When _on_pre_tool_call fires with tool_name=\"invoke_agent\" INSIDE a
    subagent_context(\"designer-otter\"), the emitted AgentInvokeStartEvent
    carries:
    - targetOtter = \"wiggum-judge\"   (the callee being invoked)
    - otter = \"designer-otter\"       (the caller — the nesting parent)

    This is the dual-stamping case that enables reconstruction of nested
    invocation chains.

    Positional args: (tool_name, tool_args)
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted: list = []
    with (
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.emit",
            side_effect=emitted.append,
        ),
        patch(
            "code_puppy.plugins.telemetry_ndjson.writer.is_enabled",
            return_value=True,
        ),
        subagent_context("designer-otter"),
    ):
        asyncio.run(
            rc._on_pre_tool_call("invoke_agent", {"agent_name": "wiggum-judge"})
        )

    invoke_start_events = [e for e in emitted if e.type == "agent_invoke_start"]
    assert len(invoke_start_events) == 1, (
        f"Expected 1 AgentInvokeStartEvent, got {len(invoke_start_events)}. "
        f"All emitted types: {[e.type for e in emitted]}"
    )
    event = invoke_start_events[0]
    assert event.targetOtter == "wiggum-judge", (
        f"Expected targetOtter='wiggum-judge' (callee), got {event.targetOtter!r}"
    )
    assert event.otter == "designer-otter", (
        f"Expected otter='designer-otter' (caller/nesting parent), got {event.otter!r}"
    )
