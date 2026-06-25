"""Defensive unit tests for telemetry_ndjson writer.py.

Added as regression guards for failure modes that the integration smoke test
cannot exercise reliably: concurrent write ordering and graceful OSError
degradation. (Charles's rule: unit tests guard lessons learned from integration
failures — not prophylactic coverage targets.)
"""

from __future__ import annotations

import json
import logging
import threading
from unittest.mock import MagicMock


def test_concurrent_writes_no_corruption(reloaded_telemetry):
    """4 threads × 250 emits = 1000 lines, each valid JSON with a unique seq.

    Verifies that the threading.Lock in writer.emit() prevents interleaved
    writes, which would corrupt NDJSON lines.
    """
    import code_puppy.plugins.telemetry_ndjson.writer as w_mod
    from code_puppy.plugins.telemetry_ndjson.otter_event import (
        ToolCallEvent,
        next_seq,
        now_iso,
    )

    ndjson_path = reloaded_telemetry
    assert w_mod.is_enabled(), "writer must be enabled for this test"

    def _worker():
        for _ in range(250):
            w_mod.emit(
                ToolCallEvent(ts=now_iso(), seq=next_seq(), toolName="read_file")
            )

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1000, f"expected 1000 lines, got {len(lines)}"

    seqs = set()
    for line in lines:
        parsed = json.loads(line)  # raises on truncated / interleaved line
        seqs.add(parsed["seq"])

    assert len(seqs) == 1000, f"seq collisions detected: only {len(seqs)} unique"


def test_graceful_degradation_on_oserror(reloaded_telemetry, monkeypatch, caplog):
    """emit() must not raise when the file handle's write() throws OSError.

    Also verifies subsequent calls don't raise (the handler stays alive and
    keeps trying — no circuit-breaker in Phase 3).
    """
    import code_puppy.plugins.telemetry_ndjson.writer as w_mod
    from code_puppy.plugins.telemetry_ndjson.otter_event import (
        ToolCallEvent,
        next_seq,
        now_iso,
    )

    assert w_mod.is_enabled(), "writer must be enabled for this test"

    # Replace the real file handle with a mock whose .write() raises
    mock_fh = MagicMock()
    mock_fh.write.side_effect = OSError("disk full")
    monkeypatch.setattr(w_mod, "_fh", mock_fh)

    event = ToolCallEvent(ts=now_iso(), seq=next_seq(), toolName="read_file")

    with caplog.at_level(
        logging.WARNING, logger="code_puppy.plugins.telemetry_ndjson.writer"
    ):
        w_mod.emit(event)  # must NOT raise

    assert "write failed" in caplog.text, "expected warning log on write failure"

    # Subsequent call also must not raise
    w_mod.emit(event)
