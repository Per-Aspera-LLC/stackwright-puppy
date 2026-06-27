# Wire the `invoke_agent` callback hook

**Status:** Proposed upstream contribution. **Not implemented in raft-puppy**
(see `CHANGES_FROM_UPSTREAM.md` for the fork's core-edit discipline).

**Target:** upstream code-puppy (`github.com/mpfaffenberger/code_puppy`).

**Companion fix:** the `agent_exception` hook has the same dead-code problem
and should be wired in the same change — see the "Companion" section below.

---

## Background

`code_puppy/callbacks.py` defines `on_invoke_agent` and registers an
`"invoke_agent"` slot in `_callbacks`, but no caller anywhere fires
`_trigger_callbacks("invoke_agent", ...)`. Specifically, `_invoke_agent_impl`
in `code_puppy/tools/subagent_invocation.py` — the natural firing site —
does not invoke it.

The hook is publicly documented in `AGENTS.md`'s hooks table, so plugins
reasonably believe it works. They subscribe, get nothing, and either silently
lose observability or (more often) work around it by intercepting
`pre_tool_call` for tool names `invoke_agent` / `invoke_agent_with_model`.
The `telemetry_ndjson` plugin in raft-puppy uses the latter workaround; see
`code_puppy/plugins/telemetry_ndjson/register_callbacks.py` and the
`SUBAGENT_TOOLS` constant therein.

---

## Proposal

### `on_invoke_agent` — suggested signature

```python
async def on_invoke_agent(
    agent_name: str,
    prompt: str,
    session_id: str,
    is_new_session: bool,
    model_name: str | None = None,
) -> List[Any]:
    """Trigger callbacks when a sub-agent invocation begins.

    Fires from _invoke_agent_impl after session_id resolution, before any
    heavy work (agent load, model resolution, MCP autostart). Plugins can
    observe sub-agent dispatch without intercepting the invoke_agent tool
    call directly.

    Args:
        agent_name: Name of the sub-agent being invoked.
        prompt: User prompt for the sub-agent run.
        session_id: Resolved session ID (auto-generated or finalized).
        is_new_session: True when this is the first turn for the session_id.
        model_name: Optional per-call model override (None = sub-agent default).
    """
    return await _trigger_callbacks(
        "invoke_agent",
        agent_name,
        prompt,
        session_id,
        is_new_session,
        model_name=model_name,
    )
```

### Call site in `_invoke_agent_impl`

In `code_puppy/tools/subagent_invocation.py:_invoke_agent_impl()`, after
`session_id` is finalized (around line 96, just after the
`if session_id is None / elif is_new_session` block) and **before**
`bus.emit(SubAgentInvocationMessage(...))` (line ~106), add:

```python
try:
    from code_puppy.callbacks import on_invoke_agent
    await on_invoke_agent(
        agent_name=agent_name,
        prompt=prompt,
        session_id=session_id,
        is_new_session=is_new_session,
        model_name=model_name,
    )
except Exception as e:
    import logging
    logging.getLogger(__name__).warning(
        "on_invoke_agent hook raised; continuing sub-agent invocation: %s", e
    )
```

---

## Why this is worth doing upstream

1. **Eliminates the magic-string workaround.** Today, plugins that want
   sub-agent dispatch visibility hardcode a
   `SUBAGENT_TOOLS = {"invoke_agent", "invoke_agent_with_model"}` constant
   and intercept tool calls. A first-class hook removes that pattern entirely.

2. **Gives subscribers the resolved `session_id`.** The auto-generated,
   hash-suffixed session ID (e.g. `designer-otter-session-a3f2b1`) is only
   finalized inside `_invoke_agent_impl`. The `pre_tool_call` workaround sees
   only the raw tool args, which may have `session_id=None`. Subscribers
   currently have to dig the resolved value out of the tool result after the
   fact, which is awkward and fragile.

3. **Fixes a documented-but-broken public API.** `AGENTS.md` advertises the
   hook. Plugins subscribe to it. They get nothing. That's a bug, not by
   design.

---

## Why raft-puppy isn't doing it locally

- The fork's golden rule (`AGENTS.md`): "nearly all new functionality should
  be a plugin." Core edits get tracked, accumulate rebase tax, and require
  justification.
- `CHANGES_FROM_UPSTREAM.md` documents 5 existing core edits, each with a
  cherry-pick commit title and rebase protocol. Adding a 6th for marginal
  gain over an existing workaround fails the cost/benefit test.
- The `pre_tool_call` translation in
  `code_puppy/plugins/telemetry_ndjson/register_callbacks.py` is
  non-invasive, proven in production, and emits the equivalent
  `agent_invoke_start` / `agent_invoke_complete` events.

---

## Companion: `agent_exception`

`code_puppy/callbacks.py` also defines `on_agent_exception(exception, *args,
**kwargs)` and registers an `"agent_exception"` slot. Same problem — never
triggered. The natural firing site is the `except Exception as e:` block in
`_invoke_agent_impl` (around line ~330, where
`error_msg = f"Error invoking agent '{agent_name}': {traceback.format_exc()}"`
is built). Suggested addition right after
`emit_error(error_msg, message_group=group_id)`:

```python
try:
    from code_puppy.callbacks import on_agent_exception
    await on_agent_exception(
        e,
        agent_name=agent_name,
        session_id=session_id,
        model_name=effective_model_name,
    )
except Exception:
    pass  # never let a hook failure mask the original error
```

Document the signature in `callbacks.py` to match. This is a natural pair
with the `invoke_agent` wire-up — a reviewer accepting one should accept both.

---

## Test sketch

For the upstream PR. Three regression tests covering:

1. **Plumbing test.** Register a recording callback via
   `register_callback("invoke_agent", ...)`. Call
   `await on_invoke_agent("designer", "do x", "designer-session-abc", True,
   model_name="claude")`. Assert the callback received exactly those
   args/kwargs.

2. **Call-site test.** Mock `load_agent`, `ModelFactory.get_model`, MCP
   autostart, and `temp_agent.run` (existing patterns in
   `tests/test_subagent_high_output_mode.py` and
   `tests/test_callbacks_extended.py` show how). Invoke `_invoke_agent_impl`.
   Assert a registered `invoke_agent` callback fires with the resolved
   (hash-suffixed) `session_id` **before** any `SubAgentInvocationMessage` is
   observed on the bus.

3. **Failure tolerance test.** Register a callback that
   `raise RuntimeError("boom")`. Verify `_invoke_agent_impl` still completes
   the (mocked) sub-agent run and returns a valid `AgentInvokeOutput`. The
   exception should be logged, not propagated.

For the `agent_exception` companion: register a callback, drive
`_invoke_agent_impl` into its except branch (mock `temp_agent.run` to raise),
assert the callback fires with the exception and `agent_name` / `session_id`
kwargs.

---

## Open questions for upstream maintainers

- **Nested sub-agents.** Should the hook also fire for nested sub-agents
  (sub-agent invoking sub-agent)? Currently `_invoke_agent_impl` doesn't know
  its depth; nesting is tracked via the `subagent_context` ContextVar.
  Recommended: yes, fire on every invocation, and let plugins inspect
  `get_subagent_depth()` if they care about depth.

- **Flag naming.** Is `is_new_session` the right flag name, or should it be
  `is_continuation: bool` (inverted)? Match whatever pattern the rest of the
  codebase uses for consistency.
