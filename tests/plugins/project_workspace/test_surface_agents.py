"""Tests for code_puppy.plugins.project_workspace.surfaces.agents.

Covers:
- merge scope: global + project agents both appear
- merge scope: name collision → project wins (project agent registered after global)
- merge scope: no workspace root → returns empty list (upstream handles everything)
- project scope: only project agents load; global-only agents are excluded
- project scope: name collision → project wins (project agent kept, global excluded)
- project scope + no workspace root → falls through to global (empty list, no error)
- project scope + workspace has no agents dir → falls through to global (empty)
- global scope: only global; project-only agents are excluded
- global scope: name collision (agent in both) → NOT excluded (global keeps it)
- global scope + no cwd project dir → returns empty list (nothing to exclude)
- Malformed agent JSON in either dir → logged + skipped, does not crash
- Empty agents dirs → empty list, no error
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from code_puppy.plugins.project_workspace.surfaces.agents import (
    _load_agents_from_dir,
    get_agents_for_scope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_agent(
    directory: Path, name: str, *, extra: dict[str, Any] | None = None
) -> Path:
    """Write a minimal valid agent JSON file into *directory* and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "name": name,
        "description": f"Test agent {name}",
        "system_prompt": f"You are {name}.",
        "tools": [],
    }
    if extra:
        data.update(extra)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _load_agents_from_dir
# ---------------------------------------------------------------------------


class TestLoadAgentsFromDir:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = _load_agents_from_dir(tmp_path)
        assert result == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = _load_agents_from_dir(tmp_path / "does-not-exist")
        assert result == []

    def test_loads_valid_agent(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "my-agent")
        result = _load_agents_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "my-agent"
        assert "json_path" in result[0]

    def test_loads_multiple_agents(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "alpha")
        _write_agent(tmp_path, "beta")
        result = _load_agents_from_dir(tmp_path)
        names = {a["name"] for a in result}
        assert names == {"alpha", "beta"}

    def test_malformed_json_is_skipped(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ NOT VALID JSON }", encoding="utf-8")
        result = _load_agents_from_dir(tmp_path)
        assert result == []

    def test_missing_required_fields_is_skipped(self, tmp_path: Path) -> None:
        """JSONAgent requires name/description/system_prompt/tools — skip incomplete files."""
        incomplete = tmp_path / "incomplete.json"
        incomplete.write_text(json.dumps({"name": "orphan"}), encoding="utf-8")
        result = _load_agents_from_dir(tmp_path)
        assert result == []

    def test_valid_and_malformed_mixed(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "good-agent")
        bad = tmp_path / "bad.json"
        bad.write_text("NOT JSON", encoding="utf-8")
        result = _load_agents_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "good-agent"


# ---------------------------------------------------------------------------
# get_agents_for_scope — merge
# ---------------------------------------------------------------------------


class TestMergeScope:
    def test_no_workspace_returns_empty(self, tmp_path: Path) -> None:
        """merge + no workspace root: upstream handles everything, callback is a no-op."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        result = get_agents_for_scope(
            "merge",
            workspace_root=None,
            _global_dir=global_dir,
        )
        assert result == []

    def test_project_agents_registered_for_workspace(self, tmp_path: Path) -> None:
        """merge: workspace project agents are registered (handles subdir case)."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        _write_agent(project_dir, "project-agent")

        result = get_agents_for_scope(
            "merge",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        names = {a["name"] for a in result}
        assert "project-agent" in names

    def test_merge_name_collision_project_registered(self, tmp_path: Path) -> None:
        """merge: when global and project share a name, project's json_path is returned."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "shared-agent")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        project_path = _write_agent(
            project_dir, "shared-agent", extra={"description": "PROJECT"}
        )

        result = get_agents_for_scope(
            "merge",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        shared = next((a for a in result if a["name"] == "shared-agent"), None)
        assert shared is not None
        assert shared["json_path"] == str(project_path)


# ---------------------------------------------------------------------------
# get_agents_for_scope — project
# ---------------------------------------------------------------------------


class TestProjectScope:
    def test_no_workspace_returns_empty_fallthrough(self, tmp_path: Path) -> None:
        """project scope + no workspace root → fall through to global (empty list)."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        result = get_agents_for_scope(
            "project",
            workspace_root=None,
            _global_dir=global_dir,
        )
        assert result == []

    def test_no_agents_dir_returns_empty_fallthrough(self, tmp_path: Path) -> None:
        """project scope + workspace exists but no agents/ dir → fall through."""
        workspace_root = tmp_path / "project"
        workspace_root.mkdir()
        (workspace_root / ".code_puppy").mkdir()
        # No agents/ subdir here

        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        result = get_agents_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        assert result == []

    def test_project_only_agents_returned(self, tmp_path: Path) -> None:
        """project scope: project agents are in the returned list."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-only")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        _write_agent(project_dir, "project-agent")

        result = get_agents_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        registrations = [r for r in result if not r.get("exclude")]
        exclusions = [r for r in result if r.get("exclude")]

        reg_names = {r["name"] for r in registrations}
        excl_names = {r["name"] for r in exclusions}

        assert "project-agent" in reg_names
        assert "global-only" in excl_names

    def test_global_only_agents_excluded(self, tmp_path: Path) -> None:
        """project scope: global agents not in project appear as exclude dicts."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-alpha")
        _write_agent(global_dir, "global-beta")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        _write_agent(project_dir, "project-agent")

        result = get_agents_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        exclusions = {r["name"] for r in result if r.get("exclude")}
        assert exclusions == {"global-alpha", "global-beta"}

    def test_shared_name_not_excluded(self, tmp_path: Path) -> None:
        """project scope: agent in both global and project is NOT excluded."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "shared")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        _write_agent(project_dir, "shared")

        result = get_agents_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        exclusions = {r["name"] for r in result if r.get("exclude")}
        assert "shared" not in exclusions

    def test_empty_global_dir_no_error(self, tmp_path: Path) -> None:
        """project scope with empty global dir — no crash."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        _write_agent(project_dir, "project-agent")

        result = get_agents_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        reg_names = {r["name"] for r in result if not r.get("exclude")}
        assert "project-agent" in reg_names


# ---------------------------------------------------------------------------
# get_agents_for_scope — global
# ---------------------------------------------------------------------------


class TestGlobalScope:
    def test_no_cwd_project_dir_returns_empty(self, tmp_path: Path) -> None:
        """global scope + no cwd project dir: nothing to exclude."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        result = get_agents_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=None,
        )
        assert result == []

    def test_project_only_agents_excluded(self, tmp_path: Path) -> None:
        """global scope: project agents not in global are excluded."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        cwd_project_dir = tmp_path / "cwd_project"
        _write_agent(cwd_project_dir, "project-only-agent")

        result = get_agents_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        excl_names = {r["name"] for r in result if r.get("exclude")}
        assert "project-only-agent" in excl_names

    def test_shared_agent_not_excluded(self, tmp_path: Path) -> None:
        """global scope: agent in both global and project-cwd is NOT excluded."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "shared")
        _write_agent(global_dir, "global-only")

        cwd_project_dir = tmp_path / "cwd_project"
        _write_agent(cwd_project_dir, "shared")
        _write_agent(cwd_project_dir, "project-only")

        result = get_agents_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        excl_names = {r["name"] for r in result if r.get("exclude")}
        # "shared" is in global — should NOT be excluded
        assert "shared" not in excl_names
        # "project-only" is NOT in global — should be excluded
        assert "project-only" in excl_names

    def test_empty_cwd_project_dir_returns_empty(self, tmp_path: Path) -> None:
        """global scope with empty cwd project dir — nothing to exclude."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        cwd_project_dir = tmp_path / "cwd_project"
        cwd_project_dir.mkdir()  # exists but empty

        result = get_agents_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        assert result == []

    def test_malformed_project_agent_skipped(self, tmp_path: Path) -> None:
        """global scope: malformed project agent files don't crash the surface."""
        global_dir = tmp_path / "global"
        _write_agent(global_dir, "global-agent")

        cwd_project_dir = tmp_path / "cwd_project"
        cwd_project_dir.mkdir()
        (cwd_project_dir / "bad.json").write_text("NOPE", encoding="utf-8")

        result = get_agents_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        # Bad file skipped → no project agents → nothing to exclude
        assert result == []


# ---------------------------------------------------------------------------
# get_agents_for_scope — unknown scope fallback
# ---------------------------------------------------------------------------


class TestUnknownScope:
    def test_unknown_scope_falls_back_to_merge(self, tmp_path: Path) -> None:
        """Unknown scope falls back to merge behaviour — no crash."""
        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "agents"
        _write_agent(project_dir, "project-agent")

        result = get_agents_for_scope(
            "bogus-scope",
            workspace_root=workspace_root,
            _global_dir=tmp_path / "global",
        )
        # Should return project agents (merge fallback)
        names = {a["name"] for a in result if not a.get("exclude")}
        assert "project-agent" in names
