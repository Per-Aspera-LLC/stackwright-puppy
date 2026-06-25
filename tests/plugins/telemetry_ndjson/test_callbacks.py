"""Defensive unit tests for telemetry_ndjson register_callbacks.py.

Locks specific architectural decisions:
- invoke_agent hook deliberately NOT subscribed (avoids double-emit).
- file_permission always returns True (observe, never gate).
- run_shell_command always returns None (observe, never block).
"""

from __future__ import annotations

import asyncio


def test_invoke_agent_hook_not_subscribed():
    """None of our handlers must appear in the invoke_agent callback phase.

    This locks the design decision (documented in register_callbacks.py):
    both invoke_agent and agent_run_start fire on sub-agent dispatch;
    subscribing to invoke_agent would cause double-emission.
    """
    import code_puppy.plugins.telemetry_ndjson.register_callbacks  # noqa: F401 — triggers module-level register_callback() calls
    from code_puppy.callbacks import _callbacks

    telemetry_handlers = [
        h
        for h in _callbacks["invoke_agent"]
        if getattr(h, "__module__", "").startswith(
            "code_puppy.plugins.telemetry_ndjson"
        )
    ]
    assert telemetry_handlers == [], (
        f"telemetry_ndjson should NOT subscribe to invoke_agent, "
        f"but found: {telemetry_handlers}"
    )


def test_file_permission_returns_true_does_not_gate(reloaded_telemetry):
    """_on_file_permission must always return True.

    Returning False would silently block file writes mid-agent-run.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    # Call the handler directly — no need to go through the callback registry
    result = rc._on_file_permission(None, "/tmp/foo.py", "write")
    assert result is True, f"expected True from _on_file_permission, got {result!r}"

    result_read = rc._on_file_permission(None, "/tmp/foo.py", "read")
    assert result_read is True

    result_unknown = rc._on_file_permission(None, "/tmp/foo.py", "chmod")
    assert result_unknown is True


def test_run_shell_command_returns_none_does_not_block(reloaded_telemetry):
    """_on_run_shell_command must return None (never {"blocked": True}).

    Returning {"blocked": True} would prevent shell commands from running.
    """
    from code_puppy.plugins.telemetry_ndjson import register_callbacks as rc

    result = asyncio.run(rc._on_run_shell_command(None, "ls -la", None, 60))
    assert result is None, (
        f"_on_run_shell_command must return None (not block), got {result!r}"
    )
