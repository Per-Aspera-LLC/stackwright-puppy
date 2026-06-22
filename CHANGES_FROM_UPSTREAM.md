# Changes from upstream code-puppy

Base: `code-puppy@0.0.574` — https://github.com/mpfaffenberger/code_puppy
Current release: `stackwright-puppy@0.0.575`

**Workspace v2 is fully shipped** (phases A–H complete, all 6 surfaces working).
See `docs/workspace-plugin-design.md` for the design reference.

## Release notes

### 0.0.575 (2026-06-22)

- **Workspace v2 shipped** — full per-surface project scoping across 6 extension
  surfaces (agents, skills, plugins, MCP, hooks, file permissions). Profile-based
  config via `.code_puppy/config.json`. 304 tests passing.
- **Entry-point change** — only `raft-puppy` is installed by this fork.
  `code-puppy` and `pup` entry points have been removed to stop shadowing
  upstream's binaries. Users of this fork should use `raft-puppy` (or
  `uvx --from stackwright-puppy raft-puppy`).

## Fork identity only

The only divergence from upstream is in `pyproject.toml` — package name
(`stackwright-puppy`), description, repository URLs
(`Per-Aspera-LLC/stackwright-pro`), and the `raft-puppy` entry point alias.
No logic changes.

## Version detection cascade

`code_puppy/__init__.py` and `code_puppy/pydantic_patches.py` try
`importlib.metadata.version("stackwright-puppy")` first, then fall back to
`"code-puppy"`, then `"0.0.0-dev"`. Upstream only has the `"code-puppy"` lookup.

When rebasing onto a new upstream release, these two files may be clobbered.
Cherry-pick the commit titled
`fix: version cascade for stackwright-puppy fork identity` to restore correct
`raft-puppy --version` output.

## Workspace plugin core edits

The `project_workspace` plugin (under `code_puppy/plugins/project_workspace/`)
requires three small, backward-compatible extensions to upstream callback
processors. These are the **only** logic changes to files outside the plugin
tree. See `docs/workspace-plugin-design.md` for full context.

All three edits are documented with cherry-pick commit titles below so they can
be cleanly re-applied after an upstream rebase.

### 1. `register_agents` exclude support — `code_puppy/agents/agent_manager.py`

Added in Phase C (commit `1f8a338`).  Two effective lines in `_discover_agents()`
step 3 handle `{"name": …, "exclude": True}` entries returned by
`register_agents` callbacks.  When seen, the named agent is popped from
`_AGENT_REGISTRY` instead of added.  Without this, the `project` and `global`
agent scopes cannot suppress agents already loaded by the hardcoded
`discover_json_agents()` call.

Backward-compatible: existing callbacks return plain `{"name", "json_path"}`
dicts and are unaffected.

### 2. `register_skills` exclude support — `code_puppy/plugins/agent_skills/discovery.py`

Added in Phase D.  `_collect_plugin_skills()` now returns a
`(plugin_skills, exclusions)` tuple.  `{"name": …, "exclude": True}` entries
in any `register_skills` callback result add the skill name to `exclusions`.
`discover_skills()` applies those exclusions *before* appending plugin skills,
so an excluded name (e.g. a global skill) can be re-added by a project-scoped
callback entry.  Without this, the `project` and `global` skill scopes cannot
suppress skills already discovered by the hardcoded directory scanner.

Backward-compatible: existing callbacks that return plain skill dicts are
unaffected; the old `_collect_plugin_skills() -> List[SkillInfo]` signature
callers are updated in the same commit.

### 3. Scope-gated plugin tier loading — `code_puppy/plugins/__init__.py`

Added in Phase E (commit `9299324`, RISK-2 resolution).  In
`load_plugin_callbacks()`, before the user-tier and project-tier loading steps,
a call to `workspace_bootstrap.read_plugin_scope()` determines whether each
tier should load:

- **project** scope → user-tier plugins skipped (builtin + project only)
- **global** scope → project-tier plugins skipped (builtin + user only)
- **merge** scope → all three tiers load (unchanged default behaviour)

The import is done inline (`import code_puppy.workspace_bootstrap as _wb`)
to avoid any circular-import risk at module parse time.  `read_plugin_scope()`
is stdlib-only and always returns a safe default on error, so failure here
cannot crash startup.

Backward-compatible: without a `.code_puppy/config.json` in the tree,
`read_plugin_scope()` returns `"merge"` and all tiers load exactly as before.

The companion module `code_puppy/workspace_bootstrap.py` (new file, ~120 LOC
including docstrings) contains the pre-plugin config reader.  It is
purposefully stdlib-only and independent of all other `code_puppy.*` modules.

## Own files — no rebase conflicts

These files are entirely new in the fork and will never conflict on upstream
rebase:

- `code_puppy/workspace.py` — workspace root discovery (`discover_root()`)
- `code_puppy/workspace_bootstrap.py` — stdlib-only pre-plugin scope reader
- `code_puppy/plugins/project_workspace/` — the entire plugin tree (6 surfaces)
- `docs/workspace-plugin-design.md` — 954-line design reference
- `tests/test_workspace.py`, `tests/test_workspace_bootstrap.py` — unit tests
- `tests/plugins/project_workspace/` — surface + integration tests

## Rebase protocol — 5 commit-classes to preserve

When rebasing onto a new upstream release, cherry-pick these commits **in
order** after the rebase to restore all fork-local changes:

1. **Fork identity** — `pyproject.toml` (package name, URLs, entry point)
2. **Version cascade** — `code_puppy/__init__.py` + `code_puppy/pydantic_patches.py`
   (title: `fix: version cascade for stackwright-puppy fork identity`)
3. **Agents exclude** — `code_puppy/agents/agent_manager.py`, +4 LOC
   (title: `feat(agents): add exclude support to register_agents callback`)
4. **Skills exclude** — `code_puppy/plugins/agent_skills/discovery.py`, +9 LOC
   (title: `feat(skills): add exclude support to register_skills callback`)
5. **Plugin scope** — `code_puppy/plugins/__init__.py` + `workspace_bootstrap.py`, ~20 LOC
   (title: `feat(plugins): pre-load workspace config read for scope-aware plugin loading`)

Commits for classes 3–5 are small, well-contained, and all three can typically
be cherry-picked without conflict because they touch distinct sections of their
respective files.

The own-files tree (classes 3–5 companion files + the plugin tree) cherry-picks
automatically without conflicts since upstream does not contain any of those
paths.
