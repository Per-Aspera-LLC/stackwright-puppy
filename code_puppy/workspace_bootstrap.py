"""code_puppy.workspace_bootstrap — pre-plugin workspace scope reader.

Reads just enough from .code_puppy/config.json to decide the plugin-loading
scope BEFORE any plugins are loaded.  Must be stdlib-only — no imports from
code_puppy.plugins.* (circular at bootstrap time).

Public API
----------
    read_plugin_scope(cwd: Path | None = None) -> str

Returns "project" | "merge" | "global".  Defaults to "merge" on any error
(safe default — all plugin tiers load normally).

Design note
-----------
This module intentionally duplicates a thin slice of the walk-up logic from
``code_puppy.workspace`` and the profile-defaults table from
``code_puppy.plugins.project_workspace.config``.  Keeping the bootstrap
stdlib-only is more important than strict DRY here — a bad import at this
point would crash startup for everyone.

Phase E of the project-workspace v2 implementation (beads: code_puppy-rv8).
See docs/workspace-plugin-design.md § RISK-2 for full context.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile → plugins-surface scope mapping.
# Mirrors _PROFILE_DEFAULTS["plugins"] in config.py — kept in sync manually.
# ---------------------------------------------------------------------------

_PROFILE_PLUGIN_SCOPE: dict[str, str] = {
    "merge": "merge",
    "strict-local": "project",
    "local-with-global-skills": "project",
    "local-mcp-only": "merge",
    "custom": "merge",
}

_VALID_SCOPES: frozenset[str] = frozenset({"project", "merge", "global"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_config(start: Path) -> Path | None:
    """Walk up from *start* to the .git/ boundary; return .code_puppy/config.json or None.

    Mirrors the ``discover_root()`` walk-up algorithm from ``code_puppy.workspace``.
    ``.code_puppy/`` check runs before ``.git/`` so a repo root that has both
    (the common case) still returns the config.
    """
    current = start.resolve()
    while True:
        code_puppy_dir = current / ".code_puppy"
        if code_puppy_dir.is_dir():
            config_file = code_puppy_dir / "config.json"
            return config_file if config_file.is_file() else None

        if (current / ".git").exists():
            return None

        parent = current.parent
        if parent == current:
            # Filesystem root — no boundary found.
            return None
        current = parent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_plugin_scope(cwd: Path | None = None) -> str:
    """Read just enough config to decide plugin-loading scope.

    Walks up from *cwd* to the .git/ boundary looking for
    .code_puppy/config.json.  Resolves the ``plugins`` surface scope from
    the profile + any ``overrides.plugins`` key.

    Args:
        cwd: Directory to start from.  Defaults to ``Path.cwd()``.

    Returns:
        One of ``"project"`` | ``"merge"`` | ``"global"``.  Always returns
        ``"merge"`` on any error — the safe default that leaves all plugin
        tiers enabled.
    """
    try:
        config_path = _find_config(Path(cwd) if cwd is not None else Path.cwd())
        if config_path is None:
            return "merge"

        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return "merge"

        profile = data.get("profile", "merge")
        if not isinstance(profile, str) or profile not in _PROFILE_PLUGIN_SCOPE:
            logger.warning(
                "[workspace_bootstrap] unknown profile %r — defaulting plugins scope to merge",
                profile,
            )
            profile = "merge"

        scope = _PROFILE_PLUGIN_SCOPE[profile]

        overrides = data.get("overrides", {})
        if isinstance(overrides, dict):
            plugin_override = overrides.get("plugins")
            if plugin_override is not None:
                if (
                    isinstance(plugin_override, str)
                    and plugin_override in _VALID_SCOPES
                ):
                    scope = plugin_override
                else:
                    logger.warning(
                        "[workspace_bootstrap] invalid plugins override %r"
                        " — keeping profile default (%r)",
                        plugin_override,
                        scope,
                    )

        return scope

    except Exception as exc:
        logger.warning(
            "[workspace_bootstrap] failed to read plugin scope (%s)"
            " — defaulting to merge",
            exc,
        )
        return "merge"
