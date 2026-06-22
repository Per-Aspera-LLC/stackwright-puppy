"""code_puppy.plugins.project_workspace.config — profile + override loading.

Public API
----------
    load_workspace_config(root: Path | None) -> WorkspaceConfig

Given a workspace root (parent of ``.code_puppy/``), loads and validates
``.code_puppy/config.json``.  Missing or malformed files fall back to
the ``merge`` profile — never raises.

Profiles (see docs/workspace-plugin-design.md § Configuration)
---------------------------------------------------------------
    merge                  — all surfaces: merge (default)
    strict-local           — all surfaces: project
    local-with-global-skills — agents/skills differ; rest project
    local-mcp-only         — only mcp: project; rest merge
    custom                 — all surfaces default to merge; specify via overrides
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All recognised extension surfaces, in a stable order.
SURFACES: tuple[str, ...] = (
    "agents",
    "skills",
    "plugins",
    "mcp",
    "hooks",
    "file_permissions",
)

#: Accepted scope values for any surface.
VALID_SCOPES: frozenset[str] = frozenset({"project", "merge", "global"})

#: Accepted profile names.
VALID_PROFILES: frozenset[str] = frozenset(
    {
        "merge",
        "strict-local",
        "local-with-global-skills",
        "local-mcp-only",
        "custom",
    }
)

# ---------------------------------------------------------------------------
# Profile defaults table (design doc § Profiles)
# ---------------------------------------------------------------------------


def _all_merge() -> dict[str, str]:
    """Return a fresh dict with every surface set to 'merge'."""
    return {s: "merge" for s in SURFACES}


def _all_project() -> dict[str, str]:
    """Return a fresh dict with every surface set to 'project'."""
    return {s: "project" for s in SURFACES}


_PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "merge": _all_merge(),
    "strict-local": _all_project(),
    "local-with-global-skills": {
        "agents": "merge",
        "skills": "global",
        "plugins": "project",
        "mcp": "project",
        "hooks": "project",
        "file_permissions": "project",
    },
    "local-mcp-only": {
        "agents": "merge",
        "skills": "merge",
        "plugins": "merge",
        "mcp": "project",
        "hooks": "merge",
        "file_permissions": "merge",
    },
    # 'custom' starts from merge-all; user MUST specify overrides for any
    # surface they want to restrict.
    "custom": _all_merge(),
}


# ---------------------------------------------------------------------------
# WorkspaceConfig
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceConfig:
    """Fully-resolved workspace configuration.

    Attributes:
        profile:  Name of the resolved profile (after fallback).
        surfaces: Per-surface scope mapping (``"merge" | "project" | "global"``).
        root:     Project root directory (parent of ``.code_puppy/``), or
                  ``None`` when no workspace was detected.
    """

    profile: str = "merge"
    surfaces: dict[str, str] = field(default_factory=_all_merge)
    root: Path | None = None


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_workspace_config(root: Path | None) -> WorkspaceConfig:
    """Load and validate workspace config from ``<root>/.code_puppy/config.json``.

    Args:
        root: Directory that contains ``.code_puppy/`` (the value returned by
              ``discover_root()``).  When ``None`` no workspace was found and
              ``merge`` defaults are returned immediately.

    Returns:
        A :class:`WorkspaceConfig`.  Never raises — all errors are logged and
        fall back to the ``merge`` profile.
    """
    if root is None:
        return WorkspaceConfig(profile="merge", surfaces=_all_merge(), root=None)

    config_path = root / ".code_puppy" / "config.json"

    if not config_path.exists():
        logger.debug(
            "[project_workspace] .code_puppy/ found but config.json missing "
            "— using profile=merge"
        )
        return WorkspaceConfig(profile="merge", surfaces=_all_merge(), root=root)

    # ---- parse JSON --------------------------------------------------------
    try:
        raw = config_path.read_text(encoding="utf-8")
        data: object = json.loads(raw)
    except Exception as exc:
        logger.warning(
            "[project_workspace] malformed config.json at %s — "
            "falling back to profile=merge (%s)",
            config_path,
            exc,
        )
        return WorkspaceConfig(profile="merge", surfaces=_all_merge(), root=root)

    if not isinstance(data, dict):
        logger.warning(
            "[project_workspace] config.json must be a JSON object "
            "— falling back to profile=merge"
        )
        return WorkspaceConfig(profile="merge", surfaces=_all_merge(), root=root)

    # ---- resolve profile ---------------------------------------------------
    profile = data.get("profile", "merge")
    if not isinstance(profile, str) or profile not in VALID_PROFILES:
        logger.warning(
            "[project_workspace] unknown profile %r — falling back to merge",
            profile,
        )
        profile = "merge"

    surfaces = _PROFILE_DEFAULTS[profile].copy()

    # ---- apply overrides ---------------------------------------------------
    overrides = data.get("overrides", {})
    if isinstance(overrides, dict):
        for surface, scope in overrides.items():
            if surface not in SURFACES:
                logger.warning(
                    "[project_workspace] unknown surface %r in overrides — skipping",
                    surface,
                )
                continue
            if not isinstance(scope, str) or scope not in VALID_SCOPES:
                logger.warning(
                    "[project_workspace] invalid scope %r for surface %r "
                    "— falling back to merge",
                    scope,
                    surface,
                )
                surfaces[surface] = "merge"
            else:
                surfaces[surface] = scope

    return WorkspaceConfig(profile=profile, surfaces=surfaces, root=root)
