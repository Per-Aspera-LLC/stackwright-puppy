"""Regenerate sample-raft-puppy-events.ndjson — run from repo root.

Usage::

    python tests/plugins/telemetry_ndjson/fixtures/regenerate_sample.py

Produces one event of every OtterEvent variant that raft-puppy can emit.
This fixture is pulled by swp-hpfa (Pro CI bead) and validated against
``@stackwright-pro/telemetry``'s Zod schema.

Re-run whenever the otter_event.py schema changes (new variant added,
field renamed, etc.) and commit the updated fixture.

Bead: code_puppy-vbt  |  ADR-002: ../stackwright/otter-viz/docs/schema-ownership-decision.md
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# Run from repo root — ensure package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

_OUT = Path(__file__).resolve().parent / "sample-raft-puppy-events.ndjson"


def main() -> None:
    # 1. Wipe previous file BEFORE the writer opens it (avoid unlink-after-open trap)
    if _OUT.exists():
        _OUT.unlink()

    # 2. Set env var, then reload writer so _init() picks it up fresh
    os.environ["STACKWRIGHT_TELEMETRY_NDJSON"] = str(_OUT)

    import code_puppy.plugins.telemetry_ndjson.writer as writer_mod

    importlib.reload(writer_mod)

    if not writer_mod.is_enabled():
        print("ERROR: writer is not enabled after reload — check env var / path")
        sys.exit(1)

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

    def _e(event) -> None:
        writer_mod.emit(event)

    # 3. Emit one of every variant ─────────────────────────────────────────

    # 1. ThinkingEvent
    _e(ThinkingEvent(ts=now_iso(), seq=next_seq(), text="Analysing the task..."))

    # 2. ReasoningDumpEvent
    _e(
        ReasoningDumpEvent(
            ts=now_iso(),
            seq=next_seq(),
            text="Step 1: read requirements. Step 2: implement. Step 3: test.",
        )
    )

    # 3. AgentResponseEvent
    _e(
        AgentResponseEvent(
            ts=now_iso(),
            seq=next_seq(),
            text="I have completed the implementation and all tests pass.",
        )
    )

    # 4. ShellCommandEvent
    _e(
        ShellCommandEvent(
            ts=now_iso(),
            seq=next_seq(),
            command="git status --short",
            timeoutSec=30,
        )
    )

    # 5. FileReadEvent
    _e(FileReadEvent(ts=now_iso(), seq=next_seq(), path="/project/src/main.py"))

    # 6. FileWriteEvent
    _e(
        FileWriteEvent(
            ts=now_iso(), seq=next_seq(), path="/project/src/main.py", bytes=1024
        )
    )

    # 7. ToolCallEvent
    _e(
        ToolCallEvent(
            ts=now_iso(),
            seq=next_seq(),
            toolName="cp_read_file",
            args={"file_path": "/project/README.md"},
        )
    )

    # 8. ToolCompleteEvent (flat layout — matches @stackwright-pro/telemetry ToolCompleteSchema)
    _e(
        ToolCompleteEvent(
            ts=now_iso(),
            seq=next_seq(),
            toolName="cp_read_file",
            success=True,
            durationMs=12.5,
        )
    )

    # 9. AgentInvokeStartEvent
    _e(
        AgentInvokeStartEvent(
            ts=now_iso(),
            seq=next_seq(),
            targetOtter="planning-agent",
            model="claude-sonnet-4-5",
        )
    )

    # 10. AgentInvokeCompleteEvent
    _e(
        AgentInvokeCompleteEvent(
            ts=now_iso(),
            seq=next_seq(),
            targetOtter="planning-agent",
            durationSec=3.7,
            success=True,
            model="claude-sonnet-4-5",
        )
    )

    # 11. TokenUpdateEvent (not yet emitted via any hook — emitted directly)
    _e(
        TokenUpdateEvent(
            ts=now_iso(),
            seq=next_seq(),
            used=4096,
            total=32768,
            percentUsed=12.5,
        )
    )

    # 4. Flush and verify ──────────────────────────────────────────────────
    if writer_mod._fh is not None:
        writer_mod._fh.flush()
        writer_mod._fh.close()

    lines = _OUT.read_text(encoding="utf-8").splitlines()
    print(f"Wrote {len(lines)} events to {_OUT}")
    for line in lines:
        e = json.loads(line)
        print(f"  seq={e['seq']:3d}  type={e['type']}")


if __name__ == "__main__":
    main()
