# Changes from upstream code-puppy

Base: `code-puppy@0.0.545` — https://github.com/mpfaffenberger/code_puppy

## Applied: PR #355 — Project Workspace (`.code_puppy/`)

**Branch**: `feature/project-workspace`
**PR**: https://github.com/mpfaffenberger/code_puppy/pull/355

Adds a unified project-local workspace directory (`.code_puppy/`) — the `.vscode/` pattern
applied to code-puppy. Projects can define their own MCP servers, JSON agents, and plugins
without touching global `~/.code_puppy/` config.

Walk-up discovery: on startup, code-puppy walks from CWD to the git root looking for
`.code_puppy/`. An optional `projectOnly` flag in `.code_puppy/config.json` switches from
additive merge to full isolation (global config skipped entirely).

```
my-project/
└── .code_puppy/
    ├── config.json       # { "projectOnly": false }  ← optional
    ├── mcp_servers.json  # project MCP servers
    ├── agents/           # JSON agent definitions
    └── plugins/          # project-scoped plugins
```

**Key design decisions:**
- Git-root boundary — walk-up stops at `.git/`, preventing config bleed across monorepo siblings
- Ephemeral local servers — local MCP servers bypass `mcp_registry.json` (session-scoped, never persisted)
- `projectOnly: false` is the default — zero behaviour change for users without `.code_puppy/`
- Builtin plugins always load — even in `projectOnly` mode, code-puppy's own internals still run

**Commits applied** (cherry-picked onto `origin/main @ 0.0.545`):
- `fe406ab` → `e8fb6f6` feat: project workspace discovery with .code-puppy/ directory
- `bf27c88` → `59ae44b` feat: MCP loading from workspace + projectOnly blocks global config
- `82324cc` → `9803c44` feat: agent discovery respects projectOnly isolation
- `51211f3` → `2e8b89b` feat: project-local plugin loading with projectOnly gate
- `b631dcb` → `9a7ae53` chore: rename workspace dir .code-puppy/ -> .code_puppy/ for consistency
- `82ce11b` → `c708c58` fix: resolve CI failures - ruff lint errors and test assertions

## Fork identity only

The only other divergence from upstream is in `pyproject.toml` --- package name, description,
repository URLs, and the `raft-puppy` entry point alias. No logic changes.

### Version detection cascade (cherry-pick this after rebase)

`code_puppy/__init__.py` and `code_puppy/pydantic_patches.py` try
`importlib.metadata.version("stackwright-puppy")` first, then fall back to
`"code-puppy"`, then `"0.0.0-dev"`. Upstream only has the `"code-puppy"` lookup.

When rebasing onto a new upstream release, these two files will be clobbered.
Cherry-pick the commit titled `fix: version cascade for stackwright-puppy fork identity`
to restore correct `raft-puppy --version` output.
