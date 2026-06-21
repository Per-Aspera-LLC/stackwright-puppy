# Project Workspace Plugin v2 — Design

> **Status**: Design-only (Phase A). No implementation code committed yet.
> **Author**: code-puppy-428ad7 (Phase A recon session, 2026-06-21)
> **Beads design issue**: `code_puppy-9nw` (now closed)
> **Upstream base**: `code-puppy@0.0.574` (commit `ce88700`)

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Discovery](#discovery)
4. [Configuration](#configuration)
5. [Resolved Design Questions](#resolved-design-questions)
6. [Surface Integration Plan](#surface-integration-plan)
7. [Lifecycle Diagram](#lifecycle-diagram)
8. [File Layout](#file-layout)
9. [Implementation Phase Order](#implementation-phase-order)
10. [Risks Identified During Recon](#risks-identified-during-recon)

---

## Overview

### Problem

PR #355 ("project workspace with `projectOnly` isolation") was abandoned after the upstream v0.0.545 → v0.0.574 rebase. It carried 6 cherry-picks that touched `config.py`, `mcp_/manager.py`, and the agent/plugin loaders directly — creating perpetual rebase conflict zones. The design was also too coarse: a single `projectOnly: true/false` boolean could not express combos like "local file permissions + global skills" that real workflows need.

### Goals

1. **Zero rebase conflicts** — plugin lives in `code_puppy/plugins/project_workspace/`, a tree upstream never touches.
2. **Profile-based scoping** — 5 named profiles (plus custom `overrides`) covering 6 extension surfaces independently.
3. **Plugin-first** — use existing `callbacks.py` hooks wherever possible; add minimal core helpers only where needed.
4. **Honest limitations** — where true isolation requires upstream core changes, say so explicitly and note the hooks needed.

### What We're Replacing (vs PR #355)

| PR #355 | v2 |
|---|---|
| Single `projectOnly: true/false` flag | 5 profiles + per-surface overrides |
| Direct edits to core files (`config.py`, `mcp_/manager.py`, agent loader, plugin loader) | Pure plugin, zero core file edits |
| Binary: all surfaces on/off together | Per-surface control (e.g. local file perms + global skills) |
| 6 cherry-picks causing rebase conflicts | 1 clean builtin plugin directory |

---

## Architecture

### Hybrid Plugin Model

The workspace plugin uses the **builtin plugin tier** (`code_puppy/plugins/project_workspace/`). It registers callbacks via `callbacks.py` at import time (during `load_plugin_callbacks()`), runs discovery at module init, and provides isolated surfaces for each resource type.

```
code_puppy/callbacks.py           ← callback bus (unchanged)
code_puppy/plugins/__init__.py    ← plugin loader (unchanged)
code_puppy/workspace.py           ← NEW: tiny discover_root() helper (≤50 LOC)
code_puppy/plugins/project_workspace/
    register_callbacks.py         ← module-level: discover + register all hooks
    _config.py                    ← profile + override loading, schema validation
    _discovery.py                 ← wraps code_puppy.workspace.discover_root
    surfaces/
        agents.py                 ← register_agents callback
        skills.py                 ← register_skills callback
        plugins.py                ← documents limitation; no callback possible
        mcp.py                    ← startup callback: inject project MCP servers
        hooks.py                  ← startup callback: load .code_puppy/hooks.json
        file_permissions.py       ← file_permission callback
```

### Critical Recon Findings

**`hook_engine/` is NOT the same as `callbacks.py`.**

- `callbacks.py` — Python-to-Python plugin bus (register Python functions as hooks)
- `hook_engine/` — subprocess-based hooks à la Claude Code's `.claude/settings.json`; processes shell scripts/commands on events (PreToolUse, PostToolUse, SessionStart, etc.)
- The builtin `claude_code_hooks` plugin **bridges** them: it registers Python callbacks that invoke the hook engine subprocess runner

This distinction is critical for the **Hooks surface** — see [Resolved Design Questions → Q1](#q1-hooks-vs-plugins).

**Plugin load order is: builtin → user → project.**

Our `project_workspace` plugin is a **builtin** — it loads FIRST, before user and project plugins. All module-level init in `register_callbacks.py` runs before any user or project plugin callbacks are registered. This means workspace discovery and config loading are complete before any other plugin sees a hook fire.

**Agent, skill, MCP discovery is lazy.**

`on_register_agents()`, `on_register_skills()`, and MCP autostart all fire AFTER `on_startup()` — they're invoked lazily when first needed. Our module-level init is complete well before any of these fire. No timing risk.

---

## Discovery

### Walk-Up Algorithm

`code_puppy.workspace.discover_root(cwd: Path | None = None) -> Path | None`

1. Start from `cwd` (defaults to `Path.cwd()`)
2. Walk up directory tree, testing each directory for `.code_puppy/`
3. Stop at `.git/` boundary — never cross the repo root
4. Return the **first ancestor** (including `cwd`) that has a `.code_puppy/` directory, or `None`

```
/home/user/projects/myrepo/           ← .git/ found here (boundary)
    .code_puppy/                      ← FOUND — return this parent
    src/
        frontend/
            components/               ← cwd here → walks up → finds .code_puppy
```

If `.git/` is found BEFORE `.code_puppy/`, return `None` (no workspace). If no `.git/` boundary is found, walk up to the filesystem root (defensive — `.git/` should always be present in normal usage).

### Module-Level Init

`register_callbacks.py` executes discovery at **import time** (during `load_plugin_callbacks()`):

```python
# Top of register_callbacks.py — runs before any hook fires
from code_puppy.plugins.project_workspace._discovery import WorkspaceState
_WS = WorkspaceState.init()   # discover_root() + load config — fast, pure
```

`WorkspaceState` holds:
- `root: Path | None` — discovered project root (parent of `.code_puppy/`)
- `config_path: Path | None` — path to `.code_puppy/config.json` (may not exist)
- `profile: str` — resolved profile name (after defaults + overrides)
- `surfaces: dict[str, str]` — resolved per-surface scope (`project`/`merge`/`global`)

---

## Configuration

### Schema

`.code_puppy/config.json`:

```json
{
  "$schema": "https://stackwright.dev/schemas/workspace-config/v2.json",
  "profile": "strict-local",
  "overrides": {
    "skills": "global",
    "mcp": "merge"
  }
}
```

All fields are optional. An empty `{}` (or missing file) applies the `merge` profile.

**JSON Schema (informal — formal schema in Phase B)**:

```
{
  profile:   string  (one of: "merge" | "strict-local" | "local-with-global-skills"
                               | "local-mcp-only" | "custom")
             default: "merge"

  overrides: {
    agents:           "project" | "merge" | "global"  (default: per profile)
    skills:           "project" | "merge" | "global"
    plugins:          "project" | "merge" | "global"
    mcp:              "project" | "merge" | "global"
    hooks:            "project" | "merge" | "global"
    file_permissions: "project" | "merge" | "global"
  }
}
```

### Profiles

| Profile | Agents | Skills | Plugins | MCP | Hooks | File-perms |
|---|---|---|---|---|---|---|
| `merge` *(default)* | merge | merge | merge | merge | merge | merge |
| `strict-local` | project | project | project | project | project | project |
| `local-with-global-skills` | merge | global | project | project | project | project |
| `local-mcp-only` | merge | merge | merge | project | merge | merge |
| `custom` | *(must specify all via overrides)* | | | | | |

> **Note**: The `default` profile (no `.code_puppy/` directory at all) is identical to `merge`. The distinction is only relevant if we later want "presence of `.code_puppy/` implies stricter defaults" — decided against this (see Q5).

### Per-Surface Scope Semantics

Each surface accepts one of three values:

- **`merge`**: Union of global (`~/.code_puppy/`) + project (`.code_puppy/`) resources. Project wins on name collision.
- **`project`**: Only project-local resources. If no `.code_puppy/` dir exists, fall back to `merge` and emit a warning.
- **`global`**: Only global resources. Project-local resources are NOT injected by our plugin (see limitations in Surface sections).

### Overrides

`overrides` is applied AFTER the profile resolves. It lets you start from a named profile and tweak individual surfaces:

```json
{
  "profile": "strict-local",
  "overrides": {
    "skills": "global"
  }
}
```

This means "strict-local for everything except skills, which use global". Resolution order:
1. Derive per-surface values from `profile`
2. Apply each key in `overrides` on top

---

## Resolved Design Questions

### Q1: Is "hooks/callbacks" a separate surface from "plugins"?

**Answer: Yes — they are genuinely different systems. Keep as 5+1 surfaces, not collapsed.**

**Rationale from recon**:

`callbacks.py` and `hook_engine/` are completely independent:
- **`callbacks.py`** — Python-to-Python bus. Plugins register Python functions (e.g. `register_callback("register_agents", fn)`). This is what `plugins/` loads.
- **`hook_engine/`** — subprocess-based hook runner (`.claude/settings.json` format). Configs are JSON files specifying shell commands to run on events (PreToolUse, etc.). The `claude_code_hooks` builtin plugin bridges them into the `callbacks.py` system.

**For our plugin:**
- **Plugins surface**: "Which plugin tiers' Python callbacks are active?"
  - `merge`: builtin + user + project (current default — already implemented)
  - `project`: builtin + project only (user tier suppressed — see limitation below)
  - `global`: builtin + user only (project additions ignored)
  - **[LIMITATION]** True suppression of user-tier plugins requires a core filter hook. The existing `plugins/__init__.py` loader has no "skip user tier" control point from outside.
  
- **Hooks surface**: "Which hook_engine configs are active?"
  - `merge`: both `~/.code_puppy/hooks.json` AND `.code_puppy/hooks.json` (what `claude_code_hooks` already does with `~/.code_puppy/hooks.json` + `.claude/settings.json`)
  - `project`: only `.code_puppy/hooks.json` (our plugin loads it; tells `claude_code_hooks` to skip global — via env var or config flag)
  - `global`: only `~/.code_puppy/hooks.json` (our plugin skips project hooks)
  - **[ACHIEVABLE]** Our plugin loads `.code_puppy/hooks.json` via its own `HookEngine` instance at `startup`. We don't need to modify `claude_code_hooks` for additive behavior.

**Implementation approach for hooks surface**: Our plugin's `hooks.py` surface creates an independent `HookEngine` and registers its own `pre_tool_call`, `post_tool_call`, etc. callbacks that fire after (or instead of) the `claude_code_hooks` ones. For `project` scope with suppression of global hooks, a config flag approach is simplest: set an env var `CODE_PUPPY_SKIP_GLOBAL_HOOKS=1` before `claude_code_hooks` reads its config. This requires `claude_code_hooks` to check the var (a 2-line core change — well within budget).

---

### Q2: File Permission Semantics for "project" Scope

**Answer: Option (b) — auto-restrict to project root — as the default. Option (a) policy file as optional extension.**

**Rationale from recon**:

The `file_permission` callback signature is:
```python
def handler(
    context: Any,
    file_path: str,
    operation: str,
    preview: str | None = None,
    message_group: str | None = None,
    operation_data: Any = None,
) -> bool:
```

Return `True` = allow, `False` = deny. The callback is registered in addition to (not replacing) `file_permission_handler`, which handles the interactive "do you approve?" UI. File op tools call `on_file_permission()` and check if ANY handler returns `False` to deny.

**Our plugin's behavior for `project` scope**:

1. **Auto-restrict (b)**: Register a `file_permission` callback that returns `False` if `os.path.abspath(file_path)` is NOT under the project root. No config file needed.

2. **Policy file (a) — optional**: If `.code_puppy/file_policy.json` exists, additional allow/deny rules apply on top of the auto-restriction:

```json
{
  "allow": ["/tmp/my-project-build/**"],
  "deny": ["**/secrets/**", "~/.ssh/**"]
}
```

Pattern matching: use `fnmatch` or `pathlib` glob semantics. `allow` rules override auto-restriction (can grant access outside project root). `deny` rules can further restrict within the project root.

**For `merge` scope**: No auto-restriction; policy file (if present) still applies as additional rules.

**For `global` scope**: Neither auto-restriction nor policy file; global file permission behavior only.

---

### Q3: `merge` / `project` / `global` Data-Level Semantics

**Answer: Project wins on collision. Fall-through behavior on missing dirs. No errors.**

**Detailed semantics per surface:**

#### Agents
- `merge`: User agents (`~/.code_puppy/agents/*.json`) + project agents (`.code_puppy/agents/*.json`). Project wins on name collision. Same as `discover_json_agents()` — already the default behavior.
- `project`: Plugin injects ONLY agents from `.code_puppy/agents/`. If no `.code_puppy/agents/` dir exists, falls through to `merge` + emits warning.  User agents from `discover_json_agents()` are still present in the registry (limitation — see Risks).
- `global`: Plugin injects nothing (skips). User agents from `discover_json_agents()` are the only source.  Project agents in `.code_puppy/agents/` are still discovered by `discover_json_agents()` (limitation — same mechanism).

#### Skills
- Identical semantics to agents. `merge` = both dirs; `project` = project dir only (limitation: user skills still in scan path); `global` = skip injection.

#### MCP
- `merge`: Plugin injects project MCP servers from `.code_puppy/mcp_servers.json` into the MCPManager registry. Global MCP servers (from `~/.config/code_puppy/mcp_servers.json`) remain. Both coexist.
- `project`: Same as `merge` for injection; ideally global servers are disabled.  Disabling global MCP servers requires registry manipulation (calling `registry.update()` to set `enabled=False` on global servers). This IS achievable from a plugin at `startup` time via `get_mcp_manager()`. No core change needed.
- `global`: Plugin skips injection. Global MCPs only.

#### Plugins
- `merge`: All tiers active (current default — our plugin does nothing).
- `project`:  True suppression of user-tier plugins not achievable from plugin. Document as limitation. The `plugins/__init__.py` loads all tiers unconditionally at import time, before our plugin runs. Proposed core addition: a `_SKIP_USER_PLUGINS` env var checked at load time.
- `global`: Plugin does nothing (same as `merge` — can't suppress project plugins either since they load alongside our plugin).

#### Hooks (hook_engine)
- `merge`: Project `.code_puppy/hooks.json` is loaded additively alongside global hooks.
- `project`: Only `.code_puppy/hooks.json` is used. Global hooks skipped. Achievable via env var flag.
- `global`: Plugin skips loading project hooks. Global hooks only (handled by `claude_code_hooks`).

#### File Permissions
- `project`: Auto-restrict to project root + optional policy file. 
- `merge`: Only policy file (no auto-restriction).
- `global`: No project-level restrictions. Global file permission behavior only.

#### Collision Rules (for `merge` mode)
- **Name collision** (same agent/skill name in both global and project dirs): **project wins**. Consistent with existing `discover_json_agents()` behavior.
- **MCP server collision** (same server name): **project wins** — the project's config is registered/updated in the MCPManager registry.
- **Silent wins, not errors**: Collisions are resolved silently at debug log level. No errors thrown.

#### Missing Project Dir Fallback
When scope is `project` and `.code_puppy/` (or the specific sub-dir) doesn't exist:
- Fall through to `merge` behavior
- Emit a warning: `"[project_workspace] scope='project' requested but .code_puppy/ not found — using merge"`
- Do NOT error out or halt

---

### Q4: Discovery Timing in the Startup Lifecycle

**Answer: No new core hook needed. Module-level init at `load_plugin_callbacks()` time is sufficient.**

**Rationale from recon**:

`load_plugin_callbacks()` is called at module import time in `cli_runner.py` line 52. This is **before** `main()` runs. The full boot sequence:

```
cli_runner.py imported
  └─ load_plugin_callbacks()              # line 52
       ├─ _load_builtin_plugins()
       │    └─ import project_workspace.register_callbacks  ← OUR PLUGIN RUNS HERE
       │         ├─ discover_root()       ← workspace root captured
       │         ├─ load_config()        ← profile + overrides loaded
       │         └─ register_callback(…) ← all surface callbacks registered
       ├─ _load_user_plugins()
       └─ _load_project_plugins()

main() runs
  ├─ argument parsing / setup
  ├─ on_startup()                        # our startup callback fires if needed
  │                                      # (optional: logging, warnings)
  └─ interactive REPL or single prompt
       ├─ on_register_agents()           ← lazy, uses _WS state 
       ├─ on_register_skills()           ← lazy, uses _WS state 
       ├─ pre_mcp_autostart()            ← lazy, uses _WS state 
       └─ file_permission()              ← per-file-op, uses _WS state 
```

The workspace root and config are captured **before any hook callback fires**. All surface callbacks operate on pre-loaded `_WS` state.

**`startup` callback**: We register one, but only for optional logging (e.g. "workspace detected at /path/to/project"). No critical work happens there.

---

### Q5: Empty `.code_puppy/` Directory Fallback

**Answer: Option B — implicit `merge` profile. Presence of directory does NOT imply isolation.**

**Rationale**:

If `.code_puppy/` exists but `config.json` is missing or empty:
- Apply `merge` profile (the same as if no `.code_puppy/` existed at all)
- Emit a debug-level log: `"[project_workspace] .code_puppy/ found but config.json missing — using profile=merge"`
- Do NOT error, do NOT warn loudly (this is expected when teams add `.code_puppy/agents/` without configuring a profile)

**Why not Option A (implicit `strict-local`)?** Too surprising. A team might add `.code_puppy/agents/` just to have project-local agents, without wanting any other isolation. Silently applying strict-local would break their global agents, MCP, skills.

**Why not Option C (error)?** A project plugin directory without a config is valid and common. Errors would break CI and non-configured projects.

The user opts in to isolation by writing `config.json`. Silence means merge.

---

## Surface Integration Plan

### 1. Agents Surface (`code_puppy-ve9`)

**Callback used**: `register_agents`

**Plugin behavior per scope**:

- `merge`: Inject agents from `.code_puppy/agents/*.json` ONLY. `discover_json_agents()` already handles the user dir AND will also pick up `.code_puppy/agents/` independently. Our callback therefore ONLY contributes if the scope wants additive project agents. Wait — actually `discover_json_agents()` ALREADY scans both `~/.code_puppy/agents/` and `.code_puppy/agents/`. So for `merge`, our `register_agents` callback returns `[]` (noop). The default behavior already handles merge correctly.

- `project`: Our callback returns agents from `.code_puppy/agents/`. But `discover_json_agents()` (step 2 in `_discover_agents()`) runs unconditionally before our callback and will have ALREADY loaded the user dir agents.  **Limitation**: We cannot suppress user agents from `discover_json_agents()` via a plugin. The plugin-registered agents (step 3) override by name, but user-dir agents with different names are still present.

- `global`: Our callback returns `[]`. We want to suppress `.code_puppy/agents/` from `discover_json_agents()`.  **Same limitation**: can't control `discover_json_agents()` from a plugin.

**Collision rules**: For names where BOTH discover_json_agents (step 2) and our callback (step 3) return a result: **step 3 wins** (plugin agents override JSON agents of the same name). Within our callback, project agents override user agents.

**Edge cases**:
- No `.code_puppy/agents/` dir: our callback returns `[]`
- Malformed JSON files: skip with warning

**Upstream code touched**: None. `code_puppy/agents/agent_manager.py` and `code_puppy/agents/json_agent.py` unchanged.

**Limitation requiring minimal core change**: To achieve true `project`-only agent scope (suppressing user agents from `discover_json_agents()`), we need one of:
1. A `filter_discovered_agents(agents_dict) -> agents_dict` hook in `agent_manager.py`'s `_discover_agents()` at step 2, OR
2. A `get_agents_directories() -> list[Path]` hook that overrides which dirs `discover_json_agents()` scans (~20 LOC in `json_agent.py`)

This is flagged as a Phase C/D consideration. For the initial implementation, `project` scope for agents means "project agents have priority" but user agents are still present.

---

### 2. Skills Surface (`code_puppy-avm`)

**Callback used**: `register_skills`

**Plugin behavior per scope**:

- `merge`: Return skills from `.code_puppy/skills/*/SKILL.md`. `get_skill_directories()` already includes `.code_puppy/skills/` and `~/.code_puppy/skills/` — so the `agent_skills` plugin handles merge natively. Our `register_skills` callback returns `[]` for merge (noop).

- `project`: Return skills from `.code_puppy/skills/*/SKILL.md` via `register_skills`. The directory scanner in `agent_skills/discovery.py` will still also scan `~/.code_puppy/skills/`.  **Same limitation as agents**: can't suppress user skills from the directory scanner.

- `global`: Return `[]`. Cannot suppress project skills from the directory scanner.

**Collision rules**: `register_skills` callback results are deduplicated by `_collect_plugin_skills()` (same-name skills are warned and skipped). For skill files discovered by the directory scanner (not via callback), deduplication by `discover_skills()` takes the LAST-found version (scan order: user dir → project dir). Project WINS.

**Edge cases**:
- `.code_puppy/skills/` dir with no subdirs: returns `[]`, no error
- `SKILL.md` missing frontmatter `name:` field: skip with warning

**Upstream code touched**: None.

**Limitation requiring minimal core change**: A `filter_skill_directories(dirs) -> list[Path]` hook in `agent_skills/discovery.py` (~15 LOC). Low risk addition.

---

### 3. Plugins Surface (`code_puppy-rv8`)

**Callback used**: None achievable from plugin. Documentation surface.

**Plugin behavior per scope**:

The plugin loader (`plugins/__init__.py`) runs `builtin → user → project` at `load_plugin_callbacks()` time, BEFORE any `register_callbacks.py` (including ours) has a chance to run. The loading is one-shot (`_PLUGINS_LOADED = True` guard).

- `merge`: Current default — all tiers loaded. Our plugin does nothing.
- `project`:  **Not achievable from a plugin**. User-tier plugins have already loaded.
- `global`:  **Not achievable from a plugin**. Same issue.

**Proposed minimal core change for `project`/`global` scope**:

Check env var in `plugins/__init__.py` before loading user-tier plugins (2-4 LOC):

```python
# In load_plugin_callbacks(), before _load_user_plugins():
if os.environ.get("CODE_PUPPY_SKIP_USER_PLUGINS") == "1":
    user_loaded = []
else:
    user_loaded = _load_user_plugins(...)
```

Our plugin would set this env var in the PARENT PROCESS before starting — or propagate it via `.code_puppy/config.json` read at very early boot (before `load_plugin_callbacks()`). This requires a pre-plugin config reader in `__main__.py` (5-10 LOC). Total change: ~15 LOC across 2 files.

**For Phase E**: Implement the env-var approach and the pre-plugin config reader. Flag this as a "minimal core hook" from our budget.

**Upstream code touched** (when Phase E is implemented):
- `code_puppy/plugins/__init__.py`: 2-4 LOC addition
- `code_puppy/__main__.py` or `code_puppy/main.py`: 5-10 LOC early config read

---

### 4. MCP Surface (`code_puppy-z1q`)

**Callback used**: `startup` callback → direct `get_mcp_manager().registry` manipulation

**Plugin behavior per scope**:

The MCP loading pipeline:
1. `MCPManager.sync_from_config_file()` loads `~/.config/code_puppy/mcp_servers.json` (global)
2. `pre_mcp_autostart(agent_name, server_names)` fires before servers start (notification only — can't filter)
3. Agent bindings control which servers autostart for which agent

**Our approach** — at `startup` callback:

- `merge`: Load `.code_puppy/mcp_servers.json` and inject into MCPManager registry via `get_mcp_manager().registry.register(ServerConfig(...))`. Global servers remain. Both coexist. Project servers added to the registry with their agent bindings intact.

- `project`: Load project MCP servers AND disable global servers:
  ```python
  manager = get_mcp_manager()
  for server in manager.registry.list_all():
      if server.name not in project_server_names:
          manager.registry.update(server.id, dataclasses.replace(server, enabled=False))
  ```
  Then register project servers. This IS achievable from a plugin — `get_mcp_manager()` returns the singleton and `registry.update()` is a public method.

- `global`: Skip injection. Global MCP servers remain as-is.

**`.code_puppy/mcp_servers.json` format** — mirrors the global `mcp_servers.json`:
```json
{
  "mcp_servers": {
    "my-project-postgres": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres"],
      "env": {"POSTGRES_URL": "postgresql://localhost/mydb"},
      "enabled": true,
      "autostart": true,
      "agents": ["code-puppy"]
    }
  }
}
```

**Edge cases**:
- No `.code_puppy/mcp_servers.json`: our plugin is a noop for MCP
- MCPManager not yet initialized at `startup` time: `get_mcp_manager()` initializes lazily, this is fine
- `project` scope + global server with same name: our `register()` call either updates or skips (registry handles dedup)

**Upstream code touched**: None. MCPManager's public API is sufficient.

---

### 5. Hooks Surface (`code_puppy-wrw`)

**Callback used**: `startup` callback → create `HookEngine` instance → register `pre_tool_call`, `post_tool_call`, `session_end` etc. callbacks

**Plugin behavior per scope**:

The `claude_code_hooks` builtin plugin ALREADY loads:
1. `~/.code_puppy/hooks.json` (global hooks)
2. `.claude/settings.json` (project-level hooks in Claude Code format)

Our plugin adds a THIRD source: `.code_puppy/hooks.json` (project hooks in Claude Code-compatible format).

- `merge`: Load `.code_puppy/hooks.json` if it exists. Create a separate `HookEngine` instance. Register its callbacks. This runs ADDITIVELY alongside `claude_code_hooks`. Both hook engines fire.

- `project`: Load ONLY `.code_puppy/hooks.json`. To suppress global hooks (`claude_code_hooks`), set env var `CODE_PUPPY_SKIP_GLOBAL_HOOKS=1` before the plugin loads (or check a config flag in `claude_code_hooks/config.py`). Requires 2-line change in `claude_code_hooks`.

- `global`: Skip loading `.code_puppy/hooks.json`. Leave `claude_code_hooks` alone. Noop.

**`.code_puppy/hooks.json` format** — same as `~/.code_puppy/hooks.json`:
```json
{
  "PreToolUse": [{
    "matcher": "agent_run_shell_command",
    "hooks": [{
      "type": "command",
      "command": "bash .code_puppy/hooks/pre-shell-check.sh",
      "timeout": 5000
    }]
  }]
}
```

This reuses the `hook_engine` system directly — no new format to design.

**Upstream code touched** (for `project` scope): `claude_code_hooks/config.py`: 2 LOC to check `CODE_PUPPY_SKIP_GLOBAL_HOOKS` env var. Total: trivial.

---

### 6. File Permissions Surface (`code_puppy-9w1`)

**Callback used**: `file_permission`

**Plugin behavior per scope**:

The `file_permission_handler` builtin plugin handles interactive "do you approve?" prompts. Our callback runs BEFORE it (or alongside it — all `file_permission` callbacks are iterated; if ANY returns `False`, the op is denied).

**Wait — actually callbacks are triggered sequentially and the caller checks `any(not result...)`:**

```python
# file_modifications.py
permission_results = on_file_permission(context, path, "write", ...)
if permission_results and any(not result for result in permission_results if result is not None):
    return _create_rejection_response(path)
```

Our callback returns `False` to block silently (no interactive prompt). The `file_permission_handler`'s prompt then never fires for denied paths. 

- `project` scope callback:
  ```python
  def _check_project_scope(context, file_path, operation, *args, **kwargs) -> bool:
      abs_path = Path(os.path.abspath(file_path))
      if not abs_path.is_relative_to(_WS.root):
          # Silently deny — too outside project root
          return False
      if _WS.policy:
          return _WS.policy.check(abs_path)  # apply .code_puppy/file_policy.json
      return True  # inside project root, no policy restrictions
  ```

- `merge` scope callback: Only applies policy file rules (if `.code_puppy/file_policy.json` exists). No auto-restriction.

- `global` scope: Do NOT register a `file_permission` callback. Global file permission behavior only.

**`.code_puppy/file_policy.json` format**:
```json
{
  "allow": [
    "/tmp/project-build/**",
    "~/.project-cache/**"
  ],
  "deny": [
    "**/secrets/**",
    "**/.env"
  ]
}
```

Resolution order: auto-restrict (project root check) → allow rules (can grant outside root) → deny rules (can restrict inside root). Explicit `allow` beats auto-restrict; explicit `deny` beats `allow`.

**Edge cases**:
- `file_path` is a relative path: `os.path.abspath()` resolves against CWD
- Symlinks: resolve via `Path.resolve()` before comparing
- `.code_puppy/` files themselves: ALWAYS allowed (the user is explicitly configuring workspace)
- `_WS.root` is `None` (no workspace detected): callback not registered, no restriction

**Upstream code touched**: None. This is purely additive via `register_callback("file_permission", ...)`.

---

## Lifecycle Diagram

```
PROCESS START
│
├─ [import cli_runner.py]
│   │
│   └─ load_plugin_callbacks()          ← PLUGIN LOADING PHASE
│       │
│       ├─ _load_builtin_plugins()
│       │   ├─ …other builtins…
│       │   ├─ claude_code_hooks        ← loads ~/.code_puppy/hooks.json +
│       │   │                              .claude/settings.json
│       │   ├─ file_permission_handler  ← registers interactive permission prompt
│       │   └─ project_workspace        ← OUR PLUGIN
│       │       ├─ discover_root()      ← CWD → walk up → find .code_puppy/
│       │       ├─ load_config()        ← read .code_puppy/config.json → resolve profile
│       │       └─ register callbacks   ← register all surface callbacks
│       │
│       ├─ _load_user_plugins()         ← ~/.code_puppy/plugins/*
│       └─ _load_project_plugins()      ← .code_puppy/plugins/*
│
├─ main() begins
│   ├─ arg parsing, agent init, model config
│   │
│   └─ on_startup()                     ← STARTUP PHASE
│       ├─ project_workspace._startup() ← optional: log "workspace at /path"
│       │                                  + inject project MCP servers
│       │                                  + load .code_puppy/hooks.json (merge scope)
│       └─ …other startup callbacks…
│
└─ INTERACTIVE LOOP (or single prompt)
    │
    ├─ on_register_agents()             ← LAZY: first agent menu or agent switch
    │   └─ project_workspace.agents()  ← return project agents per scope
    │
    ├─ on_register_skills()             ← LAZY: first skill discovery
    │   └─ project_workspace.skills()  ← return project skills per scope
    │
    ├─ pre_mcp_autostart(agent, names)  ← before each agent's MCP servers start
    │   └─ (no workspace action needed — MCP already injected at startup)
    │
    └─ on_file_permission(ctx, path, op)← PER FILE OP
        └─ project_workspace.file_perm()← project scope check → allow/deny
```

---

## File Layout

```
code_puppy/
├── workspace.py                          # NEW — ≤50 LOC discover_root() helper
│
└── plugins/
    └── project_workspace/
        ├── __init__.py                   # empty (package marker)
        ├── register_callbacks.py         # entry point
        │                                 #   module-level: _WS = WorkspaceState.init()
        │                                 #   register_callback() calls for all surfaces
        │                                 #   startup callback for MCP + hooks injection
        ├── _config.py                    # WorkspaceConfig: profile loading, override
        │                                 #   merging, schema validation
        ├── _discovery.py                 # WorkspaceState: wraps workspace.discover_root()
        │                                 #   + loads config + resolves surfaces dict
        ├── surfaces/
        │   ├── __init__.py
        │   ├── agents.py                 # register_agents callback impl
        │   ├── skills.py                 # register_skills callback impl
        │   ├── plugins.py                # DOCUMENTATION ONLY — no callback;
        │   │                             #   explains limitation + proposed core hook
        │   ├── mcp.py                    # startup callback: load .code_puppy/mcp_servers.json
        │   │                             #   + inject/disable via get_mcp_manager()
        │   ├── hooks.py                  # startup callback: load .code_puppy/hooks.json
        │   │                             #   via HookEngine; register pre_tool_call etc.
        │   └── file_permissions.py       # file_permission callback impl
        │                                 #   + file_policy.json loader
        └── tests/
            ├── __init__.py
            ├── test_config.py            # profile parsing, override merging
            ├── test_discovery.py         # discover_root() walk-up, .git boundary
            └── surfaces/
                ├── __init__.py
                ├── test_agents.py
                ├── test_skills.py
                ├── test_mcp.py
                ├── test_hooks.py
                └── test_file_permissions.py
```

**Note on `plugins.py`**: Even though there's no callback to register, this file exists to document the limitation and the proposed core change for full plugin-tier scoping. It's a "design stub" that becomes real code in a later phase if Charles decides to add the env-var core hook.

**Note on `_config.py` and `_discovery.py` naming**: Leading underscore = package-private. These are not public APIs — `register_callbacks.py` is the only intended entry point. Prevents accidental external import of internal helpers.

---

## Implementation Phase Order

Matches existing beads tickets. Phases build on each other but each is independently shippable.

### Phase B — Foundation (`code_puppy-39m`)
*Beads ticket*: `code_puppy-39m` — discovery helper

Deliverables:
- `code_puppy/workspace.py` — `discover_root(cwd)` function, ≤50 LOC
- `code_puppy/plugins/project_workspace/` skeleton:
  - `__init__.py`, `register_callbacks.py` (startup only, logs workspace location)
  - `_config.py` — profile loading, override merging, schema validation
  - `_discovery.py` — WorkspaceState class
  - `tests/` — config parsing + discover_root() tests
- Zero surface callbacks yet — just foundation wiring

**Why first**: Everything else depends on `discover_root()` and `WorkspaceState`. Easy to unit test in isolation.

---

### Phase C — Agents Surface (`code_puppy-ve9`)
*Beads ticket*: `code_puppy-ve9` — agents scope

Deliverables:
- `surfaces/agents.py` — `register_agents` callback
- Integration: scan `.code_puppy/agents/*.json` → return as JSONAgent `json_path` entries
- Tests with fixture `.code_puppy/agents/` dir
- Document `project`-scope limitation in surface file

**Why second**: Purely additive (JSON data), no side effects, simplest callback to implement and test. Low blast radius.

---

### Phase D — Skills Surface (`code_puppy-avm`)
*Beads ticket*: `code_puppy-avm` — skills scope

Deliverables:
- `surfaces/skills.py` — `register_skills` callback
- Integration: return `skill_md_path` entries for each `SKILL.md` under `.code_puppy/skills/`
- Tests with fixture skill directories

**Why third**: Same shape as agents. Can reuse test harness from Phase C. Also additive.

---

### Phase E — Plugins Surface (`code_puppy-rv8`)
*Beads ticket*: `code_puppy-rv8` — plugins scope

Deliverables:
- `surfaces/plugins.py` — documentation stub
- Optional: propose the env-var core hook to Charles
- If approved: `code_puppy/plugins/__init__.py` 2-4 LOC addition + pre-plugin config reader

**Why fourth**: Lowest value (project plugins already handled by existing loader), and the core change is optional. If Charles says "no core changes," this phase ships as docs only.

---

### Phase F — Hooks Surface (`code_puppy-wrw`)
*Beads ticket*: `code_puppy-wrw` — hooks/callbacks scope

Deliverables:
- `surfaces/hooks.py` — `startup` callback: load `.code_puppy/hooks.json`
- Reuse `HookEngine` from `code_puppy/hook_engine/`
- Register `pre_tool_call`, `post_tool_call`, `session_end`, `notification`, `user_prompt_submit`, `pre_compact` callbacks that delegate to our HookEngine instance
- Tests: mock HookEngine, verify correct events dispatched
- Optional: env-var for global hooks suppression (2-line change in `claude_code_hooks/config.py`)

**Why fifth**: Independent of agents/skills. Medium complexity (involves HookEngine internals). Could ship as `merge`-only first (additive hooks) then add `project`-scope suppression.

---

### Phase G — MCP Surface (`code_puppy-z1q`)
*Beads ticket*: `code_puppy-z1q` — MCP scope

Deliverables:
- `surfaces/mcp.py` — `startup` callback: load `.code_puppy/mcp_servers.json` → inject into MCPManager
- Handle `project` scope: disable global servers in registry
- Parse MCPServer configs (same format as global `mcp_servers.json`)
- Tests: mock MCPManager registry, verify inject/disable behavior

**Why sixth**: Stateful (touches singleton MCPManager), has network implications (MCP servers are network resources). Most important to get right. Ship `merge` scope first (additive only), then `project` scope (with disable logic) in a follow-up commit.

---

### Phase H — File Permissions Surface (`code_puppy-9w1`)
*Beads ticket*: `code_puppy-9w1` — file permissions scope

Deliverables:
- `surfaces/file_permissions.py` — `file_permission` callback
- Project root path check (`is_relative_to()`)
- Optional: `file_policy.json` parser with allow/deny glob matching
- Symlink resolution
- Tests: mock file paths, verify allow/deny behavior in all edge cases

**Why last**: Highest blast radius (fires on EVERY file op). Must be bulletproof. Premature allow/deny errors could prevent the plugin from operating on its own files. Only ship after the test suite is comprehensive.

---

### Phase I — Integration + Docs
*No dedicated beads ticket — create at end of Phase H*

Deliverables:
- End-to-end test fixture: real `.code_puppy/config.json` with `strict-local` profile, all surfaces wired
- `README.md` section: "Project Workspace Plugin"
- `CHANGES_FROM_UPSTREAM.md`: mark workspace plugin v2 as shipped
- Update kennel with final fork state

---

## Risks Identified During Recon

### RISK-1 (HIGH): Agent/Skill True Filtering Not Achievable from Plugin

**What**: `discover_json_agents()` in `agent_manager.py` and the skill directory scanner in `agent_skills/discovery.py` both hardcode a scan of `~/.code_puppy/agents/` and `~/.code_puppy/skills/` respectively. Neither consults our plugin's scope setting.

**Impact**: The `project` scope for agents and skills means "project items have priority" but global items are still available. True `project`-only isolation requires a core change.

**Proposed fix** (minimal):
- Add `filter_discovered_agents(dict) -> dict` hook call in `agent_manager.py:_discover_agents()` after step 2
- Add `filter_skill_directories(list[Path]) -> list[Path]` hook call at top of `discover_skills()`
- ~15-20 LOC per change, zero behavior change for existing code (no registered handlers = passthrough)

**Decision needed**: Charles decides whether to add these hooks in Phase C/D or accept the limitation. If accepted, document clearly in README.

---

### RISK-2 (MEDIUM): Plugin Tier Filtering Requires Pre-Plugin Boot Init

**What**: User-tier plugins (`~/.code_puppy/plugins/*`) load in `load_plugin_callbacks()` BEFORE our `project_workspace` plugin has any way to communicate "skip user plugins". The `_PLUGINS_LOADED` guard prevents re-loading.

**Impact**: `project` scope for plugins surface cannot suppress user-tier plugins. This makes `strict-local` profile incomplete.

**Proposed fix** (minimal):
- Read `.code_puppy/config.json` in `__main__.py` / very early boot (before `import cli_runner`)
- Set `CODE_PUPPY_SKIP_USER_PLUGINS=1` env var if scope is `project`
- Add 2-line check in `plugins/__init__.py` before user-tier loading
- Total: ~15 LOC across 2 files

**Decision needed**: Charles decides in Phase E. This is the "minimal core hook" budget item for plugins surface.

---

### RISK-3 (LOW): `pre_mcp_autostart` is Notification-Only

**What**: `pre_mcp_autostart(agent_name, server_names)` fires as a notification before servers start. Callbacks can't return anything that modifies the server list. Designed for credential refresh, not filtering.

**Impact**: If we want `project` scope to block specific global MCP servers from STARTING (not just from the registry), `pre_mcp_autostart` won't help. Our `startup` approach of setting `enabled=False` in the registry should prevent servers from being picked up for autostart — but this needs verification.

**Mitigation**: In Phase G, verify that `enabled=False` in MCPManager registry actually prevents autostart. If it doesn't, propose a `filter_mcp_autostart(agent_name, server_names) -> server_names` hook (~10 LOC in `_builder.py`).

---

### RISK-4 (LOW): `hook_engine` Hooks Conflict with `claude_code_hooks`

**What**: For `merge` scope (most common), both `claude_code_hooks` and our plugin will have HookEngine instances responding to the same events. If the same hook script is in BOTH `~/.code_puppy/hooks.json` AND `.code_puppy/hooks.json`, it fires twice.

**Impact**: Duplicate hook executions for shared scripts.

**Mitigation**: Document clearly that `.code_puppy/hooks.json` and `~/.code_puppy/hooks.json` should not duplicate hooks. For `project` scope (where we suppress global), no issue. For `merge`, it's user responsibility to not duplicate. Low severity.

---

### RISK-5 (LOW): CWD Capture at Import Time

**What**: `discover_root()` is called at module import time, capturing `os.getcwd()`. If CWD changes during a session (unlikely in normal usage, but possible with `os.chdir()` calls), the workspace state becomes stale.

**Impact**: Negligible in practice — code_puppy never changes CWD mid-session.

**Mitigation**: Document that workspace root is fixed at startup. If CWD-change support is ever needed, add a `refresh_workspace()` method to `WorkspaceState` (out of scope for v2).

---

### RISK-6 (LOW): `hook_engine` project hooks vs `.claude/settings.json` overlap

**What**: `claude_code_hooks` already loads `.claude/settings.json` as project-level hooks. Our plugin adds `.code_puppy/hooks.json` as ANOTHER project source. Teams might not know which file to use.

**Impact**: Confusion about which file wins; both fire in `merge` mode.

**Mitigation**: Document clearly: `.claude/settings.json` = Claude Code format (loaded by `claude_code_hooks`); `.code_puppy/hooks.json` = same format, additional hooks loaded by our plugin. They're additive. For `project` scope, only `.code_puppy/hooks.json` fires. `.claude/settings.json` is unaffected either way.

---

## Appendix: Hook Inventory at v0.0.574

Full list of `PhaseType` values from `code_puppy/callbacks.py` as of upstream commit `ce88700`. New hooks since AGENTS.md was written are marked `[NEW]`.

| Hook Name | New? | When | Surface |
|---|---|---|---|
| `startup` | | App boot | Used by: project_workspace (MCP + hooks injection) |
| `shutdown` | | Graceful exit | Not used |
| `invoke_agent` | | Sub-agent invoked | Not used |
| `agent_exception` | | Unhandled agent error | Not used |
| `version_check` | | Version check | Not used |
| `edit_file` | | Before edit_file | Not used |
| `create_file` | | Before create_file | Not used |
| `replace_in_file` | | Before replace_in_file | Not used |
| `delete_snippet` | | Before delete_snippet | Not used |
| `delete_file` | | Before delete_file | Not used |
| `run_shell_command` | | Before shell exec | Not used |
| `load_model_config` | | Patch model config | Not used |
| `load_models_config` | | Inject models | Not used |
| `load_model_descriptions` | | Description overlays | Not used |
| `load_prompt` | | System prompt assembly | Not used (optional: workspace context) |
| `agent_reload` | | Agent reload | Not used |
| `custom_command` | | Unknown `/slash` cmd | Not used |
| `custom_command_help` | | `/help` menu | Not used |
| `file_permission` | | Before file op | **Used by: file_permissions surface** |
| `pre_tool_call` | | Before tool executes | **Used by: hooks surface** |
| `post_tool_call` | | After tool finishes | **Used by: hooks surface** |
| `stream_event` | | Response streaming | Not used |
| `register_tools` | | Tool registration | Not used |
| `register_agent_tools` | | Advertise tools | Not used |
| `register_agents` | | Agent catalogue | **Used by: agents surface** |
| `register_model_type` | | Custom model type | Not used |
| `register_skills` | | Skill catalogue | **Used by: skills surface** |
| `get_model_system_prompt` | | Per-model prompt | Not used |
| `prepare_model_prompt` | [NEW] | Model prompt prep | Not used |
| `agent_run_start` | | Before agent task | Not used |
| `agent_run_end` | | After agent run | **Used by: hooks surface** |
| `agent_run_result` | [NEW] | Agent run result | Not used |
| `register_mcp_catalog_servers` | | MCP catalog | Not used (we use direct registry) |
| `register_browser_types` | [NEW] | Browser types | Not used |
| `register_model_providers` | [NEW] | Model providers | Not used |
| `message_history_processor_start` | [NEW] | History proc start | Not used |
| `message_history_processor_end` | [NEW] | History proc end | Not used |
| `on_message` | [NEW] | Message received | Not used |
| `wrap_pydantic_agent` | [NEW] | Agent wrapping | Not used |
| `agent_run_context` | [NEW] | Agent context | Not used |
| `agent_run_cancel` | [NEW] | Agent cancel | Not used |
| `should_skip_fallback_render` | [NEW] | Render control | Not used |
| `pre_mcp_autostart` | | Before MCP autostart | Not used (MCP injected at startup) |
| `interactive_turn_end` | [NEW] | Turn end | Not used |
| `interactive_turn_cancel` | [NEW] | Turn cancel | Not used |
| `agent_pause_requested` | [NEW] | Pause request | Not used |
| `user_prompt_submit` | [NEW] | User prompt | **Used by: hooks surface** |
| `pre_compact` | [NEW] | Before compact | **Used by: hooks surface** |
| `session_end` | [NEW] | Session end | **Used by: hooks surface** |
| `notification` | [NEW] | Notification | **Used by: hooks surface** |

AGENTS.md should be updated to include the `[NEW]` hooks above (separate ticket, not part of workspace plugin scope).
