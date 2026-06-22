"""Tests for code_puppy.plugins.project_workspace.surfaces.skills.

Covers:
- _load_skills_from_dir: empty dir, nonexistent dir, valid skill, multiple skills,
  skill dir without SKILL.md (warned + skipped), mixed valid + invalid
- merge scope: workspace_root == cwd (None) → empty (upstream handles it)
- merge scope: workspace_root has skills → they are returned
- merge scope: name collision → project wins (exclusion emitted for global version)
- project scope: only project skills load; global-only skills are excluded
- project scope: name collision → project wins (global excluded, project returned)
- project scope + no workspace root → falls through (empty list, no error)
- project scope + workspace has no skills dir → falls through (empty)
- global scope: project-only skills are excluded
- global scope: skills in both global and project-cwd are NOT excluded
- global scope + no cwd project dir → returns empty list (nothing to exclude)
- global scope + cwd project dir exists but empty → returns empty list
- Malformed skill dirs (no SKILL.md) in either dir → logged + skipped, no crash
- Empty dirs → empty list, no error
- Unknown scope → falls back to merge behaviour, no crash
"""

from __future__ import annotations

from pathlib import Path


from code_puppy.plugins.project_workspace.surfaces.skills import (
    _load_skills_from_dir,
    get_skills_for_scope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_skill(directory: Path, name: str, *, content: str | None = None) -> Path:
    """Write a minimal SKILL.md in *directory*/<name>/ and return the dir path."""
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = (
        content or f"---\nname: {name}\ndescription: Test skill {name}\n---\n# {name}\n"
    )
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def _write_skill_no_md(directory: Path, name: str) -> Path:
    """Write a skill directory WITHOUT a SKILL.md (invalid skill)."""
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "README.txt").write_text("oops, forgot SKILL.md", encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# _load_skills_from_dir
# ---------------------------------------------------------------------------


class TestLoadSkillsFromDir:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = _load_skills_from_dir(tmp_path)
        assert result == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = _load_skills_from_dir(tmp_path / "does-not-exist")
        assert result == []

    def test_loads_valid_skill(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "my-skill")
        result = _load_skills_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "my-skill"
        assert "skill_md_path" in result[0]
        assert result[0]["skill_md_path"].endswith("SKILL.md")

    def test_loads_multiple_skills(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        _write_skill(tmp_path, "beta")
        result = _load_skills_from_dir(tmp_path)
        names = {s["name"] for s in result}
        assert names == {"alpha", "beta"}

    def test_skips_dir_without_skill_md(self, tmp_path: Path) -> None:
        _write_skill_no_md(tmp_path, "incomplete-skill")
        result = _load_skills_from_dir(tmp_path)
        assert result == []

    def test_mixed_valid_and_invalid(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "good-skill")
        _write_skill_no_md(tmp_path, "bad-skill")
        result = _load_skills_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "good-skill"

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, ".hidden-skill")
        _write_skill(tmp_path, "visible-skill")
        result = _load_skills_from_dir(tmp_path)
        names = {s["name"] for s in result}
        assert ".hidden-skill" not in names
        assert "visible-skill" in names

    def test_skips_non_directory_entries(self, tmp_path: Path) -> None:
        """Files at the top level of the skills dir are ignored."""
        _write_skill(tmp_path, "real-skill")
        (tmp_path / "stray-file.md").write_text(
            "I am not a skill dir", encoding="utf-8"
        )
        result = _load_skills_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "real-skill"


# ---------------------------------------------------------------------------
# get_skills_for_scope — merge
# ---------------------------------------------------------------------------


class TestMergeScope:
    def test_no_workspace_returns_empty(self, tmp_path: Path) -> None:
        """merge + no workspace root: upstream handles everything, callback no-op."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        result = get_skills_for_scope(
            "merge",
            workspace_root=None,
            _global_dir=global_dir,
        )
        assert result == []

    def test_project_skills_returned_for_workspace(self, tmp_path: Path) -> None:
        """merge: workspace project skills are returned (handles subdir case)."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "project-skill")

        result = get_skills_for_scope(
            "merge",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        additions = [r for r in result if not r.get("exclude")]
        names = {r["name"] for r in additions}
        assert "project-skill" in names

    def test_merge_name_collision_project_wins(self, tmp_path: Path) -> None:
        """merge: skill in both dirs → global excluded, project skill entry returned."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "shared-skill")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "shared-skill")

        result = get_skills_for_scope(
            "merge",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        exclusions = {r["name"] for r in result if r.get("exclude")}
        additions = {r["name"] for r in result if not r.get("exclude")}

        # Global version excluded, project version added
        assert "shared-skill" in exclusions
        assert "shared-skill" in additions

    def test_merge_empty_project_dir_returns_empty(self, tmp_path: Path) -> None:
        """merge: workspace project skills dir exists but empty → no-op."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        project_dir.mkdir(parents=True, exist_ok=True)  # exists but empty

        result = get_skills_for_scope(
            "merge",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        assert result == []

    def test_merge_global_only_skill_not_excluded(self, tmp_path: Path) -> None:
        """merge: global skill with no project counterpart is NOT excluded."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-only")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "project-skill")

        result = get_skills_for_scope(
            "merge",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        exclusions = {r["name"] for r in result if r.get("exclude")}
        assert "global-only" not in exclusions


# ---------------------------------------------------------------------------
# get_skills_for_scope — project
# ---------------------------------------------------------------------------


class TestProjectScope:
    def test_no_workspace_returns_empty_fallthrough(self, tmp_path: Path) -> None:
        """project scope + no workspace root → fall through to global (empty list)."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        result = get_skills_for_scope(
            "project",
            workspace_root=None,
            _global_dir=global_dir,
        )
        assert result == []

    def test_no_skills_dir_returns_empty_fallthrough(self, tmp_path: Path) -> None:
        """project scope + workspace exists but no skills/ dir → fall through."""
        workspace_root = tmp_path / "project"
        workspace_root.mkdir()
        (workspace_root / ".code_puppy").mkdir()
        # No skills/ subdir here

        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        result = get_skills_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        assert result == []

    def test_project_skills_returned(self, tmp_path: Path) -> None:
        """project scope: project skills are in the returned list."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-only")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "project-skill")

        result = get_skills_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        additions = [r for r in result if not r.get("exclude")]
        exclusions = [r for r in result if r.get("exclude")]

        add_names = {r["name"] for r in additions}
        excl_names = {r["name"] for r in exclusions}

        assert "project-skill" in add_names
        assert "global-only" in excl_names

    def test_global_only_skills_excluded(self, tmp_path: Path) -> None:
        """project scope: global skills not in project appear as exclude dicts."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-alpha")
        _write_skill(global_dir, "global-beta")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "project-skill")

        result = get_skills_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        excl_names = {r["name"] for r in result if r.get("exclude")}
        assert excl_names == {"global-alpha", "global-beta"}

    def test_shared_skill_not_excluded(self, tmp_path: Path) -> None:
        """project scope: skill in both global and project is NOT excluded."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "shared")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "shared")

        result = get_skills_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        excl_names = {r["name"] for r in result if r.get("exclude")}
        assert "shared" not in excl_names

    def test_empty_global_dir_no_error(self, tmp_path: Path) -> None:
        """project scope with empty global dir — no crash."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()  # exists but empty

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "project-skill")

        result = get_skills_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        add_names = {r["name"] for r in result if not r.get("exclude")}
        assert "project-skill" in add_names

    def test_malformed_skill_in_project_dir_skipped(self, tmp_path: Path) -> None:
        """project scope: skill dir without SKILL.md is skipped, no crash."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill_no_md(project_dir, "broken-skill")
        _write_skill(project_dir, "valid-skill")

        result = get_skills_for_scope(
            "project",
            workspace_root=workspace_root,
            _global_dir=global_dir,
        )
        add_names = {r["name"] for r in result if not r.get("exclude")}
        assert "valid-skill" in add_names
        assert "broken-skill" not in add_names


# ---------------------------------------------------------------------------
# get_skills_for_scope — global
# ---------------------------------------------------------------------------


class TestGlobalScope:
    def test_no_cwd_project_dir_returns_empty(self, tmp_path: Path) -> None:
        """global scope + no cwd project dir: nothing to exclude."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        result = get_skills_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=None,
        )
        assert result == []

    def test_nonexistent_cwd_project_dir_returns_empty(self, tmp_path: Path) -> None:
        """global scope + cwd project dir does not exist: nothing to exclude."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        result = get_skills_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=tmp_path / "does-not-exist",
        )
        assert result == []

    def test_project_only_skills_excluded(self, tmp_path: Path) -> None:
        """global scope: project skills not in global are excluded."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        cwd_project_dir = tmp_path / "cwd_project"
        _write_skill(cwd_project_dir, "project-only-skill")

        result = get_skills_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        excl_names = {r["name"] for r in result if r.get("exclude")}
        assert "project-only-skill" in excl_names

    def test_shared_skill_not_excluded(self, tmp_path: Path) -> None:
        """global scope: skill in both global and project-cwd is NOT excluded."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "shared")
        _write_skill(global_dir, "global-only")

        cwd_project_dir = tmp_path / "cwd_project"
        _write_skill(cwd_project_dir, "shared")
        _write_skill(cwd_project_dir, "project-only")

        result = get_skills_for_scope(
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
        _write_skill(global_dir, "global-skill")

        cwd_project_dir = tmp_path / "cwd_project"
        cwd_project_dir.mkdir()  # exists but empty

        result = get_skills_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        assert result == []

    def test_malformed_project_skill_skipped(self, tmp_path: Path) -> None:
        """global scope: project skill dirs without SKILL.md don't crash."""
        global_dir = tmp_path / "global"
        _write_skill(global_dir, "global-skill")

        cwd_project_dir = tmp_path / "cwd_project"
        _write_skill_no_md(cwd_project_dir, "bad-skill")

        result = get_skills_for_scope(
            "global",
            workspace_root=None,
            _global_dir=global_dir,
            _cwd_project_dir=cwd_project_dir,
        )
        # bad-skill was skipped → nothing to exclude
        assert result == []


# ---------------------------------------------------------------------------
# get_skills_for_scope — unknown scope fallback
# ---------------------------------------------------------------------------


class TestUnknownScope:
    def test_unknown_scope_falls_back_to_merge(self, tmp_path: Path) -> None:
        """Unknown scope falls back to merge behaviour — no crash."""
        workspace_root = tmp_path / "project"
        project_dir = workspace_root / ".code_puppy" / "skills"
        _write_skill(project_dir, "project-skill")

        result = get_skills_for_scope(
            "bogus-scope",
            workspace_root=workspace_root,
            _global_dir=tmp_path / "global",
        )
        # Should return project skills (merge fallback)
        names = {r["name"] for r in result if not r.get("exclude")}
        assert "project-skill" in names

    def test_unknown_scope_no_workspace_returns_empty(self, tmp_path: Path) -> None:
        """Unknown scope with no workspace: falls back to merge no-op."""
        result = get_skills_for_scope(
            "bogus-scope",
            workspace_root=None,
            _global_dir=tmp_path / "global",
        )
        assert result == []
