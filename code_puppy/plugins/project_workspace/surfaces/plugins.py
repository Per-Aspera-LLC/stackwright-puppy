"""Plugins surface — scope-aware plugin tier control.

Unlike every other surface (agents, skills, MCP, hooks, file_permissions) that
registers a callback and acts at *runtime*, the plugins surface acts at
*bootstrap time* — before any ``register_callbacks.py`` has a chance to run.

The actual gating logic lives in ``code_puppy/plugins/__init__.py``, which
calls ``workspace_bootstrap.read_plugin_scope()`` inside
``load_plugin_callbacks()`` to gate user-tier and project-tier loading.

Scope semantics
---------------
merge   — builtin + user + project tiers all load (default; no .code_puppy/ found)
project — builtin + project tiers only; user-tier plugins are skipped
global  — builtin + user tiers only; project-tier plugins are skipped

Profile → scope mapping (for the ``plugins`` surface specifically):
  merge                    → merge
  strict-local             → project
  local-with-global-skills → project
  local-mcp-only           → merge
  custom                   → merge  (override via ``overrides.plugins``)

This module provides:
  get_active_plugin_scope()  — post-startup introspection accessor
  register()                 — symmetry with other surfaces; logs active scope

See docs/workspace-plugin-design.md § RISK-2 and beads ticket code_puppy-rv8.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_active_plugin_scope() -> str:
    """Return the resolved plugins scope for the active workspace.

    Reads from the workspace config populated by the ``startup`` callback.
    Returns ``"merge"`` before the startup callback fires or when no workspace
    is detected — the safe, all-tiers-enabled default.
    """
    from code_puppy.plugins.project_workspace.register_callbacks import (
        get_active_config,
    )

    return get_active_config().surfaces.get("plugins", "merge")


def register() -> None:
    """Log the active plugin scope.

    No callback registration is performed here — gating happens at bootstrap
    time in ``plugins/__init__.py``, well before any callback fires.  This
    function exists for symmetry with the other surface modules and as a hook
    point for future introspection needs.
    """
    scope = get_active_plugin_scope()
    logger.debug(
        "[project_workspace/plugins] active plugin scope: %s"
        " (gating applied at bootstrap)",
        scope,
    )
