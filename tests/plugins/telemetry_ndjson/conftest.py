"""Shared fixtures for telemetry_ndjson plugin tests."""

from __future__ import annotations

import importlib

import pytest

# The 7 callback phases this plugin subscribes to.
# Used by fixtures to clear-before-reload so we get exactly one handler
# generation (not two after reloads — reload creates new function objects
# that bypass the identity-based dedup in register_callback).
_TELEMETRY_PHASES = (
    "pre_tool_call",
    "post_tool_call",
    "agent_run_start",
    "agent_run_end",
    "stream_event",
    "run_shell_command",
    "file_permission",
)


@pytest.fixture
def reloaded_telemetry(tmp_path, monkeypatch):
    """Set STACKWRIGHT_TELEMETRY_NDJSON, reload writer + register_callbacks.

    Yields the resolved ``pathlib.Path`` for the NDJSON output file.

    Isolation contract
    ------------------
    1. Set env var via monkeypatch (auto-restored after test).
    2. Clear the 7 callback phases we subscribe to — prevents double-registration
       from stale function-object generations left over from prior reloads.
       (This is safe because the root conftest autouse fixture snapshots and
        restores the full callback registry around every test.)
    3. Reload ``writer`` so ``_init()`` picks up the fresh env var.
    4. Reload ``register_callbacks`` so fresh handlers are registered.
    5. Teardown: close the file handle to avoid Windows file-lock issues.
    """
    ndjson_path = tmp_path / "events.ndjson"
    monkeypatch.setenv("STACKWRIGHT_TELEMETRY_NDJSON", str(ndjson_path))

    from code_puppy.callbacks import clear_callbacks

    for phase in _TELEMETRY_PHASES:
        clear_callbacks(phase)

    import code_puppy.plugins.telemetry_ndjson.register_callbacks as rc_mod
    import code_puppy.plugins.telemetry_ndjson.writer as w_mod

    importlib.reload(w_mod)
    importlib.reload(rc_mod)

    yield ndjson_path

    # Best-effort close to prevent Windows file-lock issues
    if w_mod._fh is not None:
        try:
            w_mod._fh.close()
        except Exception:
            pass
