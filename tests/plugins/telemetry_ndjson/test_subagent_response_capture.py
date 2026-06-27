"""Tests for _on_post_tool_call JSON-string deserialization (beads swp-9pc0).

Covers the bug where pydantic-ai's _call_tool returns AgentInvokeOutput as a
JSON string, causing _get() to fail via getattr() and AgentResponseEvent to
never fire even when STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES=1.

These tests are deliberately separate from test_callbacks.py — that file is an
architectural lock-in; don't touch it.

Invocation pattern mirrors test_callbacks.py::test_run_shell_command_returns_none_does_not_block:
    asyncio.run(rc._on_post_tool_call("invoke_agent", {"agent_name": "x"}, result, 123.0))
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch


def _call_post(rc, result, tool_args=None):
    """Invoke _on_post_tool_call with positional args matching the hook signature.

    Signature: (tool_name, tool_args, result, duration_ms, context=None)
    """
    if tool_args is None:
        tool_args = {"agent_name": "x"}
    return asyncio.run(rc._on_post_tool_call("invoke_agent", tool_args, result, 123.0))


def test_json_string_result_emits_agent_response_event(reloaded_telemetry, monkeypatch):
    """Core bug (swp-9pc0): result is a JSON string → AgentResponseEvent must fire.

    Before the fix, _get() used getattr() on the string, which returned None
    for "response", so AgentResponseEvent was never emitted even with the
    STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES flag set.
    """
    monkeypatch.setenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", "1")

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted = []
    with patch(
        "code_puppy.plugins.telemetry_ndjson.writer.emit",
        side_effect=emitted.append,
    ):
        result_str = json.dumps(
            {"response": "hi there", "agent_name": "x", "error": None}
        )
        _call_post(rc, result_str)

    types = [e.type for e in emitted]
    assert "agent_invoke_complete" in types, (
        f"Missing agent_invoke_complete in emitted types: {types}"
    )

    response_events = [e for e in emitted if e.type == "agent_response"]
    assert len(response_events) == 1, (
        f"Expected 1 AgentResponseEvent, got {len(response_events)}. "
        f"All emitted types: {types}"
    )
    assert response_events[0].text == "hi there", (
        f"Expected text='hi there', got {response_events[0].text!r}"
    )


def test_dict_result_still_emits_agent_response_event(reloaded_telemetry, monkeypatch):
    """Pre-existing path: dict result → AgentResponseEvent still fires (no regression)."""
    monkeypatch.setenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", "1")

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted = []
    with patch(
        "code_puppy.plugins.telemetry_ndjson.writer.emit",
        side_effect=emitted.append,
    ):
        _call_post(rc, {"response": "hi", "agent_name": "x"})

    response_events = [e for e in emitted if e.type == "agent_response"]
    assert len(response_events) == 1, (
        f"Expected 1 AgentResponseEvent from dict result, got {len(response_events)}"
    )
    assert response_events[0].text == "hi"


def test_malformed_string_no_crash_no_response_event(reloaded_telemetry, monkeypatch):
    """Malformed JSON string: no crash, no AgentResponseEvent; AgentInvokeCompleteEvent still fires."""
    monkeypatch.setenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", "1")

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted = []
    with patch(
        "code_puppy.plugins.telemetry_ndjson.writer.emit",
        side_effect=emitted.append,
    ):
        _call_post(rc, "not json {{")

    types = [e.type for e in emitted]
    assert "agent_invoke_complete" in types, (
        f"AgentInvokeCompleteEvent missing for malformed JSON — got: {types}"
    )

    complete_events = [e for e in emitted if e.type == "agent_invoke_complete"]
    assert complete_events[0].targetOtter == "unknown", (
        f"Expected targetOtter='unknown', got {complete_events[0].targetOtter!r}"
    )

    response_events = [e for e in emitted if e.type == "agent_response"]
    assert len(response_events) == 0, (
        f"AgentResponseEvent must NOT fire for malformed JSON string, got {len(response_events)}"
    )


def test_env_flag_off_json_string_no_response_event(reloaded_telemetry, monkeypatch):
    """Env flag OFF + JSON-string result → only AgentInvokeCompleteEvent, no AgentResponseEvent."""
    # Explicitly ensure the flag is absent — paranoid but correct
    monkeypatch.delenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", raising=False)

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted = []
    with patch(
        "code_puppy.plugins.telemetry_ndjson.writer.emit",
        side_effect=emitted.append,
    ):
        result_str = json.dumps({"response": "hi", "agent_name": "x", "error": None})
        _call_post(rc, result_str)

    types = [e.type for e in emitted]
    assert "agent_invoke_complete" in types, (
        f"AgentInvokeCompleteEvent must fire even with flag off — got: {types}"
    )

    response_events = [e for e in emitted if e.type == "agent_response"]
    assert len(response_events) == 0, (
        f"AgentResponseEvent must NOT fire when env flag is off, got {len(response_events)}"
    )


def test_none_result_no_crash(reloaded_telemetry, monkeypatch):
    """None result: no crash, AgentInvokeCompleteEvent with targetOtter='unknown', no AgentResponseEvent."""
    monkeypatch.setenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", "1")

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc

    emitted = []
    with patch(
        "code_puppy.plugins.telemetry_ndjson.writer.emit",
        side_effect=emitted.append,
    ):
        _call_post(rc, None)

    complete_events = [e for e in emitted if e.type == "agent_invoke_complete"]
    assert len(complete_events) == 1, (
        f"Expected 1 AgentInvokeCompleteEvent for None result, got {len(complete_events)}"
    )
    assert complete_events[0].targetOtter == "unknown", (
        f"Expected targetOtter='unknown' for None result, got {complete_events[0].targetOtter!r}"
    )

    response_events = [e for e in emitted if e.type == "agent_response"]
    assert len(response_events) == 0, (
        f"AgentResponseEvent must NOT fire for None result, got {len(response_events)}"
    )
