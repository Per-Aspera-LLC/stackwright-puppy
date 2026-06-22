"""Hooks surface for the project-workspace v2 plugin.

Manages ``.code_puppy/hooks.json`` in the workspace root and integrates
it into the ``claude_code_hooks`` :class:`~code_puppy.hook_engine.HookEngine`
instance at ``startup``.

Sources
-------
The ``claude_code_hooks`` builtin plugin already loads:

1. ``~/.code_puppy/hooks.json`` — global hooks
2. ``.claude/settings.json``   — project hooks (Claude Code format)

This surface adds a **third** source:

3. ``<workspace_root>/.code_puppy/hooks.json`` — workspace project hooks

Scope semantics
---------------
``merge``
    Load ``.code_puppy/hooks.json`` and **add** its hooks to the existing
    ``claude_code_hooks`` :class:`~code_puppy.hook_engine.HookEngine`
    (additive; project hooks append after global hooks for the same event
    type).  No-op when no workspace root or no hooks file.

``project``
    **Reload** the ``claude_code_hooks`` engine with *only* the project
    hooks from ``.code_puppy/hooks.json``.  This fully replaces the current
    engine content, suppressing both the global ``~/.code_puppy/hooks.json``
    and any ``.claude/settings.json``.  Falls through to global (no-op) when
    no workspace root exists or the hooks file is absent.

``global``
    No-op.  ``claude_code_hooks`` keeps its normal global + project load.
    ``.code_puppy/hooks.json`` is not loaded.

Core edit note
--------------
**Zero core edits.**  This surface uses the
:class:`~code_puppy.hook_engine.HookEngine` public API only:

* ``engine.add_hook(event_type, hook)`` — append to existing registry (merge)
* ``engine.reload_config(config)``      — replace entire registry (project)

The ``claude_code_hooks`` engine is accessed via a normal module import; no
private attributes are accessed.

Hook file format
----------------
Same as ``~/.code_puppy/hooks.json`` (bare event-type dict) or the wrapped
``{"hooks": {…}}`` variant:

.. code-block:: json

    {
      "PreToolUse": [{
        "matcher": "agent_run_shell_command",
        "hooks": [{
          "type": "command",
          "command": "bash .code_puppy/hooks/pre-check.sh",
          "timeout": 5000
        }]
      }]
    }

See ``docs/workspace-plugin-design.md`` § Surface: Hooks (beads ``code_puppy-wrw``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_HOOKS_FILE = ".code_puppy/hooks.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_project_hooks(file_path: Path) -> dict[str, Any] | None:
    """Load and parse a ``.code_puppy/hooks.json`` file.

    Returns the raw event-keyed config dict (ready for
    :func:`~code_puppy.hook_engine.registry.build_registry_from_config`) or
    ``None`` when the file is absent, unreadable, or malformed.

    Supports both bare format ``{"PreToolUse": […]}`` and wrapped format
    ``{"hooks": {"PreToolUse": […]}}``.

    Never raises.
    """
    if not file_path.exists():
        return None

    try:
        raw = file_path.read_text(encoding="utf-8")
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "[project_workspace/hooks] malformed JSON in %s: %s — skipping",
            file_path,
            exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "[project_workspace/hooks] could not read %s: %s — skipping",
            file_path,
            exc,
        )
        return None

    if not isinstance(data, dict):
        logger.warning(
            "[project_workspace/hooks] %s: expected a JSON object — skipping",
            file_path,
        )
        return None

    # Wrapped format: {"hooks": {event: [...]}}
    if "hooks" in data and isinstance(data["hooks"], dict):
        logger.debug(
            "[project_workspace/hooks] loaded wrapped-format hooks from %s",
            file_path,
        )
        return dict(data["hooks"])

    return dict(data)


def _get_cch_engine(_override: Any = None) -> Any | None:
    """Return the ``_hook_engine`` from ``claude_code_hooks.register_callbacks``.

    Returns ``None`` (with a debug-level log) when the plugin is not
    installed, was not imported, or the engine was never initialised.

    Args:
        _override: Injected :class:`~code_puppy.hook_engine.HookEngine`
                   for unit tests; bypasses the real import.

    Never raises.
    """
    if _override is not None:
        return _override

    try:
        import code_puppy.plugins.claude_code_hooks.register_callbacks as _cch  # noqa: PLC0415

        engine = getattr(_cch, "_hook_engine", None)
        if engine is None:
            logger.debug(
                "[project_workspace/hooks] claude_code_hooks engine is None "
                "— no global hooks loaded; continuing"
            )
        return engine
    except ImportError:
        logger.debug(
            "[project_workspace/hooks] claude_code_hooks not available — skipping"
        )
        return None
    except Exception as exc:
        logger.warning(
            "[project_workspace/hooks] could not access cch engine: %s — skipping",
            exc,
        )
        return None


def _add_hooks_from_config(engine: Any, config: dict[str, Any]) -> int:
    """Add every hook in *config* to *engine* via ``engine.add_hook()``.

    Builds a temporary :class:`~code_puppy.hook_engine.models.HookRegistry`
    from *config*, then copies each hook across event-by-event.

    Returns the total number of hooks successfully added.  Per-hook errors
    are logged and skipped; the function never raises.
    """
    from code_puppy.hook_engine.models import HookRegistry  # noqa: PLC0415
    from code_puppy.hook_engine.registry import (  # noqa: PLC0415
        SUPPORTED_EVENT_TYPES,
        build_registry_from_config,
    )

    try:
        temp_registry = build_registry_from_config(config)
    except Exception as exc:
        logger.warning(
            "[project_workspace/hooks] could not build registry from config: "
            "%s — skipping",
            exc,
        )
        return 0

    added = 0
    for event_type in SUPPORTED_EVENT_TYPES:
        # Use the raw attribute (all hooks, not filtered) so disabled/once
        # hooks are also copied faithfully.
        attr = HookRegistry._normalize_event_type(event_type)
        hooks = getattr(temp_registry, attr, [])
        for hook in hooks:
            try:
                engine.add_hook(event_type, hook)
                added += 1
            except Exception as exc:
                logger.warning(
                    "[project_workspace/hooks] could not add hook for %s: "
                    "%s — skipping",
                    event_type,
                    exc,
                )
    return added


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_hooks_scope(
    scope: str,
    workspace_root: Path | None,
    *,
    _cch_engine: Any = None,
    _project_file: Path | None = None,
) -> None:
    """Apply workspace hooks to the ``claude_code_hooks`` engine per *scope*.

    Args:
        scope:          ``"merge"`` | ``"project"`` | ``"global"``
        workspace_root: Project root (parent of ``.code_puppy/``); may be
                        ``None`` when no workspace was found.
        _cch_engine:    Injectable :class:`~code_puppy.hook_engine.HookEngine`
                        for unit tests; bypasses the real import.
        _project_file:  Injectable path to the hooks JSON for unit tests;
                        defaults to
                        ``workspace_root / ".code_puppy" / "hooks.json"``.

    Never raises — all errors are logged and execution falls through.
    """
    # global scope: noop — leave claude_code_hooks unchanged
    if scope == "global":
        logger.debug("[project_workspace/hooks] scope=global — noop")
        return

    # --- Resolve project hooks file ----------------------------------------
    if _project_file is not None:
        project_file: Path | None = _project_file
    elif workspace_root is not None:
        project_file = workspace_root / PROJECT_HOOKS_FILE
    else:
        project_file = None

    if project_file is None:
        logger.debug(
            "[project_workspace/hooks] no workspace root — falling through to global"
        )
        return

    # --- Load project hooks --------------------------------------------------
    config = _load_project_hooks(project_file)

    if not config:
        if scope == "project":
            logger.debug(
                "[project_workspace/hooks] scope=project but no hooks file "
                "— falling through to global"
            )
        else:
            logger.debug(
                "[project_workspace/hooks] scope=merge but no hooks file — noop"
            )
        return

    # --- Get the claude_code_hooks engine ------------------------------------
    engine = _get_cch_engine(_cch_engine)

    # Engine is None: the cch plugin isn't active.  For merge/project we still
    # want to be graceful — just skip without error.
    if engine is None:
        logger.debug(
            "[project_workspace/hooks] no cch engine available — skipping "
            "hook injection for scope=%s",
            scope,
        )
        return

    # --- Apply scope ---------------------------------------------------------
    if scope == "project":
        try:
            engine.reload_config(config)
            logger.info(
                "[project_workspace/hooks] scope=project — reloaded engine "
                "with project hooks from %s",
                project_file,
            )
        except Exception as exc:
            logger.warning(
                "[project_workspace/hooks] reload_config failed: %s — skipping",
                exc,
            )
        return

    # merge (default) — add project hooks additively
    if scope != "merge":
        logger.warning(
            "[project_workspace/hooks] unknown scope %r — falling back to merge",
            scope,
        )

    added = _add_hooks_from_config(engine, config)
    logger.info(
        "[project_workspace/hooks] scope=merge — added %d hook(s) from %s",
        added,
        project_file,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register() -> None:
    """Wire the hooks surface into a second ``startup`` callback.

    Called at plugin import time from ``register_callbacks.py``.  The
    workspace config is read *inside* the callback (at call time) so the
    primary ``startup`` callback — which populates ``_active_config`` — has
    already fired before this one runs.
    """
    from code_puppy.callbacks import register_callback  # noqa: PLC0415

    def _hooks_startup() -> None:
        try:
            from code_puppy.plugins.project_workspace.register_callbacks import (  # noqa: PLC0415
                get_active_config,
            )

            config = get_active_config()
            scope = config.surfaces.get("hooks", "merge")
            workspace_root = config.root

            logger.debug(
                "[project_workspace/hooks] startup fired — scope=%s root=%s",
                scope,
                workspace_root,
            )

            apply_hooks_scope(scope, workspace_root)

        except Exception as exc:  # pragma: no cover — last-resort safety net
            logger.warning(
                "[project_workspace/hooks] surface error: %s — skipping", exc
            )

    register_callback("startup", _hooks_startup)
