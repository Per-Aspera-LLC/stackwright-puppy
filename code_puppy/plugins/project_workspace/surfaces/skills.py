"""Skills surface for the project-workspace v2 plugin.

Registers a ``register_skills`` callback that loads skill SKILL.md files
according to the workspace config's ``skills`` scope.

Scope semantics
---------------
merge   — global skills + workspace-root project skills combined; project wins
          on name collision (via exclude-then-add when workspace_root != cwd).
          When workspace_root == cwd the upstream ``discover_skills()`` already
          handles everything and the callback is a no-op.

project — workspace-root project skills ONLY.  Global skills discovered by
          upstream ``discover_skills()`` are excluded via the
          ``{"name": …, "exclude": True}`` mechanism added to
          ``agent_skills/discovery.py``.  Falls through to global behaviour
          (returns ``[]``) when no workspace root exists or the workspace has
          no ``skills/`` directory.

global  — global skills ONLY.  Any project skills that upstream
          ``discover_skills()`` already loaded from
          ``os.getcwd()/.code_puppy/skills/`` are excluded.

Core edit note
--------------
This surface depends on a change to
``code_puppy/plugins/agent_skills/discovery.py`` (``_collect_plugin_skills``):

    if entry.get("exclude") is True:
        name = str(entry.get("name") or "").strip()
        if name:
            exclusions.add(name)
        continue

``_collect_plugin_skills`` now returns ``(plugin_skills, exclusions)`` and
``discover_skills()`` applies exclusions *before* adding plugin skills, so an
excluded name can be re-added with a project-scoped SKILL.md path.

Without that change, ``project`` and ``global`` scopes can only *add* skills
but cannot remove skills that upstream's hardcoded directory scan already
loaded.

See docs/workspace-plugin-design.md § Surface Integration Plan → 2. Skills
and beads ticket code_puppy-avm.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_skills_from_dir(skills_dir: Path) -> list[dict[str, Any]]:
    """Return a list of valid skill dicts from *skills_dir*.

    Each result has the shape ``{"name": str, "skill_md_path": str}`` as
    expected by ``agent_skills/discovery._collect_plugin_skills``.  The
    ``name`` is the skill's **directory name** so it matches the
    ``SkillInfo.name`` used by the filesystem scanner.

    Skill directories without a ``SKILL.md`` file are logged as warnings and
    skipped; the function never raises.
    """
    results: list[dict[str, Any]] = []
    if not skills_dir.exists() or not skills_dir.is_dir():
        return results

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            logger.warning(
                "[project_workspace/skills] skipping %s — no SKILL.md found",
                skill_dir.name,
            )
            continue
        results.append({"name": skill_dir.name, "skill_md_path": str(skill_md)})

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_skills_for_scope(
    scope: str,
    workspace_root: Path | None,
    *,
    _global_dir: Path | None = None,
    _cwd_project_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Discover skills based on the configured scope.

    Args:
        scope:            ``"merge"`` | ``"project"`` | ``"global"``
        workspace_root:   From ``discover_root()`` / ``config.root``; may be
                          ``None`` when no workspace was found.
        _global_dir:      Injectable override for the global skills directory
                          (``~/.code_puppy/skills/``).  Used in tests.
        _cwd_project_dir: Injectable override for the cwd-relative project
                          skills directory that ``discover_skills()`` would
                          have scanned from.  Used in tests.

    Returns:
        List of skill dicts understood by
        ``agent_skills/discovery._collect_plugin_skills``.
        Each dict has one of:
        - ``{"name": str, "skill_md_path": str}``  — register/add skill
        - ``{"name": str, "exclude": True}``        — remove skill from results
    """
    # -- Resolve the global skills directory --------------------------------
    if _global_dir is not None:
        global_dir = _global_dir
    else:
        # Mirrors get_default_skill_directories()[0] in agent_skills/discovery.py
        global_dir = Path.home() / ".code_puppy" / "skills"

    # -- Resolve workspace-root project skills directory --------------------
    project_dir: Path | None = (
        workspace_root / ".code_puppy" / "skills"
        if workspace_root is not None
        else None
    )

    # -- Resolve cwd-relative project dir (what discover_skills() scanned) --
    if _cwd_project_dir is not None:
        cwd_project_dir: Path | None = _cwd_project_dir
    else:
        # Mirrors get_default_skill_directories()[1] in agent_skills/discovery.py
        cwd_project_dir = Path.cwd() / ".code_puppy" / "skills"

    # -----------------------------------------------------------------------
    # Scope logic
    # -----------------------------------------------------------------------

    if scope == "merge":
        # upstream discover_skills() already handles global + cwd-project.
        # We only need to act when workspace_root is a parent dir above cwd so
        # that the workspace's skills/ directory is included even from a subdir.
        if project_dir is None:
            return []
        project_skills = _load_skills_from_dir(project_dir)
        if not project_skills:
            return []
        # Emit exclusions for global skills overridden by workspace project
        # skills so project wins on name collision (mirrors agents merge behaviour).
        project_names = {s["name"] for s in project_skills}
        global_skills = _load_skills_from_dir(global_dir)
        exclusions = [
            {"name": s["name"], "exclude": True}
            for s in global_skills
            if s["name"] in project_names
        ]
        return exclusions + project_skills

    if scope == "project":
        # Only workspace-root project skills; fall through to global when no
        # workspace or no skills dir exists.
        if project_dir is None or not project_dir.exists():
            logger.debug(
                "[project_workspace/skills] project scope but no skills dir"
                " — falling through to global"
            )
            return []

        project_skills = _load_skills_from_dir(project_dir)
        project_names = {s["name"] for s in project_skills}

        # Exclude global skills not overridden by a project skill
        global_skills = _load_skills_from_dir(global_dir)
        exclusions = [
            {"name": s["name"], "exclude": True}
            for s in global_skills
            if s["name"] not in project_names
        ]
        return project_skills + exclusions

    if scope == "global":
        # Exclude any project skills that upstream discover_skills() may have
        # loaded from os.getcwd()/.code_puppy/skills/.
        if cwd_project_dir is None or not cwd_project_dir.exists():
            return []

        cwd_project_skills = _load_skills_from_dir(cwd_project_dir)
        if not cwd_project_skills:
            return []

        global_names = {s["name"] for s in _load_skills_from_dir(global_dir)}
        return [
            {"name": s["name"], "exclude": True}
            for s in cwd_project_skills
            if s["name"] not in global_names
        ]

    # Unknown scope — log and behave like merge
    logger.warning(
        "[project_workspace/skills] unknown scope %r — falling back to merge",
        scope,
    )
    if project_dir is None:
        return []
    return _load_skills_from_dir(project_dir)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register() -> None:
    """Wire the skills surface into the ``register_skills`` callback.

    Called at plugin import time from ``register_callbacks.py``.  The
    callback itself reads the active config at *call* time so it always sees
    the fully-resolved workspace config set by the ``startup`` hook.
    """
    from code_puppy.callbacks import register_callback

    def _register_skills() -> list[dict[str, Any]]:
        try:
            from code_puppy.plugins.project_workspace.register_callbacks import (
                get_active_config,
            )

            config = get_active_config()
            scope = config.surfaces.get("skills", "merge")
            workspace_root = config.root

            logger.debug(
                "[project_workspace/skills] register_skills fired — scope=%s root=%s",
                scope,
                workspace_root,
            )

            return get_skills_for_scope(scope, workspace_root)

        except Exception as exc:
            logger.warning(
                "[project_workspace/skills] surface error: %s — skipping", exc
            )
            return []

    register_callback("register_skills", _register_skills)
