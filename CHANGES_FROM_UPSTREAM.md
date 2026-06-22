# Changes from upstream code-puppy

Base: `code-puppy@0.0.574` — https://github.com/mpfaffenberger/code_puppy

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
requires two small, backward-compatible extensions to upstream callback
processors.  These are the **only** logic changes to files outside the plugin
tree.  See `docs/workspace-plugin-design.md` for full context.

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

### Rebase note

When rebasing onto a new upstream release, both files above may be clobbered.
Cherry-pick the commit titled:

- `feat(skills): add exclude support to register_skills callback` (Phase D)
- `feat(agents): add exclude support to register_agents callback` (Phase C)

to restore the workspace plugin core hooks.
