"""Pydantic mirror of the OtterEvent schema for raft-puppy telemetry emission.

Canonical Zod source:
    ../stackwright/otter-viz/packages/events/src/schemas.ts
Schema ownership decision:
    ../stackwright/otter-viz/docs/schema-ownership-decision.md  (ADR-002)
Bead:
    code_puppy-vbt

## Variants intentionally NOT mirrored

- ``RawLogSchema`` — ADR-002 §3: bridge-only sentinel. Producers (including
  raft-puppy) must never emit ``raw_log``; leaving it un-typeable here is the
  compile-time enforcement.
- ``PhaseStartSchema``, ``PhaseCompleteSchema`` — emitted by Pro via bead
  swp-im3c, not by raft-puppy.
- ``PipelineStateChangeSchema`` — Pro-side emission only.

## Field naming convention

camelCase field names mirror the Zod schema 1-to-1 so that JSON output is
wire-compatible without alias plumbing. The canonical serialization call is
``model_dump_json(by_alias=True, exclude_none=True)``: ``by_alias=True`` is a
no-op today but is kept for forward-compat, and ``exclude_none=True`` matches
the TypeScript emitter, which omits absent optional fields rather than writing
``null``.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Thread-safe monotonic sequence counter
# ---------------------------------------------------------------------------

_seq_iter = itertools.count(0)
_seq_lock = threading.Lock()


def next_seq() -> int:
    """Return the next monotonic sequence number. Thread-safe."""
    with _seq_lock:
        return next(_seq_iter)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with timezone suffix."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Base fields shared by every OtterEvent
# ---------------------------------------------------------------------------


class OtterBase(BaseModel):
    """Base fields present on every OtterEvent variant."""

    ts: str = Field(description="ISO-8601 timestamp")
    seq: int = Field(ge=0, description="Monotonic sequence number")
    phase: str | None = Field(
        default=None, description="Current pipeline phase if known"
    )
    otter: str | None = Field(
        default=None,
        description='Which otter emitted this (e.g. "foreman")',
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Event variants — raft-puppy surface only
# ---------------------------------------------------------------------------


class ThinkingEvent(OtterBase):
    """Model thinking / scratchpad text."""

    type: Literal["thinking"] = "thinking"
    text: str


class ReasoningDumpEvent(OtterBase):
    """Extended reasoning dump (e.g. extended-thinking blocks)."""

    type: Literal["reasoning_dump"] = "reasoning_dump"
    text: str


class AgentResponseEvent(OtterBase):
    """Final agent response text."""

    type: Literal["agent_response"] = "agent_response"
    text: str


class ShellCommandEvent(OtterBase):
    """Shell command about to be executed."""

    type: Literal["shell_command"] = "shell_command"
    command: str
    timeoutSec: int | None = None


class FileReadEvent(OtterBase):
    """File read operation."""

    type: Literal["file_read"] = "file_read"
    path: str


class FileWriteEvent(OtterBase):
    """File write / create operation."""

    type: Literal["file_write"] = "file_write"
    path: str
    bytes: int | None = None  # noqa: A003 — matches Zod field name


class ToolCallEvent(OtterBase):
    """Tool invocation start."""

    type: Literal["tool_call"] = "tool_call"
    toolName: str
    args: dict[str, Any] | None = None


class ToolCompleteEvent(OtterBase):
    """Tool invocation completion with timing and success flag.

    **Flat layout** — ``toolName``, ``success``, and ``durationMs`` live at the
    root of this event, with no nested ``payload`` object.  This mirrors the
    canonical ``ToolCompleteSchema`` in
    ``../pro/packages/telemetry/src/schemas.ts``, which adopted the flat shape
    per the Pro README's "paired events use consistent layout" rule: every
    *start* / *complete* pair exposes its primary fields at root level.
    """

    type: Literal["tool_complete"] = "tool_complete"
    toolName: str
    success: bool | None = None
    durationMs: float | None = None


class AgentInvokeStartEvent(OtterBase):
    """Sub-agent invocation starting."""

    type: Literal["agent_invoke_start"] = "agent_invoke_start"
    targetOtter: str
    model: str | None = None


class AgentInvokeCompleteEvent(OtterBase):
    """Sub-agent invocation completed.

    ``durationSec`` is **optional** — omitted when upstream metadata doesn't
    carry timing information.  Defaulting to ``0.0`` when unknown would poison
    downstream timing aggregations; ``None`` is the honest signal.  Matches
    Pro's ``AgentInvokeCompleteSchema`` which declares ``durationSec`` as
    ``.optional()``.
    """

    type: Literal["agent_invoke_complete"] = "agent_invoke_complete"
    targetOtter: str
    durationSec: float | None = None
    success: bool
    model: str | None = None


class TokenUpdateEvent(OtterBase):
    """Context-window token usage snapshot."""

    type: Literal["token_update"] = "token_update"
    used: int
    total: int
    percentUsed: float


# ---------------------------------------------------------------------------
# Discriminated union — all raft-puppy-emitted variants
# ---------------------------------------------------------------------------

OtterEvent = (
    ThinkingEvent
    | ReasoningDumpEvent
    | AgentResponseEvent
    | ShellCommandEvent
    | FileReadEvent
    | FileWriteEvent
    | ToolCallEvent
    | ToolCompleteEvent
    | AgentInvokeStartEvent
    | AgentInvokeCompleteEvent
    | TokenUpdateEvent
)
"""Union of all OtterEvent variants that raft-puppy emits.

Type-hint the writer module against this so adding a new variant here
automatically surfaces as a mypy/pyright error if the writer doesn't handle it.
"""

__all__ = [
    # Helpers
    "next_seq",
    "now_iso",
    # Base
    "OtterBase",
    # Variants
    "ThinkingEvent",
    "ReasoningDumpEvent",
    "AgentResponseEvent",
    "ShellCommandEvent",
    "FileReadEvent",
    "FileWriteEvent",
    "ToolCallEvent",
    "ToolCompleteEvent",
    "AgentInvokeStartEvent",
    "AgentInvokeCompleteEvent",
    "TokenUpdateEvent",
    # Union
    "OtterEvent",
]
