"""NDJSON writer for telemetry events.

Reads STACKWRIGHT_TELEMETRY_NDJSON at import time. If unset, all emit() calls
are no-ops with zero I/O. If set, opens a single line-buffered file handle
guarded by a threading.Lock for the process lifetime.

Never raises out of emit(): graceful degradation per AGENTS.md rule 4.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import IO, Optional

from code_puppy.plugins.telemetry_ndjson.otter_event import OtterEvent

logger = logging.getLogger(__name__)

_ENV_VAR = "STACKWRIGHT_TELEMETRY_NDJSON"
_path: Optional[str] = None
_fh: Optional[IO[str]] = None
_lock = threading.Lock()
_init_failed = False


def _init() -> None:
    """One-time initialization — opens the file if env var is set."""
    global _path, _fh, _init_failed
    _path = os.environ.get(_ENV_VAR) or None
    if _path is None:
        return
    try:
        # Append mode + line buffering. Parent dir must exist (caller's responsibility).
        _fh = open(_path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        logger.info("telemetry_ndjson: writing events to %s", _path)
    except OSError as e:
        logger.warning(
            "telemetry_ndjson: failed to open %s: %s — disabling emit", _path, e
        )
        _init_failed = True
        _fh = None


def emit(event: OtterEvent) -> None:
    """Append one NDJSON-encoded event. No-op if telemetry disabled or init failed."""
    if _fh is None or _init_failed:
        return
    try:
        line = event.model_dump_json(by_alias=True, exclude_none=True) + "\n"
        with _lock:
            _fh.write(line)
            _fh.flush()
    except Exception as e:  # noqa: BLE001 — explicitly broad, graceful degradation
        logger.warning("telemetry_ndjson: write failed: %s", e)


def is_enabled() -> bool:
    """Cheap check used by hook handlers to skip building events when disabled."""
    return _fh is not None and not _init_failed


# Initialize on import — env var is read once at plugin load time.
_init()
