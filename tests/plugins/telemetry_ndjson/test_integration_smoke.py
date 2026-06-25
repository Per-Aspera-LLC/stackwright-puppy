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
    """post_tool_call maps result dicts to payload.success correctly.

    Locks the contract that error-bearing results → success=False and
    clean results → success=True. Regression guard for ADR δ.
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
    assert ev_fail.payload.success is False, "expected success=False for error result"

    ev_ok = adapter.validate_json(lines[1])
    assert isinstance(ev_ok, ToolCompleteEvent)
    assert ev_ok.payload.success is True, "expected success=True for clean result"
