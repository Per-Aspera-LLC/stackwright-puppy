# Changelog — telemetry_ndjson

All notable changes to this plugin follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
