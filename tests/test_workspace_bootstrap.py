"""Tests for code_puppy.workspace_bootstrap — pre-plugin scope reader.

Exercises:
- _find_config: no .code_puppy/, config found in cwd, found in parent,
  .git/ boundary stops walk, .code_puppy/ + .git/ co-exist, missing config.json,
  found 3 levels up with .git/ co-located
- read_plugin_scope: no config → merge, all valid profiles, overrides.plugins,
  malformed JSON → merge + warning, non-dict JSON, unknown profile → merge + warning,
  invalid override scope → keeps profile default + warning, walk stops at .git/
  boundary, default cwd (None), custom profile
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from code_puppy.workspace_bootstrap import _find_config, read_plugin_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(directory: Path, data: dict) -> Path:
    """Write .code_puppy/config.json under *directory* and add a .git/ marker."""
    cp = directory / ".code_puppy"
    cp.mkdir(exist_ok=True)
    cfg = cp / "config.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    (directory / ".git").mkdir(exist_ok=True)
    return cfg


# ---------------------------------------------------------------------------
# _find_config
# ---------------------------------------------------------------------------


class TestFindConfig:
    def test_returns_none_when_no_code_puppy(self, tmp_path: Path) -> None:
        """No .code_puppy/ at or above start — returns None (stops at .git/)."""
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        assert _find_config(subdir) is None

    def test_finds_config_in_cwd(self, tmp_path: Path) -> None:
        """Config found directly in the starting directory."""
        cp = tmp_path / ".code_puppy"
        cp.mkdir()
        cfg = cp / "config.json"
        cfg.write_text("{}")
        (tmp_path / ".git").mkdir()
        assert _find_config(tmp_path) == cfg.resolve()

    def test_finds_config_in_parent(self, tmp_path: Path) -> None:
        """Config found one level up from starting directory."""
        cp = tmp_path / ".code_puppy"
        cp.mkdir()
        cfg = cp / "config.json"
        cfg.write_text("{}")
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "sub"
        subdir.mkdir()
        assert _find_config(subdir) == cfg.resolve()

    def test_stops_at_git_boundary_before_code_puppy(self, tmp_path: Path) -> None:
        """.git/ found before .code_puppy/ → walk stops, returns None."""
        (tmp_path / ".git").mkdir()
        # .code_puppy/ is ABOVE the .git/ boundary
        parent = tmp_path.parent
        cp = parent / ".code_puppy"
        if not cp.exists():
            # Only valid when parent doesn't already have .code_puppy/
            # and is not root; skip if we're at a filesystem edge.
            pytest.skip("parent already has .code_puppy or is root")
        subdir = tmp_path / "child"
        subdir.mkdir()
        assert _find_config(subdir) is None

    def test_code_puppy_and_git_same_dir_code_puppy_wins(self, tmp_path: Path) -> None:
        """When .code_puppy/ and .git/ co-exist, config is still returned."""
        (tmp_path / ".git").mkdir()
        cp = tmp_path / ".code_puppy"
        cp.mkdir()
        cfg = cp / "config.json"
        cfg.write_text("{}")
        assert _find_config(tmp_path) == cfg.resolve()

    def test_code_puppy_without_config_json_returns_none(self, tmp_path: Path) -> None:
        """.code_puppy/ exists but config.json is absent → None."""
        (tmp_path / ".code_puppy").mkdir()
        (tmp_path / ".git").mkdir()
        assert _find_config(tmp_path) is None

    def test_finds_config_three_levels_up_with_git(self, tmp_path: Path) -> None:
        """Walk finds .code_puppy/config.json three directories above cwd."""
        (tmp_path / ".git").mkdir()
        cp = tmp_path / ".code_puppy"
        cp.mkdir()
        cfg = cp / "config.json"
        cfg.write_text("{}")
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert _find_config(deep) == cfg.resolve()


# ---------------------------------------------------------------------------
# read_plugin_scope
# ---------------------------------------------------------------------------


class TestReadPluginScope:
    def test_no_config_returns_merge(self, tmp_path: Path) -> None:
        """No .code_puppy/ anywhere → default merge."""
        (tmp_path / ".git").mkdir()
        assert read_plugin_scope(tmp_path) == "merge"

    def test_empty_config_returns_merge(self, tmp_path: Path) -> None:
        """Empty JSON object → profile defaults to merge."""
        _write_config(tmp_path, {})
        assert read_plugin_scope(tmp_path) == "merge"

    def test_strict_local_profile_returns_project(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "strict-local"})
        assert read_plugin_scope(tmp_path) == "project"

    def test_local_with_global_skills_returns_project(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "local-with-global-skills"})
        assert read_plugin_scope(tmp_path) == "project"

    def test_local_mcp_only_returns_merge(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "local-mcp-only"})
        assert read_plugin_scope(tmp_path) == "merge"

    def test_merge_profile_explicit_returns_merge(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "merge"})
        assert read_plugin_scope(tmp_path) == "merge"

    def test_custom_profile_returns_merge(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "custom"})
        assert read_plugin_scope(tmp_path) == "merge"

    def test_override_plugins_global(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path, {"profile": "merge", "overrides": {"plugins": "global"}}
        )
        assert read_plugin_scope(tmp_path) == "global"

    def test_override_plugins_project(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path, {"profile": "merge", "overrides": {"plugins": "project"}}
        )
        assert read_plugin_scope(tmp_path) == "project"

    def test_override_beats_profile(self, tmp_path: Path) -> None:
        """Override wins even when profile says project → can be overridden to global."""
        _write_config(
            tmp_path,
            {"profile": "strict-local", "overrides": {"plugins": "global"}},
        )
        assert read_plugin_scope(tmp_path) == "global"

    def test_malformed_json_returns_merge_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cp = tmp_path / ".code_puppy"
        cp.mkdir()
        (cp / "config.json").write_text("{not valid json {{")
        (tmp_path / ".git").mkdir()
        with caplog.at_level(logging.WARNING):
            result = read_plugin_scope(tmp_path)
        assert result == "merge"
        assert "failed to read plugin scope" in caplog.text

    def test_non_dict_json_returns_merge(self, tmp_path: Path) -> None:
        """Top-level JSON array is not a valid config."""
        cp = tmp_path / ".code_puppy"
        cp.mkdir()
        (cp / "config.json").write_text('"just a string"')
        (tmp_path / ".git").mkdir()
        assert read_plugin_scope(tmp_path) == "merge"

    def test_unknown_profile_returns_merge_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(tmp_path, {"profile": "totally-bogus-profile"})
        with caplog.at_level(logging.WARNING):
            result = read_plugin_scope(tmp_path)
        assert result == "merge"
        assert "unknown profile" in caplog.text

    def test_invalid_override_scope_keeps_profile_default_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bad override value is ignored; profile default is used."""
        _write_config(
            tmp_path,
            {"profile": "strict-local", "overrides": {"plugins": "not-a-scope"}},
        )
        with caplog.at_level(logging.WARNING):
            result = read_plugin_scope(tmp_path)
        # strict-local → project; bad override ignored → still project
        assert result == "project"
        assert "invalid plugins override" in caplog.text

    def test_walk_up_stops_at_git_boundary(self, tmp_path: Path) -> None:
        """Config above a .git/ boundary is NOT found."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        # .code_puppy/ is at tmp_path level (above the .git/ boundary)
        _write_config(tmp_path, {"profile": "strict-local"})
        sub = repo / "sub"
        sub.mkdir()
        # Walk from sub hits .git/ in repo/ and stops — should NOT see tmp_path config
        assert read_plugin_scope(sub) == "merge"

    def test_none_cwd_uses_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cwd=None falls back to Path.cwd(); no crash."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir()
        # No .code_puppy/ here → merge
        assert read_plugin_scope(None) == "merge"

    def test_config_found_in_cwd_via_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cwd=None and config in cwd → scope is read correctly."""
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path, {"profile": "strict-local"})
        assert read_plugin_scope(None) == "project"

    def test_non_dict_overrides_ignored(self, tmp_path: Path) -> None:
        """If overrides is not a dict, it is silently skipped."""
        _write_config(
            tmp_path, {"profile": "merge", "overrides": ["list", "not", "dict"]}
        )
        assert read_plugin_scope(tmp_path) == "merge"

    def test_other_overrides_dont_affect_plugins(self, tmp_path: Path) -> None:
        """Overrides for other surfaces don't change the plugins scope."""
        _write_config(
            tmp_path,
            {
                "profile": "merge",
                "overrides": {"agents": "project", "skills": "global"},
            },
        )
        assert read_plugin_scope(tmp_path) == "merge"
