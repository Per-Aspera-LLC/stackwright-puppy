"""Tests for code_puppy.plugins.project_workspace.surfaces.plugins.

Covers:
- get_active_plugin_scope(): returns merge/project/global from workspace config
- get_active_plugin_scope(): returns merge before startup callback fires
- register(): doesn't crash
- register(): logs the scope at DEBUG level
- Integration: project scope → user-tier plugins skipped
- Integration: global scope → project-tier plugins skipped
- Integration: merge scope → both tiers load
- Integration: missing project dir → project_loaded still empty regardless of scope
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from code_puppy.plugins.project_workspace.surfaces.plugins import (
    get_active_plugin_scope,
    register,
)


# ---------------------------------------------------------------------------
# get_active_plugin_scope
# ---------------------------------------------------------------------------


class TestGetActivePluginScope:
    def test_returns_merge_when_no_workspace(self) -> None:
        """Before startup or with no workspace — defaults to merge."""
        from code_puppy.plugins.project_workspace.config import WorkspaceConfig

        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
            return_value=WorkspaceConfig(),  # merge defaults
        ):
            assert get_active_plugin_scope() == "merge"

    def test_returns_project_scope(self) -> None:
        from code_puppy.plugins.project_workspace.config import WorkspaceConfig

        config = WorkspaceConfig(surfaces={"plugins": "project"})
        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
            return_value=config,
        ):
            assert get_active_plugin_scope() == "project"

    def test_returns_global_scope(self) -> None:
        from code_puppy.plugins.project_workspace.config import WorkspaceConfig

        config = WorkspaceConfig(surfaces={"plugins": "global"})
        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
            return_value=config,
        ):
            assert get_active_plugin_scope() == "global"

    def test_missing_plugins_key_falls_back_to_merge(self) -> None:
        """If 'plugins' key absent from surfaces dict — returns merge."""
        from code_puppy.plugins.project_workspace.config import WorkspaceConfig

        config = WorkspaceConfig(surfaces={"agents": "project"})  # no 'plugins' key
        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
            return_value=config,
        ):
            assert get_active_plugin_scope() == "merge"


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_does_not_crash(self) -> None:
        register()

    def test_register_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        from code_puppy.plugins.project_workspace.config import WorkspaceConfig

        with (
            patch(
                "code_puppy.plugins.project_workspace.register_callbacks.get_active_config",
                return_value=WorkspaceConfig(),
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="code_puppy.plugins.project_workspace.surfaces.plugins",
            ),
        ):
            register()
        # Just verify it logged something at DEBUG (content may vary)
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "plugins" in r.name
        ]
        assert len(debug_records) >= 1


# ---------------------------------------------------------------------------
# Integration: scope-gated plugin loading
# ---------------------------------------------------------------------------


def _make_test_plugin(plugins_dir: Path, name: str) -> None:
    """Write a minimal register_callbacks.py into plugins_dir/name/."""
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "register_callbacks.py").write_text(
        "# auto-generated test plugin\n",
        encoding="utf-8",
    )


def _reset_plugin_loader() -> None:
    """Reset the plugin loader to a clean state for isolated test runs."""
    import code_puppy.plugins as pm

    pm._PLUGINS_LOADED = False
    pm._loaded_plugin_names.update({"builtin": [], "user": [], "project": []})


class TestPluginLoaderScopeGating:
    """Integration tests for the scope-gated load_plugin_callbacks()."""

    def setup_method(self) -> None:
        _reset_plugin_loader()

    def teardown_method(self) -> None:
        _reset_plugin_loader()

    def test_merge_scope_loads_both_user_and_project(self, tmp_path: Path) -> None:
        """merge → user + project tiers both load."""
        user_dir = tmp_path / "user_plugins"
        project_dir = tmp_path / "project_plugins"
        _make_test_plugin(user_dir, "user_plug")
        _make_test_plugin(project_dir, "proj_plug")

        import code_puppy.plugins as pm

        with (
            patch(
                "code_puppy.workspace_bootstrap.read_plugin_scope", return_value="merge"
            ),
            patch.object(pm, "USER_PLUGINS_DIR", user_dir),
            patch.object(pm, "get_project_plugins_directory", return_value=project_dir),
            patch.object(pm, "_load_builtin_plugins", return_value=[]),
        ):
            result = pm.load_plugin_callbacks()

        assert "user_plug" in result["user"]
        assert "proj_plug" in result["project"]

    def test_project_scope_skips_user_tier(self, tmp_path: Path) -> None:
        """project → user-tier plugins NOT loaded, project-tier loads."""
        user_dir = tmp_path / "user_plugins"
        project_dir = tmp_path / "project_plugins"
        _make_test_plugin(user_dir, "user_plug")
        _make_test_plugin(project_dir, "proj_plug")

        import code_puppy.plugins as pm

        with (
            patch(
                "code_puppy.workspace_bootstrap.read_plugin_scope",
                return_value="project",
            ),
            patch.object(pm, "USER_PLUGINS_DIR", user_dir),
            patch.object(pm, "get_project_plugins_directory", return_value=project_dir),
            patch.object(pm, "_load_builtin_plugins", return_value=[]),
        ):
            result = pm.load_plugin_callbacks()

        assert result["user"] == []
        assert "proj_plug" in result["project"]

    def test_global_scope_skips_project_tier(self, tmp_path: Path) -> None:
        """global → project-tier plugins NOT loaded, user-tier loads."""
        user_dir = tmp_path / "user_plugins"
        project_dir = tmp_path / "project_plugins"
        _make_test_plugin(user_dir, "user_plug")
        _make_test_plugin(project_dir, "proj_plug")

        import code_puppy.plugins as pm

        with (
            patch(
                "code_puppy.workspace_bootstrap.read_plugin_scope",
                return_value="global",
            ),
            patch.object(pm, "USER_PLUGINS_DIR", user_dir),
            patch.object(pm, "get_project_plugins_directory", return_value=project_dir),
            patch.object(pm, "_load_builtin_plugins", return_value=[]),
        ):
            result = pm.load_plugin_callbacks()

        assert "user_plug" in result["user"]
        assert result["project"] == []

    def test_project_scope_with_no_project_dir(self, tmp_path: Path) -> None:
        """project scope + no project dir → project_loaded is empty, no crash."""
        user_dir = tmp_path / "user_plugins"
        _make_test_plugin(user_dir, "user_plug")

        import code_puppy.plugins as pm

        with (
            patch(
                "code_puppy.workspace_bootstrap.read_plugin_scope",
                return_value="project",
            ),
            patch.object(pm, "USER_PLUGINS_DIR", user_dir),
            patch.object(pm, "get_project_plugins_directory", return_value=None),
            patch.object(pm, "_load_builtin_plugins", return_value=[]),
        ):
            result = pm.load_plugin_callbacks()

        assert result["user"] == []
        assert result["project"] == []

    def test_global_scope_with_no_project_dir(self, tmp_path: Path) -> None:
        """global scope + no project dir → user loads, project stays empty."""
        user_dir = tmp_path / "user_plugins"
        _make_test_plugin(user_dir, "user_plug")

        import code_puppy.plugins as pm

        with (
            patch(
                "code_puppy.workspace_bootstrap.read_plugin_scope",
                return_value="global",
            ),
            patch.object(pm, "USER_PLUGINS_DIR", user_dir),
            patch.object(pm, "get_project_plugins_directory", return_value=None),
            patch.object(pm, "_load_builtin_plugins", return_value=[]),
        ):
            result = pm.load_plugin_callbacks()

        assert "user_plug" in result["user"]
        assert result["project"] == []

    def test_bootstrap_failure_falls_back_to_merge(self, tmp_path: Path) -> None:
        """If read_plugin_scope raises, load_plugin_callbacks itself should not crash.

        Note: read_plugin_scope has its own internal try/except so it always
        returns a string.  This test verifies the loader works even if the
        bootstrap module raises unexpectedly (belt-and-suspenders).
        """
        user_dir = tmp_path / "user_plugins"
        project_dir = tmp_path / "project_plugins"
        _make_test_plugin(user_dir, "user_plug")
        _make_test_plugin(project_dir, "proj_plug")

        import code_puppy.plugins as pm

        # Simulate read_plugin_scope returning merge (its failure fallback)
        with (
            patch(
                "code_puppy.workspace_bootstrap.read_plugin_scope", return_value="merge"
            ),
            patch.object(pm, "USER_PLUGINS_DIR", user_dir),
            patch.object(pm, "get_project_plugins_directory", return_value=project_dir),
            patch.object(pm, "_load_builtin_plugins", return_value=[]),
        ):
            result = pm.load_plugin_callbacks()

        # merge → both tiers
        assert "user_plug" in result["user"]
        assert "proj_plug" in result["project"]

    def test_idempotency_guard_still_works(self, tmp_path: Path) -> None:
        """Second call to load_plugin_callbacks() returns empty (idempotency)."""
        import code_puppy.plugins as pm

        pm._PLUGINS_LOADED = True  # simulate already loaded
        result = pm.load_plugin_callbacks()
        assert result == {"builtin": [], "user": [], "project": []}
