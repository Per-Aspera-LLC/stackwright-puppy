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
wire-compatible without alias plumbing. ``model_dump_json(by_alias=True)`` is
a no-op here but callers should pass it anyway for forward-compat.
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


class ToolCompletePayload(BaseModel):
    """Nested payload for ToolCompleteEvent.

    Keeps the nested-payload shape matching current Zod. bead swp-im3c may
    flatten this on the Pro side in the future — that's Pro's call, not ours.
    """

    toolName: str
    success: bool | None = None
    durationMs: float | None = None

    model_config = {"extra": "forbid"}


class ToolCompleteEvent(OtterBase):
    """Tool invocation completion with timing and success flag."""

    type: Literal["tool_complete"] = "tool_complete"
    payload: ToolCompletePayload


class AgentInvokeStartEvent(OtterBase):
    """Sub-agent invocation starting."""

    type: Literal["agent_invoke_start"] = "agent_invoke_start"
    targetOtter: str
    model: str | None = None


class AgentInvokeCompleteEvent(OtterBase):
    """Sub-agent invocation completed."""

    type: Literal["agent_invoke_complete"] = "agent_invoke_complete"
    targetOtter: str
    durationSec: float
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
    "ToolCompletePayload",
    "ToolCompleteEvent",
    "AgentInvokeStartEvent",
    "AgentInvokeCompleteEvent",
    "TokenUpdateEvent",
    # Union
    "OtterEvent",
]
