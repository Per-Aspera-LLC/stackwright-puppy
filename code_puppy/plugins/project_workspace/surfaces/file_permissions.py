"""File permissions surface for the project-workspace v2 plugin.

Registers a ``file_permission`` callback that enforces per-scope file op
restrictions:

- ``global``:  noop — defers entirely to upstream's interactive prompt.
- ``merge``:   advisory policy only — applies ``deny`` rules from
               ``.code_puppy/file_policy.json`` if present.  No auto-restriction
               to project root.
- ``project``: auto-restricts to the workspace root.  File ops outside the
               root are silently blocked unless an ``allow`` rule or
               ``allow_outside_project: true`` explicitly permits them.
               ``deny`` rules always override ``allow`` rules.

Policy file (``.code_puppy/file_policy.json``) schema::

    {
        "allow": ["./build/**", "/tmp/**"],
        "deny":  ["./secrets/**", "**/.env"],
        "allow_outside_project": false
    }

Pattern resolution rules:

- Relative patterns (``./foo/**``) are resolved against the workspace root.
- Home-dir patterns (``~/cache/**``) are expanded via
  :func:`pathlib.Path.expanduser`.
- All other patterns are used as-is with :func:`fnmatch.fnmatch`.
- ``*`` in Python's fnmatch matches any character including path separators,
  so ``**/secrets/**`` matches absolute paths containing ``/secrets/``.
- Deny wins on conflict: if both ``allow`` and ``deny`` match, ``deny`` wins.
- Missing / malformed policy → treated as empty (no overlay, no crash).

Registration
------------
``register()`` installs a secondary ``startup`` callback that:

1. Reads the resolved workspace config (populated by the primary startup hook).
2. Caches scope + workspace root + policy in module-level state.
3. Inserts ``_workspace_file_permission`` at **position 0** of the
   ``file_permission`` callback list, ensuring our check fires *before*
   the interactive prompt from ``file_permission_handler`` so that
   blocked paths are denied silently.

Core edit note
--------------
None.  Pure plugin — uses PEP-8 private access to
``code_puppy.callbacks._callbacks`` only to insert at position 0.

See docs/workspace-plugin-design.md § Surface Integration Plan → 6. File
Permissions Surface and beads ticket ``code_puppy-9w1``.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Policy filename inside ``.code_puppy/``.
POLICY_FILENAME = "file_policy.json"


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------


def load_policy(workspace_root: Path | None) -> dict | None:
    """Load ``.code_puppy/file_policy.json`` from *workspace_root*.

    Returns the parsed dict on success; ``None`` when the file is absent or
    unreadable.  Never raises.
    """
    if workspace_root is None:
        return None
    policy_path = workspace_root / ".code_puppy" / POLICY_FILENAME
    if not policy_path.exists():
        return None
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[project_workspace/file_perms] failed to load %s: %s — treating as empty policy",
            policy_path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "[project_workspace/file_perms] %s must be a JSON object — ignoring",
            policy_path,
        )
        return None
    return data


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_path(file_path: str | Path) -> Path:
    """Return the resolved absolute path, following symlinks.

    Falls back to :func:`Path.absolute` when :func:`Path.resolve` raises
    (e.g. a non-existent path on an unusual filesystem).
    """
    try:
        return Path(file_path).resolve()
    except OSError:
        return Path(file_path).absolute()


def is_inside_workspace(resolved_path: Path, workspace_root: Path) -> bool:
    """Return ``True`` if *resolved_path* is contained within the resolved *workspace_root*.

    Both paths are resolved before comparison so symlinks cannot trick the
    containment check.
    """
    try:
        return resolved_path.is_relative_to(workspace_root.resolve())
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Pattern resolution + matching
# ---------------------------------------------------------------------------


def _resolve_pattern(pattern: str, workspace_root: Path | None) -> str:
    """Convert a possibly-relative glob pattern to an absolute form.

    ``./foo/**``  →  ``{workspace_root}/foo/**``  (or CWD if no root)
    ``~/bar/**``  →  ``{HOME}/bar/**``
    All other patterns are returned unchanged (including absolute ``/tmp/**``
    and double-star ``**/secrets/**`` patterns).
    """
    if pattern.startswith("~/"):
        try:
            return str(Path(pattern).expanduser())
        except RuntimeError:
            return pattern
    if pattern.startswith("./"):
        base = workspace_root if workspace_root is not None else Path.cwd()
        # "./foo/**" → "/workspace/foo/**"
        return str(base) + pattern[1:]
    if pattern == ".":
        base = workspace_root if workspace_root is not None else Path.cwd()
        return str(base)
    return pattern


def matches_any(
    resolved_path: Path,
    patterns: list[str],
    workspace_root: Path | None = None,
) -> bool:
    """Return ``True`` if *resolved_path* matches any glob pattern.

    Patterns are resolved against *workspace_root* when they are relative
    (``./``-prefixed).  Python :func:`fnmatch.fnmatch` is used; note that
    ``*`` matches **all** characters including path separators.
    """
    path_str = str(resolved_path)
    for pattern in patterns:
        abs_pattern = _resolve_pattern(pattern, workspace_root)
        if fnmatch.fnmatch(path_str, abs_pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Core check function (pure, fully testable without module state)
# ---------------------------------------------------------------------------


def check_file_permission(
    context: Any,
    file_path: str | Path,
    operation: str,
    scope: str,
    workspace_root: Path | None,
    policy: dict | None,
) -> bool | None:
    """Evaluate project-workspace file permission policy.

    This is the pure, testable entry point.  It resolves *file_path* to an
    absolute path (following symlinks) and delegates to :func:`_evaluate`.

    Args:
        context:        Tool context — passed through, not inspected here.
        file_path:      Path from the tool call; may be relative or absolute.
        operation:      Operation description string (e.g. ``"write"``).
        scope:          ``"global"`` | ``"merge"`` | ``"project"``.
        workspace_root: Resolved workspace root from ``discover_root()``.
        policy:         Parsed ``.code_puppy/file_policy.json`` dict, or
                        ``None`` when absent.

    Returns:
        ``False``  — silently block the operation.
        ``None``   — defer to other callbacks (interactive prompt etc.).
    """
    try:
        resolved = resolve_path(file_path)
        return _evaluate(resolved, scope, workspace_root, policy)
    except Exception as exc:  # pragma: no cover — last-resort defensive catch
        logger.warning(
            "[project_workspace/file_perms] unexpected error checking %r (%s) — deferring",
            str(file_path),
            exc,
        )
        return None


def _evaluate(
    resolved: Path,
    scope: str,
    workspace_root: Path | None,
    policy: dict | None,
) -> bool | None:
    """Inner evaluation — receives a pre-resolved path."""
    if scope == "global":
        return None

    deny_patterns: list[str] = policy.get("deny", []) if policy else []
    allow_patterns: list[str] = policy.get("allow", []) if policy else []

    # ------------------------------------------------------------------
    # merge scope: advisory deny rules only, no auto-restriction
    # ------------------------------------------------------------------
    if scope == "merge":
        if deny_patterns and matches_any(resolved, deny_patterns, workspace_root):
            logger.debug(
                "[project_workspace/file_perms] merge-scope deny: %s", resolved
            )
            return False
        return None

    # ------------------------------------------------------------------
    # project scope: auto-restrict to workspace root + policy overlays
    # ------------------------------------------------------------------
    if scope == "project":
        if workspace_root is None:
            # No workspace detected — behave like global (no restriction)
            return None

        # Deny always wins, regardless of whether path is inside/outside root
        if deny_patterns and matches_any(resolved, deny_patterns, workspace_root):
            logger.debug(
                "[project_workspace/file_perms] project-scope deny: %s", resolved
            )
            return False

        # Inside workspace root: deny was the only possible block → defer
        if is_inside_workspace(resolved, workspace_root):
            return None

        # Outside workspace root — check explicit allow permissions
        allow_outside = (
            bool(policy.get("allow_outside_project", False)) if policy else False
        )
        if allow_outside:
            return None
        if allow_patterns and matches_any(resolved, allow_patterns, workspace_root):
            return None

        # Default: block (outside workspace root, not explicitly permitted)
        logger.debug(
            "[project_workspace/file_perms] project-scope outside-root block: %s (root=%s)",
            resolved,
            workspace_root,
        )
        return False

    # Unknown scope — fall through safely
    logger.warning("[project_workspace/file_perms] unknown scope %r — deferring", scope)
    return None


# ---------------------------------------------------------------------------
# Module-level state (populated once at startup, read on every file op)
# ---------------------------------------------------------------------------

_scope: str = "merge"
_workspace_root: Path | None = None
_policy: dict | None = None


# ---------------------------------------------------------------------------
# file_permission callback
# ---------------------------------------------------------------------------


def _workspace_file_permission(
    context: Any,
    file_path: str,
    operation: str,
    preview: str | None = None,
    message_group: str | None = None,
    operation_data: Any = None,
) -> bool | None:
    """``file_permission`` callback — delegates to :func:`check_file_permission`."""
    return check_file_permission(
        context,
        file_path,
        operation,
        _scope,
        _workspace_root,
        _policy,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register() -> None:
    """Register the ``file_permission`` callback via a secondary startup hook.

    Pattern mirrors ``surfaces/mcp.py``: we register a second ``startup``
    callback that reads the resolved config singleton (set by the primary
    startup hook), caches scope + workspace root + policy in module state,
    then inserts ``_workspace_file_permission`` at **position 0** of the
    ``file_permission`` callback list.

    Position 0 insertion ensures our scope check fires *before*
    ``file_permission_handler``\\'s interactive prompt, so paths blocked by
    project policy are denied silently without first prompting the user.

    For ``global`` scope no callback is registered (explicit noop).
    """
    from code_puppy.callbacks import register_callback

    def _fp_startup() -> None:
        global _scope, _workspace_root, _policy
        try:
            from code_puppy.plugins.project_workspace.register_callbacks import (
                get_active_config,
            )

            config = get_active_config()
            scope = config.surfaces.get("file_permissions", "merge")
            workspace_root = config.root
            policy = load_policy(workspace_root)

            _scope = scope
            _workspace_root = workspace_root
            _policy = policy

            logger.debug(
                "[project_workspace/file_perms] startup — scope=%s root=%s policy=%s",
                scope,
                workspace_root,
                "loaded" if policy else "none",
            )

            if scope == "global":
                # noop: let upstream's interactive prompt handle everything
                return

            # Insert at position 0 so our check fires before the interactive
            # prompt from file_permission_handler.
            from code_puppy.callbacks import _callbacks

            if _workspace_file_permission not in _callbacks["file_permission"]:
                _callbacks["file_permission"].insert(0, _workspace_file_permission)
                logger.debug(
                    "[project_workspace/file_perms] callback registered at position 0"
                )

        except Exception as exc:
            logger.warning(
                "[project_workspace/file_perms] startup failed (%s) — skipping", exc
            )

    register_callback("startup", _fp_startup)
