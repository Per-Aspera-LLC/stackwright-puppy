"""Tests for code_puppy.plugins.project_workspace.config.

Covers:
- Each of the 5 named profiles resolves to correct surface defaults
- overrides block layers on top of a profile
- Invalid per-surface scope value falls back to 'merge' + logs warning
- Missing config.json returns merge defaults
- Malformed JSON returns merge defaults + warning
- Empty .code_puppy/ dir (no config.json) returns merge defaults
- root=None returns merge defaults immediately
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from code_puppy.plugins.project_workspace.config import (
    SURFACES,
    load_workspace_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write .code_puppy/config.json under tmp_path, return tmp_path."""
    dot_dir = tmp_path / ".code_puppy"
    dot_dir.mkdir(exist_ok=True)
    (dot_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def _empty_workspace(tmp_path: Path) -> Path:
    """Create .code_puppy/ dir with no config.json, return tmp_path."""
    (tmp_path / ".code_puppy").mkdir(exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Named profile defaults
# ---------------------------------------------------------------------------


class TestNamedProfiles:
    def test_merge_profile(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "merge"})
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "merge"
        assert all(v == "merge" for v in cfg.surfaces.values())
        assert set(cfg.surfaces.keys()) == set(SURFACES)

    def test_strict_local_profile(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "strict-local"})
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "strict-local"
        assert all(v == "project" for v in cfg.surfaces.values())

    def test_local_with_global_skills_profile(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "local-with-global-skills"})
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "local-with-global-skills"
        assert cfg.surfaces["agents"] == "merge"
        assert cfg.surfaces["skills"] == "global"
        assert cfg.surfaces["plugins"] == "project"
        assert cfg.surfaces["mcp"] == "project"
        assert cfg.surfaces["hooks"] == "project"
        assert cfg.surfaces["file_permissions"] == "project"

    def test_local_mcp_only_profile(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "local-mcp-only"})
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "local-mcp-only"
        assert cfg.surfaces["mcp"] == "project"
        # all other surfaces should be merge
        for surface in SURFACES:
            if surface != "mcp":
                assert cfg.surfaces[surface] == "merge", surface

    def test_custom_profile_defaults_to_merge(self, tmp_path: Path) -> None:
        """'custom' with no overrides falls back to all-merge (safe default)."""
        _write_config(tmp_path, {"profile": "custom"})
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "custom"
        assert all(v == "merge" for v in cfg.surfaces.values())


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_override_single_surface(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {"profile": "merge", "overrides": {"mcp": "project"}},
        )
        cfg = load_workspace_config(tmp_path)
        assert cfg.surfaces["mcp"] == "project"
        # everything else still merge
        for surface in SURFACES:
            if surface != "mcp":
                assert cfg.surfaces[surface] == "merge"

    def test_override_strict_local_relax_skills(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {"profile": "strict-local", "overrides": {"skills": "global"}},
        )
        cfg = load_workspace_config(tmp_path)
        assert cfg.surfaces["skills"] == "global"
        # all others still project
        for surface in SURFACES:
            if surface != "skills":
                assert cfg.surfaces[surface] == "project"

    def test_invalid_scope_falls_back_to_merge(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(
            tmp_path,
            {"profile": "strict-local", "overrides": {"agents": "bogus-value"}},
        )
        with caplog.at_level(logging.WARNING):
            cfg = load_workspace_config(tmp_path)

        # agents should have fallen back to merge
        assert cfg.surfaces["agents"] == "merge"
        # warning was logged
        assert any("invalid scope" in r.message.lower() for r in caplog.records)

    def test_unknown_surface_in_overrides_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(
            tmp_path,
            {"profile": "merge", "overrides": {"nonexistent_surface": "project"}},
        )
        with caplog.at_level(logging.WARNING):
            cfg = load_workspace_config(tmp_path)

        assert "nonexistent_surface" not in cfg.surfaces
        assert any("unknown surface" in r.message.lower() for r in caplog.records)

    def test_all_valid_scope_values_accepted(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {
                "profile": "merge",
                "overrides": {
                    "agents": "project",
                    "skills": "global",
                    "mcp": "merge",
                },
            },
        )
        cfg = load_workspace_config(tmp_path)
        assert cfg.surfaces["agents"] == "project"
        assert cfg.surfaces["skills"] == "global"
        assert cfg.surfaces["mcp"] == "merge"


# ---------------------------------------------------------------------------
# Fallback / error cases
# ---------------------------------------------------------------------------


class TestFallbacks:
    def test_no_root_returns_merge_defaults(self) -> None:
        cfg = load_workspace_config(None)
        assert cfg.profile == "merge"
        assert cfg.root is None
        assert all(v == "merge" for v in cfg.surfaces.values())

    def test_missing_config_json_returns_merge(self, tmp_path: Path) -> None:
        """Empty .code_puppy/ with no config.json → merge defaults."""
        _empty_workspace(tmp_path)
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "merge"
        assert cfg.root == tmp_path
        assert all(v == "merge" for v in cfg.surfaces.values())

    def test_malformed_json_returns_merge_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        dot_dir = tmp_path / ".code_puppy"
        dot_dir.mkdir()
        (dot_dir / "config.json").write_text("{ NOT VALID JSON }", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            cfg = load_workspace_config(tmp_path)

        assert cfg.profile == "merge"
        assert any("malformed" in r.message.lower() for r in caplog.records)

    def test_json_not_object_returns_merge_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        dot_dir = tmp_path / ".code_puppy"
        dot_dir.mkdir()
        (dot_dir / "config.json").write_text("[1, 2, 3]", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            cfg = load_workspace_config(tmp_path)

        assert cfg.profile == "merge"
        assert any("json object" in r.message.lower() for r in caplog.records)

    def test_unknown_profile_falls_back_to_merge(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(tmp_path, {"profile": "turbo-isolation"})

        with caplog.at_level(logging.WARNING):
            cfg = load_workspace_config(tmp_path)

        assert cfg.profile == "merge"
        assert any("unknown profile" in r.message.lower() for r in caplog.records)

    def test_root_returned_on_missing_config(self, tmp_path: Path) -> None:
        """Even with missing config.json, root is still set on the result."""
        _empty_workspace(tmp_path)
        cfg = load_workspace_config(tmp_path)
        assert cfg.root == tmp_path

    def test_root_returned_on_valid_config(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"profile": "strict-local"})
        cfg = load_workspace_config(tmp_path)
        assert cfg.root == tmp_path
