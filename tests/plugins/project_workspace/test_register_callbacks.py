"""Tests for code_puppy.plugins.project_workspace.register_callbacks.

Covers:
- startup() runs without exception when no .code_puppy/ exists
- startup() runs without exception when config.json is malformed
- get_active_config() returns the resolved WorkspaceConfig after startup
- get_active_config() returns a safe merge default before startup fires
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch


from code_puppy.plugins.project_workspace.config import WorkspaceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_register_callbacks():
    """Re-import register_callbacks with a clean _active_config state.

    We need this because _active_config is a module-level singleton that
    persists across test runs within the same process.
    """
    mod_name = "code_puppy.plugins.project_workspace.register_callbacks"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


# ---------------------------------------------------------------------------
# get_active_config() before startup
# ---------------------------------------------------------------------------


class TestGetActiveConfigBeforeStartup:
    def test_returns_merge_defaults_before_startup(self) -> None:
        """get_active_config() must not raise even if _startup() hasn't fired."""
        mod = _reload_register_callbacks()
        # Force _active_config to None to simulate pre-startup state
        mod._active_config = None

        cfg = mod.get_active_config()
        assert isinstance(cfg, WorkspaceConfig)
        assert cfg.profile == "merge"
        assert cfg.root is None


# ---------------------------------------------------------------------------
# _startup() happy path
# ---------------------------------------------------------------------------


class TestStartupHappyPath:
    def test_startup_with_no_workspace(self, tmp_path: Path) -> None:
        """startup() runs cleanly when discover_root returns None."""
        mod = _reload_register_callbacks()

        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.discover_root",
            return_value=None,
        ):
            mod._startup()

        cfg = mod.get_active_config()
        assert isinstance(cfg, WorkspaceConfig)
        assert cfg.profile == "merge"
        assert cfg.root is None

    def test_startup_with_valid_workspace(self, tmp_path: Path) -> None:
        """startup() loads the config when a workspace root is found."""
        dot_dir = tmp_path / ".code_puppy"
        dot_dir.mkdir()
        (dot_dir / "config.json").write_text(
            json.dumps({"profile": "strict-local"}), encoding="utf-8"
        )

        mod = _reload_register_callbacks()

        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.discover_root",
            return_value=tmp_path,
        ):
            mod._startup()

        cfg = mod.get_active_config()
        assert isinstance(cfg, WorkspaceConfig)
        assert cfg.profile == "strict-local"
        assert cfg.root == tmp_path
        assert all(v == "project" for v in cfg.surfaces.values())


# ---------------------------------------------------------------------------
# _startup() error handling
# ---------------------------------------------------------------------------


class TestStartupErrorHandling:
    def test_startup_with_malformed_config_does_not_raise(self, tmp_path: Path) -> None:
        """startup() falls back to merge when config.json is garbage."""
        dot_dir = tmp_path / ".code_puppy"
        dot_dir.mkdir()
        (dot_dir / "config.json").write_text("THIS IS NOT JSON", encoding="utf-8")

        mod = _reload_register_callbacks()

        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.discover_root",
            return_value=tmp_path,
        ):
            mod._startup()  # must not raise

        cfg = mod.get_active_config()
        assert cfg.profile == "merge"

    def test_startup_with_no_code_puppy_dir_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """startup() is fine when the workspace root has no .code_puppy/."""
        mod = _reload_register_callbacks()

        # tmp_path has no .code_puppy/ subdir — discover_root returns None
        with patch(
            "code_puppy.plugins.project_workspace.register_callbacks.discover_root",
            return_value=None,
        ):
            mod._startup()  # must not raise

        cfg = mod.get_active_config()
        assert isinstance(cfg, WorkspaceConfig)

    def test_get_active_config_always_returns_workspace_config(self) -> None:
        """get_active_config() never returns None — always a WorkspaceConfig."""
        mod = _reload_register_callbacks()
        mod._active_config = None

        result = mod.get_active_config()
        assert isinstance(result, WorkspaceConfig)
