"""Integration smoke tests for the telemetry_ndjson plugin.

Philosophy (Charles's integration-first rule): fire the real hooks through the
real registered callbacks, verify the actual NDJSON file. Unit tests in
test_writer.py and test_callbacks.py are regression guards for specific edge
cases, not substitutes for this end-to-end path.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from code_puppy.callbacks import _trigger_callbacks, _trigger_callbacks_sync
from code_puppy.plugins.telemetry_ndjson.otter_event import (
    OtterEvent,
    ToolCompleteEvent,
)

from .conftest import _TELEMETRY_PHASES

# ---------------------------------------------------------------------------
# Fake pydantic-ai part objects for stream_event duck-typing
# ---------------------------------------------------------------------------


class _FakeReasoningPart:
    """Mimics a pydantic-ai ThinkingPart/ReasoningPart (has content attr)."""

    content = "Step 1: analyse the requirements. Step 2: write the code."


class _FakePartEndEvent:
    """Mimics a PartEndEvent wrapper (has .part attr)."""

    part = _FakeReasoningPart()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_env_var_unset_zero_io(tmp_path, monkeypatch):
    """When env var is unset, emit() is a no-op and no file is created."""
    monkeypatch.delenv("STACKWRIGHT_TELEMETRY_NDJSON", raising=False)

    from code_puppy.callbacks import clear_callbacks

    for phase in _TELEMETRY_PHASES:
        clear_callbacks(phase)

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc_mod
    import code_puppy.plugins.telemetry_ndjson.writer as w_mod

    importlib.reload(w_mod)
    importlib.reload(rc_mod)

    assert w_mod.is_enabled() is False, "expected writer disabled when env unset"

    # Fire a real hook — should be a silent no-op
    asyncio.run(
        _trigger_callbacks("pre_tool_call", "read_file", {"path": "/etc/hosts"}, None)
    )

    # No NDJSON file anywhere in tmp_path
    ndjson_files = list(tmp_path.glob("*.ndjson"))
    assert ndjson_files == [], f"unexpected files: {ndjson_files}"

    # Cleanup
    if w_mod._fh is not None:
        try:
            w_mod._fh.close()
        except Exception:
            pass


def test_full_hook_roundtrip(reloaded_telemetry):
    """Fire one of each hook; verify NDJSON is valid OtterEvents with correct types."""
    ndjson_path = reloaded_telemetry

    # Fire each hook once ────────────────────────────────────────────────────
    asyncio.run(
        _trigger_callbacks(
            "pre_tool_call", "cp_read_file", {"path": "/README.md"}, None
        )
    )
    asyncio.run(
        _trigger_callbacks(
            "post_tool_call",
            "cp_read_file",
            {"path": "/README.md"},
            {"content": "# Hello"},
            42.0,
            None,
        )
    )
    asyncio.run(
        _trigger_callbacks(
            "agent_run_start", "planning-agent", "claude-sonnet-4", "sess-abc"
        )
    )
    asyncio.run(
        _trigger_callbacks(
            "agent_run_end",
            "planning-agent",
            "claude-sonnet-4",
            "sess-abc",
            True,
            None,
            "All tasks complete.",  # response_text → also emits AgentResponseEvent
            {"duration_sec": 3.7},
        )
    )
    asyncio.run(
        _trigger_callbacks("run_shell_command", None, "git status --short", None, 30)
    )
    # Two file_permission calls: one read, one write
    _trigger_callbacks_sync(
        "file_permission", None, "/project/src/main.py", "read", None, None, None
    )
    _trigger_callbacks_sync(
        "file_permission", None, "/project/src/main.py", "write", None, None, None
    )
    # stream_event with a fake Reasoning-typed inner part
    asyncio.run(
        _trigger_callbacks("stream_event", "part_end", _FakePartEndEvent(), "sess-abc")
    )

    # Read and parse ─────────────────────────────────────────────────────────
    assert ndjson_path.exists(), "NDJSON file was not created"
    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert lines, "NDJSON file is empty"

    # Every line must be valid JSON
    parsed = [json.loads(line) for line in lines]

    # seq must be strictly monotonically increasing
    seqs = [p["seq"] for p in parsed]
    for i in range(len(seqs) - 1):
        assert seqs[i] < seqs[i + 1], f"seq not monotonic at position {i}: {seqs}"

    # Every event must round-trip through the OtterEvent Pydantic model
    adapter = TypeAdapter(OtterEvent)
    for line in lines:
        adapter.validate_json(line)  # raises ValidationError on schema violation

    # Required event types must be present
    types_emitted = {p["type"] for p in parsed}
    required = {
        "tool_call",
        "tool_complete",
        "agent_invoke_start",
        "agent_invoke_complete",
        "shell_command",
        "file_read",
        "file_write",
    }
    missing = required - types_emitted
    assert not missing, f"missing required event types: {missing}"

    # ADR-002 contract: raw_log must NEVER appear
    assert "raw_log" not in types_emitted, "raw_log emitted — ADR-002 violation!"


def test_tool_complete_success_failure(reloaded_telemetry):
    """post_tool_call maps result dicts to success correctly (flat shape).

    Locks the contract that error-bearing results → success=False and
    clean results → success=True.  Fields live at root (no nested payload).
    Regression guard for ADR δ + Phase 3 flattening.
    """
    ndjson_path = reloaded_telemetry
    adapter = TypeAdapter(OtterEvent)

    # Error result → success=False
    asyncio.run(
        _trigger_callbacks(
            "post_tool_call",
            "cp_shell",
            {},
            {"error": "command not found"},
            5.0,
            None,
        )
    )
    # Success result → success=True
    asyncio.run(
        _trigger_callbacks(
            "post_tool_call",
            "cp_shell",
            {},
            {"stdout": "ok", "exit_code": 0},
            5.0,
            None,
        )
    )

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, f"expected 2 events, got {len(lines)}"

    ev_fail = adapter.validate_json(lines[0])
    assert isinstance(ev_fail, ToolCompleteEvent)
    assert ev_fail.success is False, "expected success=False for error result"

    ev_ok = adapter.validate_json(lines[1])
    assert isinstance(ev_ok, ToolCompleteEvent)
    assert ev_ok.success is True, "expected success=True for clean result"


# ---------------------------------------------------------------------------
# Sub-agent translation tests (invoke_agent → agent_invoke_start/complete)
# ---------------------------------------------------------------------------


def test_invoke_agent_emits_agent_invoke_start_not_tool_call(reloaded_telemetry):
    """pre_tool_call with 'invoke_agent' must emit agent_invoke_start, NOT tool_call.

    Regression guard: the plugin-side translation (SUBAGENT_TOOLS gate) must
    suppress the generic tool_call path so the Pro side doesn't double-count.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    ndjson_path = reloaded_telemetry

    asyncio.run(
        rc._on_pre_tool_call(
            "invoke_agent",
            {"agent_name": "qa-kitten", "prompt": "go"},
        )
    )

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"expected exactly 1 event, got {len(lines)}: {lines}"

    ev = json.loads(lines[0])
    assert ev["type"] == "agent_invoke_start", f"wrong type: {ev['type']}"
    assert ev["targetOtter"] == "qa-kitten"

    types = {json.loads(line)["type"] for line in lines}
    assert "tool_call" not in types, (
        "tool_call must be suppressed for sub-agent dispatch"
    )


def test_invoke_agent_with_model_carries_model_field(reloaded_telemetry):
    """invoke_agent_with_model passes model_name through to AgentInvokeStartEvent.model."""
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    ndjson_path = reloaded_telemetry

    asyncio.run(
        rc._on_pre_tool_call(
            "invoke_agent_with_model",
            {"agent_name": "obsidian-agent", "prompt": "x", "model_name": "gpt-4o"},
        )
    )

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    ev = json.loads(lines[0])
    assert ev["type"] == "agent_invoke_start"
    assert ev["targetOtter"] == "obsidian-agent"
    assert ev["model"] == "gpt-4o"


def test_invoke_agent_complete_emits_agent_invoke_complete_not_tool_complete(
    reloaded_telemetry,
):
    """post_tool_call with 'invoke_agent' emits agent_invoke_complete, NOT tool_complete.

    Also verifies success=True when result.error is None, and durationSec conversion.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    ndjson_path = reloaded_telemetry
    result = SimpleNamespace(
        agent_name="qa-kitten", error=None, response="hello", model_name=None
    )

    asyncio.run(
        rc._on_post_tool_call(
            "invoke_agent",
            {"agent_name": "qa-kitten", "prompt": "x"},
            result,
            1234.0,
        )
    )

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    # 2 events: agent_invoke_complete + token_update (per-subagent telemetry;
    # response="hello" is non-empty so Option B fallback fires).
    assert len(lines) == 2, f"expected 2 events (complete + token_update), got {len(lines)}"

    ev = json.loads(lines[0])
    assert ev["type"] == "agent_invoke_complete"
    assert ev["success"] is True
    assert ev["targetOtter"] == "qa-kitten"
    assert ev["durationSec"] == pytest.approx(1.234)

    types = {json.loads(line)["type"] for line in lines}
    assert "tool_complete" not in types, (
        "tool_complete must be suppressed for sub-agent"
    )


def test_invoke_agent_complete_propagates_error_as_success_false(reloaded_telemetry):
    """When result.error is non-None, agent_invoke_complete carries success=False."""
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    ndjson_path = reloaded_telemetry
    result = SimpleNamespace(
        agent_name="qa-kitten", error="boom", response=None, model_name=None
    )

    asyncio.run(rc._on_post_tool_call("invoke_agent", {}, result, 500.0))

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    ev = json.loads(lines[0])
    assert ev["type"] == "agent_invoke_complete"
    assert ev["success"] is False


def test_invoke_agent_complete_handles_dict_result(reloaded_telemetry):
    """The _get() helper in post_tool_call handles plain dict results (not just attrs).

    Confirms that both AgentInvokeOutput-style objects AND dict results from
    agents that return plain dicts are handled without AttributeError.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    ndjson_path = reloaded_telemetry
    result = {
        "agent_name": "qa-kitten",
        "error": None,
        "response": "hello",
        "model_name": None,
    }

    asyncio.run(rc._on_post_tool_call("invoke_agent", {}, result, 1000.0))

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    # 2 events: agent_invoke_complete + token_update (response="hello" triggers Option B).
    assert len(lines) == 2

    ev = json.loads(lines[0])
    assert ev["type"] == "agent_invoke_complete"
    assert ev["success"] is True
    assert ev["targetOtter"] == "qa-kitten"


def test_subagent_response_env_var_off_skips_response_event(
    reloaded_telemetry, monkeypatch
):
    """With STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES unset, no agent_response.

    Only agent_invoke_complete should be emitted — response text is dropped
    because sub-agent responses can be large and are off by default.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    monkeypatch.delenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", raising=False)
    ndjson_path = reloaded_telemetry
    result = SimpleNamespace(
        agent_name="qa-kitten", error=None, response="hello world", model_name=None
    )

    asyncio.run(rc._on_post_tool_call("invoke_agent", {}, result, 100.0))

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    # 2 events: agent_invoke_complete + token_update. AgentResponseEvent is suppressed
    # (SUBAGENT_RESPONSES flag is off) but token telemetry always fires when response
    # text is non-empty (Option B fallback).
    assert len(lines) == 2, (
        f"expected 2 events (complete + token_update, no response), got {len(lines)}: {lines}"
    )
    types = {json.loads(line)["type"] for line in lines}
    assert "agent_invoke_complete" in types
    assert "agent_response" not in types


def test_subagent_response_env_var_on_emits_response_event(
    reloaded_telemetry, monkeypatch
):
    """STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES=1 enables AgentResponseEvent.

    Both agent_invoke_complete AND agent_response must appear; the response
    event carries the sub-agent's .response text.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    monkeypatch.setenv("STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES", "1")
    ndjson_path = reloaded_telemetry
    result = SimpleNamespace(
        agent_name="qa-kitten", error=None, response="hello world", model_name=None
    )

    asyncio.run(rc._on_post_tool_call("invoke_agent", {}, result, 100.0))

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    # 3 events: agent_invoke_complete, token_update, agent_response.
    # token_update is emitted by per-subagent telemetry (Option B fallback on response text).
    assert len(lines) == 3, (
        f"expected 3 events (complete + token_update + response), got {len(lines)}: {lines}"
    )

    events = [json.loads(line) for line in lines]
    types = {e["type"] for e in events}
    assert "agent_invoke_complete" in types
    assert "agent_response" in types

    response_ev = next(e for e in events if e["type"] == "agent_response")
    assert response_ev["text"] == "hello world"


def test_non_subagent_tool_still_emits_tool_call_and_complete(reloaded_telemetry):
    """Non-sub-agent tools still produce tool_call + tool_complete (flat shape).

    Regression guard: the SUBAGENT_TOOLS gate must be an early return, not a
    full replacement of the normal emit path.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    ndjson_path = reloaded_telemetry

    asyncio.run(rc._on_pre_tool_call("read_file", {"path": "/foo"}))
    asyncio.run(
        rc._on_post_tool_call("read_file", {"path": "/foo"}, {"content": "hello"}, 50.0)
    )

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, f"expected 2 events, got {len(lines)}"

    events = [json.loads(line) for line in lines]
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_complete" in types

    # Verify flat shape on tool_complete — old nested .payload must be gone
    tc_ev = next(e for e in events if e["type"] == "tool_complete")
    assert tc_ev["toolName"] == "read_file"
    assert tc_ev["success"] is True
    assert tc_ev["durationMs"] == pytest.approx(50.0)
    assert "payload" not in tc_ev, (
        "nested payload shape must be gone (Phase 3 breaking change)"
    )
