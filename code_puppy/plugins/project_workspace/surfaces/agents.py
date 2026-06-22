"""Agents surface for the project-workspace v2 plugin.

Registers a ``register_agents`` callback that loads agent JSON files
according to the workspace config's ``agents`` scope.

Scope semantics
---------------
merge   — global agents + workspace-root project agents combined; project wins
          on name collision.  This supplements what upstream
          ``discover_json_agents()`` already does — importantly, it covers the
          subdir case where ``os.getcwd() != workspace_root`` and
          ``discover_json_agents()`` would otherwise miss the workspace's
          ``.code_puppy/agents/`` directory entirely.

project — workspace-root project agents ONLY.  Global agents discovered by
          upstream ``discover_json_agents()`` are excluded via the
          ``{"name": …, "exclude": True}`` mechanism added to agent_manager.
          Falls through to global behaviour (returns ``[]``) when no workspace
          root exists or the workspace has no ``agents/`` directory.

global  — global agents ONLY.  Any project agents that upstream
          ``discover_json_agents()`` already loaded from
          ``os.getcwd()/.code_puppy/agents/`` are excluded.

Core edit note
--------------
This surface depends on a two-line addition to
``code_puppy/agents/agent_manager.py`` (step 3 of ``_discover_agents``):

    elif agent_def.get("exclude") is True:
        _AGENT_REGISTRY.pop(agent_name, None)

Without that change, ``project`` and ``global`` scopes can only *add* agents
but cannot remove agents that upstream's hardcoded discovery already loaded.
The edit is minimal (2 effective lines) and makes ``register_agents`` fully
bidirectional — plugins may add OR exclude named agents.

See docs/workspace-plugin-design.md § Surface Integration Plan → 1. Agents
and beads ticket code_puppy-ve9.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_agents_from_dir(agents_dir: Path) -> list[dict[str, Any]]:
    """Return a list of valid agent dicts from *agents_dir*.

    Each result has the shape ``{"name": str, "json_path": str}`` as expected
    by ``agent_manager._discover_agents`` step 3.

    Malformed or unreadable files are logged as warnings and skipped; the
    function never raises.
    """
    results: list[dict[str, Any]] = []
    if not agents_dir.exists() or not agents_dir.is_dir():
        return results

    try:
        from code_puppy.agents.json_agent import JSONAgent
    except Exception as exc:  # pragma: no cover — JSONAgent always importable
        logger.warning(
            "[project_workspace/agents] could not import JSONAgent: %s — "
            "skipping directory %s",
            exc,
            agents_dir,
        )
        return results

    for json_file in sorted(agents_dir.glob("*.json")):
        try:
            agent = JSONAgent(str(json_file))
            results.append({"name": agent.name, "json_path": str(json_file)})
        except Exception as exc:
            logger.warning(
                "[project_workspace/agents] skipping %s: %s",
                json_file.name,
                exc,
            )

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_agents_for_scope(
    scope: str,
    workspace_root: Path | None,
    *,
    _global_dir: Path | None = None,
    _cwd_project_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Discover agents based on the configured scope.

    Args:
        scope:            ``"merge"`` | ``"project"`` | ``"global"``
        workspace_root:   From ``discover_root()`` / ``config.root``; may be
                          ``None`` when no workspace was found.
        _global_dir:      Injectable override for the global agents directory
                          (``~/.code_puppy/agents/``).  Used in tests.
        _cwd_project_dir: Injectable override for the cwd-relative project
                          agents directory that ``discover_json_agents()``
                          would have loaded from.  Used in tests.

    Returns:
        List of agent dicts understood by ``agent_manager._discover_agents``
        step 3.  Each dict has one of:
        - ``{"name": str, "json_path": str}`` — register/overwrite agent
        - ``{"name": str, "exclude": True}``  — remove agent from registry
    """
    # -- Resolve the global agents directory --------------------------------
    if _global_dir is not None:
        global_dir = _global_dir
    else:
        try:
            from code_puppy.config import get_user_agents_directory

            global_dir = Path(get_user_agents_directory())
        except Exception as exc:  # pragma: no cover — config always importable
            logger.warning(
                "[project_workspace/agents] could not resolve global agents dir: %s"
                " — returning empty list",
                exc,
            )
            return []

    # -- Resolve workspace-root project agents directory --------------------
    project_dir: Path | None = (
        workspace_root / ".code_puppy" / "agents"
        if workspace_root is not None
        else None
    )

    # -- Resolve cwd-relative project dir (what discover_json_agents loaded) --
    if _cwd_project_dir is not None:
        cwd_project_dir: Path | None = _cwd_project_dir
    else:
        try:
            from code_puppy.config import get_project_agents_directory

            raw = get_project_agents_directory()
            cwd_project_dir = Path(raw) if raw is not None else None
        except Exception:  # pragma: no cover
            cwd_project_dir = None

    # -----------------------------------------------------------------------
    # Scope logic
    # -----------------------------------------------------------------------

    if scope == "merge":
        # upstream discover_json_agents() already handles global + cwd-project.
        # We only need to act when workspace_root is a parent dir above cwd so
        # that the workspace's agents/ directory is included even from a subdir.
        if project_dir is None:
            return []
        return _load_agents_from_dir(project_dir)

    if scope == "project":
        # Only workspace-root project agents; fall through to global when no
        # workspace or no agents dir exists.
        if project_dir is None or not project_dir.exists():
            logger.debug(
                "[project_workspace/agents] project scope but no agents dir"
                " — falling through to global"
            )
            return []

        project_agents = _load_agents_from_dir(project_dir)
        project_names = {a["name"] for a in project_agents}

        # Exclude global agents not overridden by a project agent
        global_agents = _load_agents_from_dir(global_dir)
        exclusions = [
            {"name": a["name"], "exclude": True}
            for a in global_agents
            if a["name"] not in project_names
        ]
        return project_agents + exclusions

    if scope == "global":
        # Exclude any project agents that upstream discover_json_agents may
        # have loaded from os.getcwd()/.code_puppy/agents/.
        if cwd_project_dir is None:
            return []

        cwd_project_agents = _load_agents_from_dir(cwd_project_dir)
        if not cwd_project_agents:
            return []

        global_names = {a["name"] for a in _load_agents_from_dir(global_dir)}
        return [
            {"name": a["name"], "exclude": True}
            for a in cwd_project_agents
            if a["name"] not in global_names
        ]

    # Unknown scope — log and behave like merge
    logger.warning(
        "[project_workspace/agents] unknown scope %r — falling back to merge",
        scope,
    )
    if project_dir is None:
        return []
    return _load_agents_from_dir(project_dir)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register() -> None:
    """Wire the agents surface into the ``register_agents`` callback.

    Called at plugin import time from ``register_callbacks.py``.  The
    callback itself reads the active config at *call* time so it always sees
    the fully-resolved workspace config set by the ``startup`` hook.
    """
    from code_puppy.callbacks import register_callback

    def _register_agents() -> list[dict[str, Any]]:
        try:
            from code_puppy.plugins.project_workspace.register_callbacks import (
                get_active_config,
            )

            config = get_active_config()
            scope = config.surfaces.get("agents", "merge")
            workspace_root = config.root

            logger.debug(
                "[project_workspace/agents] register_agents fired — scope=%s root=%s",
                scope,
                workspace_root,
            )

            return get_agents_for_scope(scope, workspace_root)

        except Exception as exc:
            logger.warning(
                "[project_workspace/agents] surface error: %s — skipping", exc
            )
            return []

    register_callback("register_agents", _register_agents)
