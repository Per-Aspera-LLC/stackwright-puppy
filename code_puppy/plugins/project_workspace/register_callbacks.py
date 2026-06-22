"""code_puppy.plugins.project_workspace.register_callbacks — plugin entry point.

Registers a ``startup`` callback that:

1. Discovers the project workspace root via :func:`code_puppy.workspace.discover_root`.
2. Loads and validates the workspace config via :mod:`.config`.
3. Logs which profile is active.
4. Stores the resolved config in a module-level singleton.

Surface callbacks (agents, skills, MCP, hooks, file permissions) are
implemented in Phases C–H.  This module is intentionally minimal —
no surface logic lives here yet.

See docs/workspace-plugin-design.md § Phase B for the design rationale.
"""

from __future__ import annotations

import logging

from code_puppy.callbacks import register_callback
from code_puppy.workspace import discover_root

from .config import WorkspaceConfig, load_workspace_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton — populated by _startup(), readable via get_active_config()
# ---------------------------------------------------------------------------

_active_config: WorkspaceConfig | None = None


def get_active_config() -> WorkspaceConfig:
    """Return the resolved workspace config.

    If the ``startup`` callback has not yet fired (e.g. in tests that call
    this before :func:`code_puppy.callbacks.on_startup`), returns a
    ``merge``-defaults config so callers always get a valid object.
    """
    if _active_config is None:
        return WorkspaceConfig()
    return _active_config


# ---------------------------------------------------------------------------
# Startup callback
# ---------------------------------------------------------------------------


def _startup() -> None:
    """Discover workspace root, load config, and cache the result.

    Designed to never raise — any failure falls back to merge defaults
    and logs a warning.
    """
    global _active_config

    try:
        root = discover_root()
        config = load_workspace_config(root)
        _active_config = config

        if root is not None:
            logger.info(
                "[project_workspace] workspace detected at %s — profile=%s",
                root,
                config.profile,
            )
        else:
            logger.debug(
                "[project_workspace] no .code_puppy/ found in tree — profile=merge"
            )

    except Exception as exc:  # pragma: no cover — last-resort safety net
        logger.warning(
            "[project_workspace] startup failed unexpectedly (%s) "
            "— using merge defaults",
            exc,
        )
        _active_config = WorkspaceConfig()


# ---------------------------------------------------------------------------
# Registration — runs at import time (during load_plugin_callbacks)
# ---------------------------------------------------------------------------

register_callback("startup", _startup)
