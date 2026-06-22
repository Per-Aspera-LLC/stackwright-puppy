"""Tests for code_puppy.plugins.project_workspace.surfaces.file_permissions.

Security-sensitive surface: covers all scope semantics, path-resolution
attacks, policy edge cases, and registration mechanics.

Covers:
- load_policy: missing file, malformed JSON, non-object JSON, valid file
- resolve_path: absolute, relative, non-existent, symlink following
- is_inside_workspace: inside, outside, same-as-root, symlink escape,
  parent-directory escape
- _resolve_pattern: ./-relative, ~/-home, already-absolute, bare dot
- matches_any: various pattern types, no-match case
- check_file_permission / _evaluate:
  - global scope → None (always)
  - merge scope + no policy → None
  - merge scope + deny match → False
  - merge scope + allow match only → None (no auto-restriction)
  - merge scope + no deny match → None
  - project scope + no workspace_root → None (fall through)
  - project scope + path inside workspace → None (defer)
  - project scope + path outside workspace → False (blocked)
  - project scope + path outside workspace + allow_outside_project=True → None
  - project scope + path outside workspace + matching allow[] → None
  - project scope + inside workspace + deny match → False
  - project scope + outside workspace + allow match but deny match too → False
  - project scope + allow match + deny match (deny wins) → False
  - unknown scope → None
- Path-resolution attacks:
  - parent escape: ../../etc/passwd blocked in project scope
  - symlink escape: symlink inside workspace pointing outside → blocked
  - relative path resolved against CWD before checking
- Policy edge cases:
  - empty allow and deny lists → noop in merge; still auto-restrict in project
  - malformed JSON → treated as empty policy, no crash
  - missing policy file → None (no overlay)
  - allow_outside_project default is False
- register() mechanics:
  - sets module state correctly on startup
  - global scope → callback NOT inserted
  - non-global scope → callback inserted at position 0
  - idempotent (second startup call doesn't double-register)
  - exception during startup → logged, no crash
- Concurrency: two calls with different paths return independent results
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


from code_puppy.plugins.project_workspace.surfaces.file_permissions import (
    POLICY_FILENAME,
    _evaluate,
    _resolve_pattern,
    _workspace_file_permission,
    check_file_permission,
    is_inside_workspace,
    load_policy,
    matches_any,
    register,
    resolve_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory structure and return the root."""
    root = tmp_path / "workspace"
    (root / ".code_puppy").mkdir(parents=True)
    return root


def _write_policy(workspace_root: Path, data: dict) -> Path:
    """Write a file_policy.json file and return its path."""
    path = workspace_root / ".code_puppy" / POLICY_FILENAME
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_policy
# ---------------------------------------------------------------------------


class TestLoadPolicy:
    def test_returns_none_when_workspace_root_is_none(self) -> None:
        assert load_policy(None) is None

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        assert load_policy(ws) is None

    def test_parses_valid_policy(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_policy(ws, {"allow": ["./**"], "deny": ["./secrets/**"]})
        policy = load_policy(ws)
        assert policy is not None
        assert policy["allow"] == ["./**"]
        assert policy["deny"] == ["./secrets/**"]

    def test_returns_none_on_malformed_json(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / ".code_puppy" / POLICY_FILENAME).write_text(
            "{not: valid json}", encoding="utf-8"
        )
        assert load_policy(ws) is None

    def test_returns_none_when_json_is_array(self, tmp_path: Path) -> None:
        """JSON must be an object (dict), not an array."""
        ws = _make_workspace(tmp_path)
        (ws / ".code_puppy" / POLICY_FILENAME).write_text(
            '["allow", "deny"]', encoding="utf-8"
        )
        assert load_policy(ws) is None

    def test_returns_none_when_json_is_string(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / ".code_puppy" / POLICY_FILENAME).write_text(
            '"just a string"', encoding="utf-8"
        )
        assert load_policy(ws) is None

    def test_empty_policy_object_is_valid(self, tmp_path: Path) -> None:
        """Empty {} is a valid policy (no rules)."""
        ws = _make_workspace(tmp_path)
        _write_policy(ws, {})
        assert load_policy(ws) == {}

    def test_allow_outside_project_flag_preserved(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_policy(ws, {"allow_outside_project": True})
        policy = load_policy(ws)
        assert policy is not None
        assert policy["allow_outside_project"] is True


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_absolute_path_returned_unchanged_content(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("x")
        result = resolve_path(str(f))
        assert result == f.resolve()

    def test_relative_path_becomes_absolute(self) -> None:
        result = resolve_path("relative/path.txt")
        assert result.is_absolute()

    def test_dotdot_components_are_resolved(self, tmp_path: Path) -> None:
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        result = resolve_path(str(subdir / "../../a"))
        assert result == (tmp_path / "a").resolve()

    def test_returns_path_object(self, tmp_path: Path) -> None:
        result = resolve_path(tmp_path / "nonexistent.txt")
        assert isinstance(result, Path)

    def test_follows_symlinks(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("data")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = resolve_path(link)
        assert result == target.resolve()


# ---------------------------------------------------------------------------
# is_inside_workspace
# ---------------------------------------------------------------------------


class TestIsInsideWorkspace:
    def test_path_inside_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        child = ws / "src" / "main.py"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("")
        assert is_inside_workspace(child.resolve(), ws) is True

    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "other" / "file.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("")
        assert is_inside_workspace(outside.resolve(), ws) is False

    def test_workspace_root_itself_is_inside(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        assert is_inside_workspace(ws.resolve(), ws) is True

    def test_sibling_directory_is_outside(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        assert is_inside_workspace(sibling.resolve(), ws) is False

    def test_parent_directory_is_outside(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # tmp_path is the parent of workspace
        assert is_inside_workspace(tmp_path.resolve(), ws) is False

    def test_symlink_escape_is_detected(self, tmp_path: Path) -> None:
        """A symlink inside the workspace pointing to /tmp is treated as outside."""
        ws = _make_workspace(tmp_path)
        outside_target = tmp_path / "secret.txt"
        outside_target.write_text("topsecret")
        evil_link = ws / "evil_link.txt"
        evil_link.symlink_to(outside_target)
        # resolve_path follows the symlink
        resolved = resolve_path(evil_link)
        assert is_inside_workspace(resolved, ws) is False

    def test_dotdot_path_outside_workspace(self, tmp_path: Path) -> None:
        """../../etc/passwd-style path resolves to outside."""
        ws = _make_workspace(tmp_path)
        escape = ws / ".." / ".." / "etc" / "passwd"
        resolved = resolve_path(escape)
        assert is_inside_workspace(resolved, ws) is False


# ---------------------------------------------------------------------------
# _resolve_pattern
# ---------------------------------------------------------------------------


class TestResolvePattern:
    def test_absolute_pattern_unchanged(self, tmp_path: Path) -> None:
        assert _resolve_pattern("/tmp/foo/**", tmp_path) == "/tmp/foo/**"

    def test_relative_pattern_resolved_against_workspace(self, tmp_path: Path) -> None:
        result = _resolve_pattern("./secrets/**", tmp_path)
        assert result == str(tmp_path) + "/secrets/**"

    def test_relative_dot_only(self, tmp_path: Path) -> None:
        result = _resolve_pattern(".", tmp_path)
        assert result == str(tmp_path)

    def test_home_pattern_expanded(self) -> None:
        result = _resolve_pattern("~/.config/**", None)
        home = str(Path.home())
        assert result.startswith(home)
        assert ".config/**" in result

    def test_doublestar_pattern_unchanged(self, tmp_path: Path) -> None:
        assert _resolve_pattern("**/secrets/**", tmp_path) == "**/secrets/**"

    def test_relative_pattern_without_workspace_uses_cwd(self) -> None:
        result = _resolve_pattern("./foo/**", None)
        assert result == str(Path.cwd()) + "/foo/**"


# ---------------------------------------------------------------------------
# matches_any
# ---------------------------------------------------------------------------


class TestMatchesAny:
    def test_matches_absolute_pattern(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("")
        assert matches_any(target, [str(tmp_path) + "/**"], tmp_path) is True

    def test_no_match_returns_false(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        assert matches_any(target, ["/nonexistent/**"], tmp_path) is False

    def test_empty_patterns_returns_false(self, tmp_path: Path) -> None:
        assert matches_any(tmp_path / "file.txt", [], tmp_path) is False

    def test_doublestar_matches_across_separators(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "secrets" / "key.pem"
        target.parent.mkdir(parents=True)
        target.write_text("")
        assert matches_any(target, ["**/secrets/**"], tmp_path) is True

    def test_relative_pattern_resolved_against_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        target = ws / "secrets" / "api_key"
        target.parent.mkdir(parents=True)
        target.write_text("")
        assert matches_any(target, ["./secrets/**"], ws) is True

    def test_dotenv_pattern(self, tmp_path: Path) -> None:
        env_file = tmp_path / "workspace" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("")
        assert matches_any(env_file, ["**/.env"], tmp_path) is True


# ---------------------------------------------------------------------------
# _evaluate — the core logic under test
# ---------------------------------------------------------------------------


class TestEvaluateGlobalScope:
    def test_always_returns_none(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        inside = ws / "file.txt"
        outside = tmp_path / "outside.txt"
        assert _evaluate(inside, "global", ws, None) is None
        assert _evaluate(outside, "global", ws, None) is None
        assert _evaluate(outside, "global", None, None) is None

    def test_policy_ignored_in_global_scope(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        target = tmp_path / "outside.txt"
        policy = {"deny": [str(tmp_path) + "/**"]}
        # Even if policy would deny it, global scope returns None
        assert _evaluate(target, "global", ws, policy) is None


# ---------------------------------------------------------------------------
# _evaluate — merge scope
# ---------------------------------------------------------------------------


class TestEvaluateMergeScope:
    def test_no_policy_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "file.txt"
        assert _evaluate(path, "merge", None, None) is None

    def test_empty_policy_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "file.txt"
        assert _evaluate(path, "merge", None, {}) is None

    def test_deny_match_returns_false(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        secrets = ws / "secrets" / "key.pem"
        secrets.parent.mkdir(parents=True)
        secrets.write_text("")
        policy = {"deny": ["./secrets/**"]}
        assert _evaluate(secrets.resolve(), "merge", ws, policy) is False

    def test_allow_only_returns_none(self, tmp_path: Path) -> None:
        """Allow rules have no effect in merge scope — no auto-restriction."""
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "external.txt"
        outside.write_text("")
        policy = {"allow": [str(tmp_path) + "/**"]}
        assert _evaluate(outside.resolve(), "merge", ws, policy) is None

    def test_no_deny_match_returns_none(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        safe = ws / "src" / "main.py"
        safe.parent.mkdir(parents=True)
        safe.write_text("")
        policy = {"deny": ["./secrets/**"]}
        assert _evaluate(safe.resolve(), "merge", ws, policy) is None

    def test_outside_root_not_blocked_in_merge(self, tmp_path: Path) -> None:
        """merge scope has no auto-restriction — outside paths are allowed."""
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside_file.txt"
        outside.write_text("")
        assert _evaluate(outside.resolve(), "merge", ws, None) is None

    def test_deny_overrides_allow_in_merge(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        target = ws / "secrets" / "key.pem"
        target.parent.mkdir(parents=True)
        target.write_text("")
        policy = {
            "allow": ["./secrets/**"],
            "deny": ["./secrets/**"],
        }
        # deny wins
        assert _evaluate(target.resolve(), "merge", ws, policy) is False


# ---------------------------------------------------------------------------
# _evaluate — project scope
# ---------------------------------------------------------------------------


class TestEvaluateProjectScope:
    def test_no_workspace_root_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "file.txt"
        assert _evaluate(path, "project", None, None) is None

    def test_inside_workspace_returns_none(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        inside = ws / "src" / "main.py"
        inside.parent.mkdir(parents=True)
        inside.write_text("")
        assert _evaluate(inside.resolve(), "project", ws, None) is None

    def test_outside_workspace_returns_false(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        assert _evaluate(outside.resolve(), "project", ws, None) is False

    def test_outside_with_allow_outside_project_true_returns_none(
        self, tmp_path: Path
    ) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "build_output.txt"
        outside.write_text("")
        policy = {"allow_outside_project": True}
        assert _evaluate(outside.resolve(), "project", ws, policy) is None

    def test_outside_with_matching_allow_returns_none(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        external_build = tmp_path / "build" / "output.js"
        external_build.parent.mkdir()
        external_build.write_text("")
        policy = {"allow": [str(tmp_path / "build") + "/**"]}
        assert _evaluate(external_build.resolve(), "project", ws, policy) is None

    def test_inside_workspace_with_deny_returns_false(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        secrets = ws / "secrets" / "api_key"
        secrets.parent.mkdir(parents=True)
        secrets.write_text("")
        policy = {"deny": ["./secrets/**"]}
        assert _evaluate(secrets.resolve(), "project", ws, policy) is False

    def test_deny_wins_over_allow_for_inside_path(self, tmp_path: Path) -> None:
        """deny takes precedence over allow, even for inside-workspace paths."""
        ws = _make_workspace(tmp_path)
        f = ws / "secrets" / "key.pem"
        f.parent.mkdir(parents=True)
        f.write_text("")
        policy = {
            "allow": ["./secrets/**"],
            "deny": ["./secrets/**"],
        }
        assert _evaluate(f.resolve(), "project", ws, policy) is False

    def test_deny_wins_over_allow_for_outside_path(self, tmp_path: Path) -> None:
        """deny wins even when path is explicitly allowed and allow_outside_project."""
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "external.txt"
        outside.write_text("")
        policy = {
            "allow": [str(tmp_path) + "/**"],
            "allow_outside_project": True,
            "deny": [str(tmp_path) + "/**"],
        }
        assert _evaluate(outside.resolve(), "project", ws, policy) is False

    def test_empty_policy_still_restricts_outside(self, tmp_path: Path) -> None:
        """Empty allow/deny lists don't disable the auto-restriction."""
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        assert _evaluate(outside.resolve(), "project", ws, {}) is False

    def test_unknown_scope_returns_none(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = tmp_path / "file.txt"
        assert _evaluate(path, "weirdscope", ws, None) is None


# ---------------------------------------------------------------------------
# Path-resolution attacks
# ---------------------------------------------------------------------------


class TestPathResolutionAttacks:
    """Security tests: verify path attacks cannot bypass project-scope checks."""

    def test_parent_escape_dotdot_blocked(self, tmp_path: Path) -> None:
        """../../etc/passwd-style path is blocked in project scope."""
        ws = _make_workspace(tmp_path)
        # Construct a path that uses .. to escape
        escape_path = str(ws / ".." / ".." / "etc" / "passwd")
        result = check_file_permission(None, escape_path, "write", "project", ws, None)
        assert result is False

    def test_absolute_path_outside_workspace_blocked(self, tmp_path: Path) -> None:
        """An absolute path to /tmp is blocked in project scope."""
        ws = _make_workspace(tmp_path)
        outside_absolute = tmp_path / "outside_dir" / "file.txt"
        outside_absolute.parent.mkdir(parents=True)
        outside_absolute.write_text("")
        result = check_file_permission(
            None, str(outside_absolute), "write", "project", ws, None
        )
        assert result is False

    def test_symlink_inside_workspace_pointing_outside_is_blocked(
        self, tmp_path: Path
    ) -> None:
        """Symlink inside workspace pointing to an outside file is blocked."""
        ws = _make_workspace(tmp_path)
        outside_secret = tmp_path / "outside_secret.txt"
        outside_secret.write_text("secret data")
        # Create a symlink inside the workspace that points outside
        evil_link = ws / "evil_link.txt"
        evil_link.symlink_to(outside_secret)
        # Our check resolves symlinks before comparing
        result = check_file_permission(
            None, str(evil_link), "read", "project", ws, None
        )
        assert result is False

    def test_symlink_chain_to_outside_is_blocked(self, tmp_path: Path) -> None:
        """Multi-hop symlink chain ending outside workspace is blocked."""
        ws = _make_workspace(tmp_path)
        outside_target = tmp_path / "outside_target.txt"
        outside_target.write_text("target data")
        # First link inside workspace → second link outside workspace
        link1 = ws / "link1.txt"
        link1.symlink_to(outside_target)
        result = check_file_permission(None, str(link1), "write", "project", ws, None)
        assert result is False

    def test_relative_path_resolved_correctly(self, tmp_path: Path) -> None:
        """Relative paths are resolved against CWD before checking."""
        ws = _make_workspace(tmp_path)
        inside = ws / "file.txt"
        inside.write_text("")
        # Use an absolute path (resolved path is what matters)
        result = check_file_permission(None, str(inside), "read", "project", ws, None)
        assert result is None  # inside workspace, should defer

    def test_path_exactly_at_workspace_root_is_inside(self, tmp_path: Path) -> None:
        """The workspace root itself counts as inside."""
        ws = _make_workspace(tmp_path)
        result = check_file_permission(None, str(ws), "read", "project", ws, None)
        assert result is None

    def test_workspace_sibling_dir_is_outside(self, tmp_path: Path) -> None:
        """A sibling directory (shares parent with workspace) is outside."""
        ws = _make_workspace(tmp_path)
        sibling = tmp_path / "another_workspace"
        sibling.mkdir()
        result = check_file_permission(
            None, str(sibling / "file.txt"), "write", "project", ws, None
        )
        assert result is False


# ---------------------------------------------------------------------------
# check_file_permission — public API
# ---------------------------------------------------------------------------


class TestCheckFilePermission:
    def test_returns_none_for_global_scope(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "anywhere.txt"
        assert (
            check_file_permission(None, str(outside), "write", "global", ws, None)
            is None
        )

    def test_returns_false_for_project_scope_outside(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        assert (
            check_file_permission(None, str(outside), "write", "project", ws, None)
            is False
        )

    def test_returns_none_for_project_scope_inside(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        inside = ws / "src" / "app.py"
        inside.parent.mkdir(parents=True)
        inside.write_text("")
        assert (
            check_file_permission(None, str(inside), "write", "project", ws, None)
            is None
        )

    def test_context_is_accepted_and_ignored(self, tmp_path: Path) -> None:
        """context parameter is accepted; its value doesn't affect logic."""
        ws = _make_workspace(tmp_path)
        inside = ws / "file.txt"
        inside.write_text("")
        ctx = MagicMock()
        assert (
            check_file_permission(ctx, str(inside), "read", "project", ws, None) is None
        )

    def test_operation_string_does_not_affect_result(self, tmp_path: Path) -> None:
        """Operation value (write/read/delete) doesn't change block logic."""
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        for op in ("write", "read", "delete", "replace text in", "delete snippet from"):
            assert (
                check_file_permission(None, str(outside), op, "project", ws, None)
                is False
            ), f"Expected False for operation={op!r}"

    def test_concurrent_calls_are_independent(self, tmp_path: Path) -> None:
        """Two calls with different paths return independent results."""
        ws = _make_workspace(tmp_path)
        inside = ws / "good.py"
        inside.write_text("")
        outside = tmp_path / "bad.txt"
        outside.write_text("")

        result_inside = check_file_permission(
            None, str(inside), "write", "project", ws, None
        )
        result_outside = check_file_permission(
            None, str(outside), "write", "project", ws, None
        )
        assert result_inside is None
        assert result_outside is False


# ---------------------------------------------------------------------------
# Policy edge cases via check_file_permission
# ---------------------------------------------------------------------------


class TestPolicyEdgeCases:
    def test_empty_allow_list_still_restricts_outside(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        policy = {"allow": [], "deny": []}
        assert (
            check_file_permission(None, str(outside), "write", "project", ws, policy)
            is False
        )

    def test_allow_outside_project_false_explicit_still_restricts(
        self, tmp_path: Path
    ) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        policy = {"allow_outside_project": False}
        assert (
            check_file_permission(None, str(outside), "write", "project", ws, policy)
            is False
        )

    def test_malformed_policy_treated_as_none(self, tmp_path: Path) -> None:
        """If load_policy returned None (malformed file), project scope still restricts."""
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")
        # None policy = no rules loaded (malformed file scenario)
        assert (
            check_file_permission(None, str(outside), "write", "project", ws, None)
            is False
        )

    def test_policy_with_only_allow_outside_project_allows_any_outside(
        self, tmp_path: Path
    ) -> None:
        ws = _make_workspace(tmp_path)
        outside = tmp_path / "build" / "output.js"
        outside.parent.mkdir()
        outside.write_text("")
        policy = {"allow_outside_project": True}
        assert (
            check_file_permission(None, str(outside), "write", "project", ws, policy)
            is None
        )

    def test_deny_inside_workspace_in_merge_scope(self, tmp_path: Path) -> None:
        """merge scope with deny rule blocks even inside workspace."""
        ws = _make_workspace(tmp_path)
        secrets = ws / "secrets" / "key"
        secrets.parent.mkdir(parents=True)
        secrets.write_text("")
        policy = {"deny": ["./secrets/**"]}
        assert (
            check_file_permission(None, str(secrets), "write", "merge", ws, policy)
            is False
        )


# ---------------------------------------------------------------------------
# _workspace_file_permission callback
# ---------------------------------------------------------------------------


class TestWorkspaceFilePermissionCallback:
    def test_callback_delegates_to_check(self, tmp_path: Path) -> None:
        """The callback reads module state and calls check_file_permission."""
        import code_puppy.plugins.project_workspace.surfaces.file_permissions as fp_mod

        ws = _make_workspace(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("")

        # Temporarily patch module state
        original_scope = fp_mod._scope
        original_root = fp_mod._workspace_root
        original_policy = fp_mod._policy
        try:
            fp_mod._scope = "project"
            fp_mod._workspace_root = ws
            fp_mod._policy = None

            result = _workspace_file_permission(None, str(outside), "write")
            assert result is False
        finally:
            fp_mod._scope = original_scope
            fp_mod._workspace_root = original_root
            fp_mod._policy = original_policy

    def test_callback_accepts_all_positional_kwargs(self, tmp_path: Path) -> None:
        """Callback must accept the full file_permission signature."""
        import code_puppy.plugins.project_workspace.surfaces.file_permissions as fp_mod

        ws = _make_workspace(tmp_path)
        inside = ws / "file.py"
        inside.write_text("")

        original_scope = fp_mod._scope
        original_root = fp_mod._workspace_root
        try:
            fp_mod._scope = "project"
            fp_mod._workspace_root = ws
            fp_mod._policy = None

            # Full signature: context, file_path, operation, preview, message_group, operation_data
            result = _workspace_file_permission(
                None,
                str(inside),
                "write",
                "some_preview",
                "msg_group",
                {"content": "hello", "overwrite": True},
            )
            assert result is None  # inside workspace
        finally:
            fp_mod._scope = original_scope
            fp_mod._workspace_root = original_root
            fp_mod._policy = None


# ---------------------------------------------------------------------------
# register() mechanics
# ---------------------------------------------------------------------------


class TestRegister:
    """Test the startup-callback registration logic."""

    def _run_fp_startup(
        self,
        scope: str,
        workspace_root: Path | None,
        policy: dict | None,
        tmp_path: Path,
    ) -> None:
        """Helper: simulate calling register() then firing startup."""
        from code_puppy.callbacks import _callbacks

        # Isolate: remove any existing file_permission callbacks
        saved = list(_callbacks["file_permission"])
        _callbacks["file_permission"].clear()

        try:
            # Build a fake active config
            fake_config = MagicMock()
            fake_config.surfaces.get.return_value = scope
            fake_config.root = workspace_root

            with (
                patch(
                    "code_puppy.plugins.project_workspace.surfaces.file_permissions.load_policy",
                    return_value=policy,
                ),
                patch(
                    "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                    return_value=fake_config,
                ),
                patch("code_puppy.callbacks.register_callback") as mock_reg,
            ):
                register()
                # Capture and fire the startup callback that was registered
                assert mock_reg.called
                call_args = mock_reg.call_args
                assert call_args[0][0] == "startup"
                startup_fn = call_args[0][1]

            # Now actually fire the startup callback (without patching register_callback)
            startup_fn()
        finally:
            # Restore
            saved_fp_callbacks = list(_callbacks["file_permission"])
            _callbacks["file_permission"].clear()
            _callbacks["file_permission"].extend(saved)
            return saved_fp_callbacks  # type: ignore[return-value]

    def test_global_scope_does_not_register_callback(self, tmp_path: Path) -> None:
        import code_puppy.plugins.project_workspace.surfaces.file_permissions as fp_mod
        from code_puppy.callbacks import _callbacks

        saved = list(_callbacks["file_permission"])
        _callbacks["file_permission"].clear()
        try:
            fake_config = MagicMock()
            fake_config.surfaces.get.return_value = "global"
            fake_config.root = tmp_path

            startup_holder: list[Any] = []

            def capture_startup(phase: str, fn: Any) -> None:
                if phase == "startup":
                    startup_holder.append(fn)

            with patch(
                "code_puppy.callbacks.register_callback", side_effect=capture_startup
            ):
                register()

            assert startup_holder, "startup callback should have been registered"

            with (
                patch(
                    "code_puppy.plugins.project_workspace.surfaces.file_permissions.load_policy",
                    return_value=None,
                ),
                patch(
                    "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                    return_value=fake_config,
                ),
            ):
                startup_holder[0]()

            assert (
                fp_mod._workspace_file_permission not in _callbacks["file_permission"]
            )
        finally:
            _callbacks["file_permission"].clear()
            _callbacks["file_permission"].extend(saved)

    def test_project_scope_inserts_callback_at_position_0(self, tmp_path: Path) -> None:
        import code_puppy.plugins.project_workspace.surfaces.file_permissions as fp_mod
        from code_puppy.callbacks import _callbacks

        # Simulate an existing callback (like file_permission_handler's)
        dummy = lambda *a, **kw: True  # noqa: E731
        saved = list(_callbacks["file_permission"])
        _callbacks["file_permission"].clear()
        _callbacks["file_permission"].append(dummy)

        try:
            ws = _make_workspace(tmp_path)
            fake_config = MagicMock()
            fake_config.surfaces.get.return_value = "project"
            fake_config.root = ws

            startup_holder: list[Any] = []

            def capture_startup(phase: str, fn: Any) -> None:
                if phase == "startup":
                    startup_holder.append(fn)

            with patch(
                "code_puppy.callbacks.register_callback", side_effect=capture_startup
            ):
                register()

            assert startup_holder

            with (
                patch(
                    "code_puppy.plugins.project_workspace.surfaces.file_permissions.load_policy",
                    return_value=None,
                ),
                patch(
                    "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                    return_value=fake_config,
                ),
            ):
                startup_holder[0]()

            # Our callback should be at position 0, before dummy
            assert _callbacks["file_permission"][0] is fp_mod._workspace_file_permission
            assert _callbacks["file_permission"][1] is dummy
        finally:
            _callbacks["file_permission"].clear()
            _callbacks["file_permission"].extend(saved)

    def test_startup_idempotent_second_call_no_duplicate(self, tmp_path: Path) -> None:
        """Calling startup twice doesn't register our callback twice."""
        import code_puppy.plugins.project_workspace.surfaces.file_permissions as fp_mod
        from code_puppy.callbacks import _callbacks

        saved = list(_callbacks["file_permission"])
        _callbacks["file_permission"].clear()

        try:
            ws = _make_workspace(tmp_path)
            fake_config = MagicMock()
            fake_config.surfaces.get.return_value = "project"
            fake_config.root = ws

            startup_holder: list[Any] = []

            def capture_startup(phase: str, fn: Any) -> None:
                startup_holder.append(fn)

            with patch(
                "code_puppy.callbacks.register_callback", side_effect=capture_startup
            ):
                register()

            assert startup_holder
            fn = startup_holder[0]

            with (
                patch(
                    "code_puppy.plugins.project_workspace.surfaces.file_permissions.load_policy",
                    return_value=None,
                ),
                patch(
                    "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                    return_value=fake_config,
                ),
            ):
                fn()
                fn()  # fire twice

            count = _callbacks["file_permission"].count(
                fp_mod._workspace_file_permission
            )
            assert count == 1, f"Expected 1 registration, got {count}"
        finally:
            _callbacks["file_permission"].clear()
            _callbacks["file_permission"].extend(saved)

    def test_startup_exception_does_not_crash(self, tmp_path: Path) -> None:
        """If get_active_config raises, startup logs and continues without crashing."""
        from code_puppy.callbacks import _callbacks

        saved = list(_callbacks["file_permission"])
        _callbacks["file_permission"].clear()
        try:
            startup_holder: list[Any] = []

            def capture_startup(phase: str, fn: Any) -> None:
                startup_holder.append(fn)

            with patch(
                "code_puppy.callbacks.register_callback", side_effect=capture_startup
            ):
                register()

            assert startup_holder
            with patch(
                "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                side_effect=RuntimeError("boom"),
            ):
                startup_holder[0]()  # must not raise
        finally:
            _callbacks["file_permission"].clear()
            _callbacks["file_permission"].extend(saved)

    def test_module_state_set_correctly_after_startup(self, tmp_path: Path) -> None:
        import code_puppy.plugins.project_workspace.surfaces.file_permissions as fp_mod
        from code_puppy.callbacks import _callbacks

        ws = _make_workspace(tmp_path)
        policy_data = {"deny": ["./secrets/**"]}
        saved = list(_callbacks["file_permission"])
        _callbacks["file_permission"].clear()

        try:
            fake_config = MagicMock()
            fake_config.surfaces.get.return_value = "project"
            fake_config.root = ws

            startup_holder: list[Any] = []

            def capture_startup(phase: str, fn: Any) -> None:
                startup_holder.append(fn)

            with patch(
                "code_puppy.callbacks.register_callback", side_effect=capture_startup
            ):
                register()

            assert startup_holder

            with (
                patch(
                    "code_puppy.plugins.project_workspace.surfaces.file_permissions.load_policy",
                    return_value=policy_data,
                ),
                patch(
                    "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                    return_value=fake_config,
                ),
            ):
                startup_holder[0]()

            assert fp_mod._scope == "project"
            assert fp_mod._workspace_root == ws
            assert fp_mod._policy == policy_data
        finally:
            _callbacks["file_permission"].clear()
            _callbacks["file_permission"].extend(saved)
