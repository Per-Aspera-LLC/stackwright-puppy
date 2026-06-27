# Changelog — telemetry_ndjson

All notable changes to this plugin follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — tightening pass (planning-agent-13d2f9)

### Fixed

- **Finding 1 — seq resets across restarts (`runId` added).** `_seq_iter` is
  module-level state, so every process restart resets the seq counter to zero.
  Same NDJSON file ended up with multiple runs of seq 0..N interleaved, with no
  way for consumers to distinguish them. Fix: generate a UUID4 once at import
  time (`_RUN_ID`) and add a `runId: str | None` field to `OtterBase`. A Pydantic
  v2 `@model_validator(mode="before")` on `OtterBase` auto-stamps every emitted
  event without requiring call-site changes. The `runId` counter resets naturally
  on process restart — intentional, because a new `runId` marks a new run.
  `current_run_id()` is exported from `otter_event.__all__` for use by consumers.

  **TODO (Zod side):** Add `runId` to `BaseSchema` in
  `otter-viz/packages/events/src/schemas.ts`. The existing Zod `BaseSchema`
  uses plain `z.object()` (not `.strict()`), so unknown fields are silently
  stripped — `runId` in NDJSON does NOT cause dashboard breakage today. But
  the field should be accepted explicitly for type-safety in otter-viz consumers.

- **Finding 2 — thinking/reasoning events never emitted.** `_on_stream_event`
  was trying to extract content from `PartStartEvent.part` (content is empty
  at that point) and from `PartEndEvent.part` (the attribute doesn't exist on
  `PartEndEvent`). Result: zero `thinking` / `reasoning_dump` events in any
  captured NDJSON. Fix: replaced the broken point-in-time extraction with a
  proper delta accumulator mirroring `subagent_stream_handler.py`'s pattern.
  A `_part_accumulator` dict (keyed by `(agent_session_id, part_index)`) is
  populated on `part_start`, extended on `part_delta`, and flushed on `part_end`.
  `ThinkingEvent` / `ReasoningDumpEvent` are only emitted at `part_end` with the
  full accumulated text. Empty parts are skipped. A `threading.Lock` guards the
  accumulator for safety.

- **Finding 4 — `{"raw": ""}` args noise.** MCP tools with no arguments produced
  `args: {"raw": ""}` in `tool_call` events — a JSON-parse fallback from
  `pydantic_patches.py`. Added a one-line normalization in `_on_pre_tool_call`:
  if `tool_args_raw == {"raw": ""}`, treat it as `None`; the `args` field is
  then absent from NDJSON output (via `exclude_none=True`).

### Added

- **Task 3 — Token usage telemetry (new feature).** `_on_agent_run_end` now
  emits a `TokenUpdateEvent` after the `AgentInvokeCompleteEvent`. Data source:
  `AgentRunStats.get_last_cycle_stats()["output_tokens"]` (lazy import to avoid
  import-cycle risk). A module-level `_cumulative_output_tokens` counter
  accumulates across cycles, giving Grafana a monotonically-increasing series
  rather than per-cycle deltas. `total` is read from
  `code_puppy.config.get_protected_token_count()` with a 200 000 fallback.
  `otter` is stamped with the agent name so per-otter Grafana queries work.

  **Caveat:** `run_stats.py` early-returns when `is_subagent()` is True, so
  sub-agent token counts are NOT separately tracked — we get top-level
  (foreman) cycle data only. Per-otter aggregation is a future enhancement
  that would require `run_stats.py` to be extended for sub-agents. The
  `_cumulative_output_tokens` counter resets on process restart, which is
  correct: new `runId` = new cumulative series.

  **TODO (future):** Add per-otter aggregation to `run_stats.py` so sub-agent
  token counts can be broken out in Grafana.

### Added (continuation — per-sub-agent token telemetry, planning-agent-13d2f9)

- **Per-sub-agent `TokenUpdateEvent` emission.** Each sub-agent invocation now
  emits its own `TokenUpdateEvent` stamped with `otter=<sub-agent name>`,
  enabling per-otter Grafana series in the otter-viz dashboard. Token counts are
  heuristic estimates (~chars/2.5). Two sources are tried in order:
  - **Option A (primary):** reads
    `SubAgentConsoleManager.get_agent_state(session_id).token_count`, which
    `subagent_stream_handler.py` live-updates from `content_delta` events using
    the same `~chars/2.5` heuristic that `BaseAgent.estimate_token_count` uses
    for compaction decisions.
  - **Option B (fallback):** if Option A returns 0/None (state already
    unregistered or no `session_id` on result), estimates from
    `len(response_text) / 2.5`. Lower-bound only — doesn't count
    thinking/reasoning tokens.

  Both top-level (foreman, via `_on_agent_run_end`) and sub-agent (via
  `_on_post_tool_call`) emissions now share a single
  `_bump_and_emit_token_update()` helper, keeping the monotonically-increasing
  `_cumulative_output_tokens` counter consistent. The existing `_token_lock` was
  renamed to `_cumulative_lock` for clarity.

  Once `run_stats.py` is extended for sub-agents (see TODO above), this path
  can be upgraded from heuristic to exact counts with no schema change — swap
  the data source inside `_bump_and_emit_token_update`.

## [Unreleased] — previous changes

### Fixed

- **Wire-format parity with TS emitter**: writer now calls `event.model_dump_json(by_alias=True, exclude_none=True)` so optional fields with `None` values (e.g. `phase`, `otter`, `timeoutSec`, `bytes`, `args`, `model`, `durationSec`) are omitted from NDJSON output instead of serialized as `null`. Matches the behavior of the TypeScript `@stackwright-pro/telemetry` emitter — downstream consumers were diverging on null-key presence. Pure addition: any consumer using `.get()` semantics is unaffected.

- **Fix**: Deserialize JSON-string `result` in `_on_post_tool_call` so `AgentResponseEvent` actually fires when `STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES=1`. Pydantic-AI's `_call_tool` returns the sub-agent payload as a JSON string, which `getattr()` could not navigate. (beads `swp-9pc0`)

### Changed

- **Sub-agent attribution semantics corrected (replaces in-progress
  `targetOtter` stamping from earlier in this Unreleased cycle).** The
  intra-sub-agent attribution work now stamps the existing **`otter`** field
  on `OtterBase` (semantically "who emitted this event" — the caller) instead
  of adding a `targetOtter` field to non-invocation event types (where
  callee semantics make no sense). Affected event types: `ToolCallEvent`,
  `ToolCompleteEvent`, `ShellCommandEvent`, `FileReadEvent`, `FileWriteEvent`,
  `ThinkingEvent`, `ReasoningDumpEvent`, `AgentResponseEvent`. The field
  populates only when emitted from within a sub-agent's execution context
  (via `code_puppy.tools.subagent_context` ContextVar); top-level events
  leave `otter` unset and the key is omitted from JSON via `exclude_none=True`.

  `AgentInvokeStartEvent` and `AgentInvokeCompleteEvent` now stamp **both**
  fields: `targetOtter` (callee, the sub-agent being invoked — unchanged) and
  `otter` (caller, populated when one sub-agent invokes another, useful for
  reconstructing nested invocation chains).

  Net schema impact vs upstream Pro: **zero** — `otter` was already declared
  on `OtterBase`. The `targetOtter` field is no longer added to non-invocation
  event types.

## [1.1.0] — Unreleased

### Changed

- **BREAKING (wire format):** `ToolCompleteEvent` is now **flat**.
  Previously emitted:
  ```json
  {"type": "tool_complete", "payload": {"toolName": "...", "success": true, "durationMs": 42.0}}
  ```
  Now emits:
  ```json
  {"type": "tool_complete", "toolName": "...", "success": true, "durationMs": 42.0}
  ```
  Aligns with `@stackwright-pro/telemetry` `ToolCompleteSchema` (Pro design
  decision: paired events share the same root layout — see
  `../pro/packages/telemetry/src/schemas.ts`).  The `ToolCompletePayload`
  Pydantic class has been removed entirely.

- `AgentInvokeCompleteEvent.durationSec` is now **optional** (`float | None`,
  default `None`).  Previously required and defaulted to `0.0` when upstream
  metadata lacked timing — that fabricated value poisoned downstream timing
  aggregations.  Now omitted when unknown, matching Pro's
  `AgentInvokeCompleteSchema` which declares `durationSec` as `.optional()`.
  The `_on_agent_run_end` handler in `register_callbacks.py` has been updated
  accordingly.

### Added

- **Sub-agent telemetry:** `invoke_agent` and `invoke_agent_with_model` tool
  calls are now translated into `agent_invoke_start` / `agent_invoke_complete`
  events instead of the generic `tool_call` / `tool_complete` events.
  `targetOtter` carries the sub-agent name; `model` carries any per-call model
  override.  The generic events are suppressed for those tool names to avoid
  double-counting on the Pro side.

- **`STACKWRIGHT_TELEMETRY_NDJSON_SUBAGENT_RESPONSES` env var** (default off):
  when set to `1` / `true` / `yes` / `on`, the plugin additionally emits an
  `AgentResponseEvent` carrying the sub-agent's `.response` text after each
  `agent_invoke_complete`.  Off by default because sub-agent responses can be
  very large and noisy in NDJSON logs.

### Notes

- The `invoke_agent` core callback hook in `code_puppy/callbacks.py` is
  currently **dead code** — `_trigger_callbacks("invoke_agent", ...)` is never
  called anywhere in raft-puppy (notably absent from `_invoke_agent_impl` in
  `code_puppy/tools/subagent_invocation.py`).  Sub-agent telemetry emission is
  implemented plugin-side via `pre_tool_call` / `post_tool_call` translation as
  a clean workaround.  A follow-up task should either wire that hook up in
  `_invoke_agent_impl`, or remove it from `callbacks.py` as dead code.

## [1.0.0] — Initial release

- Phase 3 plugin: NDJSON telemetry emission via OtterEvent Pydantic schema
  mirroring `../pro/packages/telemetry/src/schemas.ts`.
- Hooks: `pre_tool_call`, `post_tool_call`, `agent_run_start`, `agent_run_end`,
  `stream_event`, `run_shell_command`, `file_permission`.
