"""End-to-end integration test: all 6 workspace surfaces from one fixture.

Builds a complete, realistic ``.code_puppy/`` tree under ``tmp_path`` and
exercises every surface's public ``get_*_for_scope`` / ``apply_*_scope``
function against it.  By testing the surface functions directly (rather than
firing full callbacks) we stay fast, deterministic, and actually maintainable.

Surfaces covered
----------------
1. config    — load_workspace_config() reads profile + overrides
2. agents    — get_agents_for_scope() returns project / merge / global lists
3. skills    — get_skills_for_scope() ditto
4. mcp       — apply_mcp_scope() registers / disables servers via mock manager
5. hooks     — apply_hooks_scope() adds / reloads via mock engine
6. file_perms — check_file_permission() + load_policy() enforces policy

Extra scenarios
---------------
- Override: strict-local profile but hooks overridden to merge
- Profile switching: config.json rewritten between test runs
- No-workspace fallback: None root → merge defaults, empty surface results
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_puppy.plugins.project_workspace.config import load_workspace_config
from code_puppy.plugins.project_workspace.surfaces.agents import get_agents_for_scope
from code_puppy.plugins.project_workspace.surfaces.file_permissions import (
    check_file_permission,
    load_policy,
)
from code_puppy.plugins.project_workspace.surfaces.hooks import apply_hooks_scope
from code_puppy.plugins.project_workspace.surfaces.mcp import apply_mcp_scope
from code_puppy.plugins.project_workspace.surfaces.skills import get_skills_for_scope

# ---------------------------------------------------------------------------
# Fixture data constants
# ---------------------------------------------------------------------------

_AGENT_JSON = {
    "name": "sample-agent",
    "description": "A sample project-workspace agent for e2e testing.",
    "system_prompt": "You are a sample e2e agent.",
    "tools": ["read_file", "list_files"],
}

_SKILL_MD = "---\nname: sample-skill\n---\n\n# Sample Skill\n\nTest skill for e2e.\n"

_MCP_SERVERS = {
    "mcp_servers": {
        "project-mcp": {
            "type": "stdio",
            "command": "uvx",
            "args": ["sample-mcp"],
        }
    }
}

# Bare-format hooks (event key at top level)
_HOOKS = {
    "PreToolUse": [
        {
            "matcher": "agent_run_shell_command",
            "hooks": [
                {"type": "command", "command": "echo pre-check", "timeout": 5000}
            ],
        }
    ]
}

# deny uses ./ prefix so it resolves against workspace root
_FILE_POLICY = {
    "allow": [],
    "deny": ["./.code_puppy/secrets/**"],
    "allow_outside_project": False,
}

_SAMPLE_PLUGIN = """\
from code_puppy.callbacks import register_callback

def _on_startup():
    pass

register_callback("startup", _on_startup)
"""

# ---------------------------------------------------------------------------
# Shared workspace fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return *tmp_path* populated with a complete ``.code_puppy/`` tree.

    Profile: strict-local, with ``hooks`` overridden to ``merge``.

    Layout::

        <tmp_path>/
        ├── .git/
        ├── .code_puppy/
        │   ├── config.json               # strict-local + hooks→merge override
        │   ├── agents/sample-agent.json
        │   ├── skills/sample-skill/SKILL.md
        │   ├── plugins/sample/register_callbacks.py
        │   ├── mcp_servers.json
        │   ├── hooks.json
        │   └── file_policy.json
        └── .claude/settings.json
    """
    root = tmp_path

    # .git — workspace boundary anchor
    (root / ".git").mkdir()

    dot = root / ".code_puppy"
    dot.mkdir()

    # Config: strict-local but hooks overridden to merge
    (dot / "config.json").write_text(
        json.dumps({"profile": "strict-local", "overrides": {"hooks": "merge"}}),
        encoding="utf-8",
    )

    # Agents
    agents_dir = dot / "agents"
    agents_dir.mkdir()
    (agents_dir / "sample-agent.json").write_text(
        json.dumps(_AGENT_JSON), encoding="utf-8"
    )

    # Skills
    skill_dir = dot / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")

    # Plugins
    plugin_dir = dot / "plugins" / "sample"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "register_callbacks.py").write_text(_SAMPLE_PLUGIN, encoding="utf-8")

    # MCP servers
    (dot / "mcp_servers.json").write_text(json.dumps(_MCP_SERVERS), encoding="utf-8")

    # Hooks
    (dot / "hooks.json").write_text(json.dumps(_HOOKS), encoding="utf-8")

    # File policy
    (dot / "file_policy.json").write_text(json.dumps(_FILE_POLICY), encoding="utf-8")

    # .claude/settings.json (alternate hooks source, present to mirror real setups)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{}", encoding="utf-8")

    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_dir(tmp_path: Path, name: str) -> Path:
    """Create and return an empty directory under tmp_path."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mock_manager(existing: list | None = None):
    """Return a MagicMock MCPManager with an empty registry by default."""
    mgr = MagicMock()
    mgr.registry.list_all.return_value = existing or []
    return mgr


# ---------------------------------------------------------------------------
# I. Fixture integrity
# ---------------------------------------------------------------------------


class TestFixtureIntegrity:
    """Sanity: the fixture builds everything we claim it does."""

    def test_dot_code_puppy_exists(self, workspace: Path) -> None:
        assert (workspace / ".code_puppy").is_dir()

    def test_all_files_present(self, workspace: Path) -> None:
        dot = workspace / ".code_puppy"
        assert (dot / "config.json").is_file()
        assert (dot / "agents" / "sample-agent.json").is_file()
        assert (dot / "skills" / "sample-skill" / "SKILL.md").is_file()
        assert (dot / "plugins" / "sample" / "register_callbacks.py").is_file()
        assert (dot / "mcp_servers.json").is_file()
        assert (dot / "hooks.json").is_file()
        assert (dot / "file_policy.json").is_file()

    def test_git_dir_present(self, workspace: Path) -> None:
        assert (workspace / ".git").is_dir()

    def test_claude_settings_present(self, workspace: Path) -> None:
        assert (workspace / ".claude" / "settings.json").is_file()


# ---------------------------------------------------------------------------
# II. Config surface
# ---------------------------------------------------------------------------


class TestConfigSurface:
    """load_workspace_config() reads profile + overrides correctly."""

    def test_loads_strict_local_profile(self, workspace: Path) -> None:
        cfg = load_workspace_config(workspace)
        assert cfg.profile == "strict-local"
        assert cfg.root == workspace

    def test_strict_local_non_overridden_surfaces_are_project(
        self, workspace: Path
    ) -> None:
        cfg = load_workspace_config(workspace)
        for surface in ("agents", "skills", "plugins", "mcp", "file_permissions"):
            assert cfg.surfaces[surface] == "project", f"{surface} should be project"

    def test_hooks_overridden_to_merge(self, workspace: Path) -> None:
        cfg = load_workspace_config(workspace)
        assert cfg.surfaces["hooks"] == "merge"

    def test_missing_config_json_defaults_to_merge(self, tmp_path: Path) -> None:
        dot = tmp_path / ".code_puppy"
        dot.mkdir()
        cfg = load_workspace_config(tmp_path)
        assert cfg.profile == "merge"
        assert all(v == "merge" for v in cfg.surfaces.values())


# ---------------------------------------------------------------------------
# III. Agents surface
# ---------------------------------------------------------------------------


class TestAgentsSurface:
    """get_agents_for_scope() returns the right agents for each scope."""

    def test_project_scope_finds_workspace_agent(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        empty = _empty_dir(tmp_path, "empty-agents")
        result = get_agents_for_scope(
            "project", workspace, _global_dir=empty, _cwd_project_dir=empty
        )
        names = {r["name"] for r in result if not r.get("exclude")}
        assert "sample-agent" in names

    def test_merge_scope_finds_workspace_agent(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        empty = _empty_dir(tmp_path, "empty-agents")
        result = get_agents_for_scope(
            "merge", workspace, _global_dir=empty, _cwd_project_dir=empty
        )
        names = {r["name"] for r in result if not r.get("exclude")}
        assert "sample-agent" in names

    def test_global_scope_excludes_cwd_project_agents(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        # Put a cwd-project agent in a separate dir
        cwd_dir = _empty_dir(tmp_path, "cwd-agents")
        (cwd_dir / "cwd-agent.json").write_text(
            json.dumps(
                {
                    "name": "cwd-agent",
                    "description": "cwd agent",
                    "system_prompt": "test",
                    "tools": [],
                }
            ),
            encoding="utf-8",
        )
        empty_global = _empty_dir(tmp_path, "empty-global")

        result = get_agents_for_scope(
            "global", workspace, _global_dir=empty_global, _cwd_project_dir=cwd_dir
        )
        excluded = {r["name"] for r in result if r.get("exclude")}
        assert "cwd-agent" in excluded

    def test_project_scope_no_root_returns_empty(self, tmp_path: Path) -> None:
        empty = _empty_dir(tmp_path, "empty")
        result = get_agents_for_scope(
            "project", None, _global_dir=empty, _cwd_project_dir=empty
        )
        assert result == []

    def test_project_scope_result_contains_json_path(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        empty = _empty_dir(tmp_path, "empty-agents")
        result = get_agents_for_scope(
            "project", workspace, _global_dir=empty, _cwd_project_dir=empty
        )
        non_excl = [r for r in result if not r.get("exclude")]
        assert len(non_excl) == 1
        assert "json_path" in non_excl[0]
        assert non_excl[0]["json_path"].endswith(".json")


# ---------------------------------------------------------------------------
# IV. Skills surface
# ---------------------------------------------------------------------------


class TestSkillsSurface:
    """get_skills_for_scope() returns the right skills for each scope."""

    def test_project_scope_finds_workspace_skill(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        empty = _empty_dir(tmp_path, "empty-skills")
        result = get_skills_for_scope("project", workspace, _global_dir=empty)
        names = {r["name"] for r in result if not r.get("exclude")}
        assert "sample-skill" in names

    def test_merge_scope_finds_workspace_skill(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        empty = _empty_dir(tmp_path, "empty-skills")
        result = get_skills_for_scope(
            "merge", workspace, _global_dir=empty, _cwd_project_dir=empty
        )
        names = {r["name"] for r in result if not r.get("exclude")}
        assert "sample-skill" in names

    def test_project_scope_result_contains_skill_md_path(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        empty = _empty_dir(tmp_path, "empty-skills")
        result = get_skills_for_scope("project", workspace, _global_dir=empty)
        non_excl = [r for r in result if not r.get("exclude")]
        assert len(non_excl) == 1
        assert "skill_md_path" in non_excl[0]
        assert non_excl[0]["skill_md_path"].endswith("SKILL.md")

    def test_project_scope_no_root_returns_empty(self, tmp_path: Path) -> None:
        empty = _empty_dir(tmp_path, "empty")
        result = get_skills_for_scope(
            "project", None, _global_dir=empty, _cwd_project_dir=empty
        )
        assert result == []


# ---------------------------------------------------------------------------
# V. MCP surface
# ---------------------------------------------------------------------------


class TestMcpSurface:
    """apply_mcp_scope() registers / disables MCP servers per scope."""

    def test_merge_scope_registers_project_server(self, workspace: Path) -> None:
        mgr = _mock_manager()
        project_file = workspace / ".code_puppy" / "mcp_servers.json"
        apply_mcp_scope("merge", workspace, _manager=mgr, _project_file=project_file)
        assert mgr.register_server.called

    def test_project_scope_registers_project_server(self, workspace: Path) -> None:
        mgr = _mock_manager()
        project_file = workspace / ".code_puppy" / "mcp_servers.json"
        apply_mcp_scope("project", workspace, _manager=mgr, _project_file=project_file)
        assert mgr.register_server.called

    def test_global_scope_is_noop(self, workspace: Path) -> None:
        mgr = _mock_manager()
        project_file = workspace / ".code_puppy" / "mcp_servers.json"
        apply_mcp_scope("global", workspace, _manager=mgr, _project_file=project_file)
        mgr.register_server.assert_not_called()
        mgr.update_server.assert_not_called()

    def test_project_scope_disables_unreferenced_global_servers(
        self, workspace: Path
    ) -> None:
        """Global servers not in project mcp_servers.json are disabled."""
        fake_global = MagicMock()
        fake_global.id = "global-123"
        fake_global.name = "global-server"  # NOT in project config

        mgr = _mock_manager(existing=[fake_global])
        fake_managed = MagicMock()
        mgr.get_server.return_value = fake_managed

        project_file = workspace / ".code_puppy" / "mcp_servers.json"
        apply_mcp_scope("project", workspace, _manager=mgr, _project_file=project_file)

        fake_managed.disable.assert_called_once()

    def test_no_workspace_root_is_noop_for_project_scope(self, tmp_path: Path) -> None:
        mgr = _mock_manager()
        apply_mcp_scope("project", None, _manager=mgr)
        mgr.register_server.assert_not_called()


# ---------------------------------------------------------------------------
# VI. Hooks surface
# ---------------------------------------------------------------------------


class TestHooksSurface:
    """apply_hooks_scope() adds / reloads hooks per scope."""

    def test_merge_scope_calls_add_hook(self, workspace: Path) -> None:
        engine = MagicMock()
        project_file = workspace / ".code_puppy" / "hooks.json"
        apply_hooks_scope(
            "merge", workspace, _cch_engine=engine, _project_file=project_file
        )
        assert engine.add_hook.called

    def test_project_scope_calls_reload_config(self, workspace: Path) -> None:
        engine = MagicMock()
        project_file = workspace / ".code_puppy" / "hooks.json"
        apply_hooks_scope(
            "project", workspace, _cch_engine=engine, _project_file=project_file
        )
        engine.reload_config.assert_called_once()
        engine.add_hook.assert_not_called()

    def test_global_scope_is_noop(self, workspace: Path) -> None:
        engine = MagicMock()
        project_file = workspace / ".code_puppy" / "hooks.json"
        apply_hooks_scope(
            "global", workspace, _cch_engine=engine, _project_file=project_file
        )
        engine.add_hook.assert_not_called()
        engine.reload_config.assert_not_called()

    def test_no_engine_does_not_raise(self, workspace: Path) -> None:
        """When the cch engine is unavailable, hooks surface is silently graceful."""
        project_file = workspace / ".code_puppy" / "hooks.json"
        # _cch_engine=None → _get_cch_engine tries real import, may return None
        # Either way must not raise
        apply_hooks_scope(
            "merge", workspace, _cch_engine=None, _project_file=project_file
        )


# ---------------------------------------------------------------------------
# VII. File permissions surface
# ---------------------------------------------------------------------------


class TestFilePermissionsSurface:
    """check_file_permission() + load_policy() enforce the project policy."""

    def test_load_policy_reads_file_policy_json(self, workspace: Path) -> None:
        policy = load_policy(workspace)
        assert isinstance(policy, dict)
        assert "deny" in policy
        assert "allow_outside_project" in policy

    def test_project_scope_allows_inside_workspace(self, workspace: Path) -> None:
        policy = load_policy(workspace)
        inside = workspace / "src" / "main.py"
        result = check_file_permission(
            None, str(inside), "write", "project", workspace, policy
        )
        assert result is None  # None = defer (allowed by project scope)

    def test_project_scope_blocks_outside_workspace(self, workspace: Path) -> None:
        policy = load_policy(workspace)
        # Sibling of workspace root — definitely outside
        outside = workspace.parent / "sibling_dir" / "file.py"
        result = check_file_permission(
            None, str(outside), "write", "project", workspace, policy
        )
        assert result is False

    def test_project_scope_blocks_path_matching_deny_rule(
        self, workspace: Path
    ) -> None:
        policy = load_policy(workspace)
        # ./.code_puppy/secrets/** resolves to workspace/.code_puppy/secrets/**
        denied = workspace / ".code_puppy" / "secrets" / "token.txt"
        result = check_file_permission(
            None, str(denied), "write", "project", workspace, policy
        )
        assert result is False

    def test_merge_scope_allows_outside_workspace(self, workspace: Path) -> None:
        """merge scope: no auto-restriction to workspace root."""
        policy = load_policy(workspace)
        outside = workspace.parent / "sibling.py"
        result = check_file_permission(
            None, str(outside), "write", "merge", workspace, policy
        )
        assert result is None  # merge only applies explicit deny rules

    def test_merge_scope_applies_deny_rules(self, workspace: Path) -> None:
        policy = load_policy(workspace)
        denied = workspace / ".code_puppy" / "secrets" / "token.txt"
        result = check_file_permission(
            None, str(denied), "write", "merge", workspace, policy
        )
        assert result is False

    def test_global_scope_is_always_none(self, workspace: Path) -> None:
        policy = load_policy(workspace)
        # Even a normally-denied path: global defers entirely
        denied = workspace / ".code_puppy" / "secrets" / "token.txt"
        result = check_file_permission(
            None, str(denied), "write", "global", workspace, policy
        )
        assert result is None

    def test_project_scope_no_workspace_root_allows_everything(self) -> None:
        """No workspace root → project scope falls back to no restriction."""
        result = check_file_permission(
            None, "/tmp/anywhere.py", "write", "project", None, None
        )
        assert result is None


# ---------------------------------------------------------------------------
# VIII. Profile switching
# ---------------------------------------------------------------------------


class TestProfileSwitching:
    """Rewriting config.json changes surface scopes consistently."""

    def test_switch_from_strict_local_to_merge(self, workspace: Path) -> None:
        cfg_strict = load_workspace_config(workspace)
        assert cfg_strict.surfaces["agents"] == "project"
        assert cfg_strict.surfaces["hooks"] == "merge"  # overridden

        # Switch to merge (no overrides)
        (workspace / ".code_puppy" / "config.json").write_text(
            json.dumps({"profile": "merge"}), encoding="utf-8"
        )
        cfg_merge = load_workspace_config(workspace)
        assert cfg_merge.profile == "merge"
        assert all(v == "merge" for v in cfg_merge.surfaces.values())

    def test_local_with_global_skills_profile(self, workspace: Path) -> None:
        (workspace / ".code_puppy" / "config.json").write_text(
            json.dumps({"profile": "local-with-global-skills"}), encoding="utf-8"
        )
        cfg = load_workspace_config(workspace)
        assert cfg.surfaces["agents"] == "merge"
        assert cfg.surfaces["skills"] == "global"
        assert cfg.surfaces["mcp"] == "project"
        assert cfg.surfaces["hooks"] == "project"

    def test_local_mcp_only_profile(self, workspace: Path) -> None:
        (workspace / ".code_puppy" / "config.json").write_text(
            json.dumps({"profile": "local-mcp-only"}), encoding="utf-8"
        )
        cfg = load_workspace_config(workspace)
        assert cfg.surfaces["mcp"] == "project"
        assert cfg.surfaces["agents"] == "merge"
        assert cfg.surfaces["skills"] == "merge"
        assert cfg.surfaces["plugins"] == "merge"

    def test_custom_profile_with_per_surface_overrides(self, workspace: Path) -> None:
        (workspace / ".code_puppy" / "config.json").write_text(
            json.dumps(
                {
                    "profile": "custom",
                    "overrides": {"agents": "project", "skills": "global"},
                }
            ),
            encoding="utf-8",
        )
        cfg = load_workspace_config(workspace)
        assert cfg.profile == "custom"
        assert cfg.surfaces["agents"] == "project"
        assert cfg.surfaces["skills"] == "global"
        assert cfg.surfaces["mcp"] == "merge"  # custom default = merge

    def test_profile_switch_reflected_in_agents_surface(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        """Verify config change actually flows through to surface results."""
        empty = _empty_dir(tmp_path, "empty-agents")

        # strict-local: project scope → workspace agents returned
        cfg_strict = load_workspace_config(workspace)
        agents_strict = get_agents_for_scope(
            cfg_strict.surfaces["agents"],
            cfg_strict.root,
            _global_dir=empty,
            _cwd_project_dir=empty,
        )
        assert any(not r.get("exclude") for r in agents_strict)

        # Switch to merge
        (workspace / ".code_puppy" / "config.json").write_text(
            json.dumps({"profile": "merge"}), encoding="utf-8"
        )
        cfg_merge = load_workspace_config(workspace)
        # merge scope with same workspace still finds agents (merge includes project dir)
        agents_merge = get_agents_for_scope(
            cfg_merge.surfaces["agents"],
            cfg_merge.root,
            _global_dir=empty,
            _cwd_project_dir=empty,
        )
        assert any(not r.get("exclude") for r in agents_merge)


# ---------------------------------------------------------------------------
# IX. No-workspace fallback
# ---------------------------------------------------------------------------


class TestNoWorkspaceFallback:
    """No .code_puppy/ → merge defaults everywhere, no errors."""

    def test_config_returns_merge_defaults(self) -> None:
        cfg = load_workspace_config(None)
        assert cfg.profile == "merge"
        assert cfg.root is None
        assert all(v == "merge" for v in cfg.surfaces.values())

    def test_agents_project_scope_no_root_empty(self, tmp_path: Path) -> None:
        empty = _empty_dir(tmp_path, "empty")
        result = get_agents_for_scope(
            "project", None, _global_dir=empty, _cwd_project_dir=empty
        )
        assert result == []

    def test_skills_project_scope_no_root_empty(self, tmp_path: Path) -> None:
        empty = _empty_dir(tmp_path, "empty")
        result = get_skills_for_scope(
            "project", None, _global_dir=empty, _cwd_project_dir=empty
        )
        assert result == []

    def test_mcp_project_scope_no_root_is_noop(self) -> None:
        mgr = _mock_manager()
        apply_mcp_scope("project", None, _manager=mgr)
        mgr.register_server.assert_not_called()

    def test_hooks_merge_scope_no_root_does_not_raise(self) -> None:
        engine = MagicMock()
        apply_hooks_scope("merge", None, _cch_engine=engine)
        engine.add_hook.assert_not_called()

    def test_file_perms_no_policy_project_scope_outside_blocks(self) -> None:
        """No policy + project scope + no workspace root → no restriction."""
        result = check_file_permission(
            None, "/tmp/anything.py", "write", "project", None, None
        )
        assert result is None
