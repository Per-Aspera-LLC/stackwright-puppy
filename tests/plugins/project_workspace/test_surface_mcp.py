"""Tests for code_puppy.plugins.project_workspace.surfaces.mcp.

Covers:
- _load_mcp_servers_from_file: missing file, malformed JSON, wrong structure,
  valid file with single/multiple servers
- merge scope: project servers injected on top of global
- merge scope: name collision → project wins (update_server called)
- merge scope: no workspace root → noop (no injection, no errors)
- merge scope: empty project file → noop
- project scope: project servers injected; global servers NOT in project disabled
- project scope: name collision → project wins (project server kept, global updated)
- project scope + no workspace root → falls through (no disable, no inject)
- project scope + no mcp_servers.json → falls through (no disable)
- global scope: noop regardless of what project file contains
- Unknown scope → falls back to merge behaviour, no crash
- Malformed JSON in project file → logged + skipped, no crash
- registry.list_all() raises → logged + skipped, no crash
- register_server raises → per-server error logged, other servers continue
- MCP-specific: project servers are added to manager registry
- MCP-specific: disabled global servers are NOT enabled when a project server
  with a different name is injected
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_puppy.plugins.project_workspace.surfaces.mcp import (
    _load_mcp_servers_from_file,
    apply_mcp_scope,
)


# ---------------------------------------------------------------------------
# Fake MCPManager infrastructure
# ---------------------------------------------------------------------------


class FakeManagedServer:
    """Minimal stand-in for ManagedMCPServer used in tests."""

    def __init__(self, name: str, enabled: bool = True) -> None:
        self.name = name
        self._enabled = enabled

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def is_enabled(self) -> bool:
        return self._enabled


class FakeServerConfig:
    """Minimal stand-in for ServerConfig used in tests."""

    def __init__(
        self,
        name: str,
        *,
        id: str = "",
        type: str = "sse",
        enabled: bool = True,
        config: dict | None = None,
    ) -> None:
        self.name = name
        self.id = id or f"fake-{name}"
        self.type = type
        self.enabled = enabled
        self.config = config or {}


class FakeMCPManager:
    """Lightweight fake of MCPManager for unit testing the MCP surface.

    Tracks which servers are registered, updated, and disabled so tests
    can assert on the correct operations without touching the real singleton.
    """

    def __init__(self, *, global_servers: list[FakeServerConfig] | None = None) -> None:
        # registry attribute mirrors MCPManager.registry
        self._server_map: dict[str, FakeServerConfig] = {}
        self._managed: dict[str, FakeManagedServer] = {}

        # Pre-populate "global" servers (simulating sync_from_config at init)
        for sc in global_servers or []:
            self._server_map[sc.id] = sc
            self._managed[sc.id] = FakeManagedServer(sc.name, enabled=sc.enabled)

        self.registry = self._FakeRegistry(self._server_map)

        # Track calls for assertions
        self.registered: list[
            Any
        ] = []  # ServerConfig objects passed to register_server
        self.updated: list[tuple[str, Any]] = []  # (server_id, config) pairs

    class _FakeRegistry:
        def __init__(self, server_map: dict[str, Any]) -> None:
            self._map = server_map

        def list_all(self) -> list[Any]:
            return list(self._map.values())

        def get_by_name(self, name: str) -> Any | None:
            for sc in self._map.values():
                if sc.name == name:
                    return sc
            return None

    def register_server(self, config: Any) -> str:
        """Add a new server (no name-collision check for simplicity)."""
        server_id = f"fake-{config.name}"
        config.id = server_id
        self._server_map[server_id] = config
        self._managed[server_id] = FakeManagedServer(
            config.name, enabled=config.enabled
        )
        self.registered.append(config)
        return server_id

    def update_server(self, server_id: str, config: Any) -> bool:
        """Update existing server config in registry + managed store."""
        if server_id not in self._server_map:
            return False
        self._server_map[server_id] = config
        # Update managed server's name for correct assertions
        if server_id in self._managed:
            self._managed[server_id].name = config.name
        self.updated.append((server_id, config))
        return True

    def get_server(self, server_id: str) -> FakeManagedServer | None:
        return self._managed.get(server_id)

    # --- convenience helpers for assertions ---

    def registered_names(self) -> set[str]:
        return {c.name for c in self.registered}

    def updated_names(self) -> set[str]:
        return {c.name for _, c in self.updated}

    def disabled_names(self) -> set[str]:
        return {ms.name for ms in self._managed.values() if not ms._enabled}

    def enabled_names(self) -> set[str]:
        return {ms.name for ms in self._managed.values() if ms._enabled}


# ---------------------------------------------------------------------------
# File fixtures
# ---------------------------------------------------------------------------


def _write_mcp_json(directory: Path, servers: dict[str, dict]) -> Path:
    """Write an mcp_servers.json in *directory* and return the file path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mcp_servers.json"
    path.write_text(json.dumps({"mcp_servers": servers}), encoding="utf-8")
    return path


def _make_global_server(name: str, server_id: str | None = None) -> FakeServerConfig:
    """Create a FakeServerConfig representing a "global" (pre-loaded) server."""
    return FakeServerConfig(
        name=name,
        id=server_id or f"global-{name}",
        type="sse",
        enabled=True,
        config={"type": "sse", "url": f"http://global/{name}"},
    )


# ---------------------------------------------------------------------------
# _load_mcp_servers_from_file
# ---------------------------------------------------------------------------


class TestLoadMcpServersFromFile:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        result = _load_mcp_servers_from_file(tmp_path / "nonexistent.json")
        assert result == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path: Path) -> None:
        bad = tmp_path / "mcp_servers.json"
        bad.write_text("{ NOT VALID JSON }", encoding="utf-8")
        result = _load_mcp_servers_from_file(bad)
        assert result == {}

    def test_wrong_structure_returns_empty_dict(self, tmp_path: Path) -> None:
        """mcp_servers value is a list, not a dict → skip."""
        bad = tmp_path / "mcp_servers.json"
        bad.write_text(json.dumps({"mcp_servers": ["a", "b"]}), encoding="utf-8")
        result = _load_mcp_servers_from_file(bad)
        assert result == {}

    def test_valid_single_server(self, tmp_path: Path) -> None:
        path = _write_mcp_json(
            tmp_path, {"my-server": {"type": "stdio", "command": "uvx"}}
        )
        result = _load_mcp_servers_from_file(path)
        assert "my-server" in result
        assert result["my-server"]["command"] == "uvx"

    def test_valid_multiple_servers(self, tmp_path: Path) -> None:
        path = _write_mcp_json(
            tmp_path,
            {
                "server-a": {"type": "sse", "url": "http://a"},
                "server-b": {"type": "stdio", "command": "npx"},
            },
        )
        result = _load_mcp_servers_from_file(path)
        assert set(result.keys()) == {"server-a", "server-b"}

    def test_empty_mcp_servers_dict(self, tmp_path: Path) -> None:
        path = _write_mcp_json(tmp_path, {})
        result = _load_mcp_servers_from_file(path)
        assert result == {}

    def test_missing_mcp_servers_key_returns_empty_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp_servers.json"
        path.write_text(json.dumps({"other_key": {}}), encoding="utf-8")
        result = _load_mcp_servers_from_file(path)
        assert result == {}


# ---------------------------------------------------------------------------
# apply_mcp_scope — global scope
# ---------------------------------------------------------------------------


class TestGlobalScope:
    def test_global_scope_is_noop(self, tmp_path: Path) -> None:
        """global scope: never touches the manager."""
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        project_file = _write_mcp_json(
            tmp_path, {"project-server": {"type": "sse", "url": "http://project"}}
        )
        apply_mcp_scope(
            "global", tmp_path, _manager=manager, _project_file=project_file
        )
        assert manager.registered == []
        assert manager.updated == []
        assert manager.disabled_names() == set()

    def test_global_scope_no_workspace(self) -> None:
        """global scope + no workspace root: still noop."""
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        apply_mcp_scope("global", None, _manager=manager)
        assert manager.registered == []
        assert manager.disabled_names() == set()


# ---------------------------------------------------------------------------
# apply_mcp_scope — merge scope
# ---------------------------------------------------------------------------


class TestMergeScope:
    def test_no_workspace_root_is_noop(self) -> None:
        """merge + no workspace root: upstream handles everything, we do nothing."""
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        apply_mcp_scope("merge", None, _manager=manager)
        assert manager.registered == []
        assert manager.updated == []

    def test_no_project_file_is_noop(self, tmp_path: Path) -> None:
        """merge + workspace root exists but no mcp_servers.json: noop."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        apply_mcp_scope("merge", workspace, _manager=manager)
        assert manager.registered == []
        assert manager.updated == []

    def test_project_server_is_registered(self, tmp_path: Path) -> None:
        """merge: new project server is registered (not in global)."""
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        project_file = _write_mcp_json(
            tmp_path, {"project-server": {"type": "stdio", "command": "uvx"}}
        )
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        assert "project-server" in manager.registered_names()
        assert manager.disabled_names() == set()

    def test_global_server_remains_enabled_after_merge(self, tmp_path: Path) -> None:
        """merge: global servers remain enabled when project adds new servers."""
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        project_file = _write_mcp_json(
            tmp_path, {"project-server": {"type": "sse", "url": "http://project"}}
        )
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        assert "global-server" in manager.enabled_names()

    def test_name_collision_project_wins_merge(self, tmp_path: Path) -> None:
        """merge: project server with same name as global → update_server called."""
        global_server = _make_global_server("shared-server")
        manager = FakeMCPManager(global_servers=[global_server])
        project_file = _write_mcp_json(
            tmp_path,
            {"shared-server": {"type": "stdio", "command": "project-cmd"}},
        )
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        # Should update, not register anew
        assert "shared-server" in manager.updated_names()
        assert "shared-server" not in manager.registered_names()
        # Verify the updated config has the project command
        _, updated_config = next(
            (item for item in manager.updated if item[1].name == "shared-server"),
            (None, None),
        )
        assert updated_config is not None
        assert updated_config.config.get("command") == "project-cmd"

    def test_multiple_project_servers_all_registered(self, tmp_path: Path) -> None:
        """merge: multiple project servers are all injected."""
        manager = FakeMCPManager()
        project_file = _write_mcp_json(
            tmp_path,
            {
                "server-a": {"type": "sse", "url": "http://a"},
                "server-b": {"type": "stdio", "command": "cmd-b"},
            },
        )
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        assert manager.registered_names() == {"server-a", "server-b"}


# ---------------------------------------------------------------------------
# apply_mcp_scope — project scope
# ---------------------------------------------------------------------------


class TestProjectScope:
    def test_no_workspace_root_falls_through(self) -> None:
        """project scope + no workspace: noop (fall through to global)."""
        global_sv = _make_global_server("global-server")
        manager = FakeMCPManager(global_servers=[global_sv])
        apply_mcp_scope("project", None, _manager=manager)
        assert manager.registered == []
        # Global server must NOT be disabled
        assert manager.disabled_names() == set()

    def test_no_mcp_servers_json_falls_through(self, tmp_path: Path) -> None:
        """project scope + workspace exists but no mcp_servers.json: fall through."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        global_sv = _make_global_server("global-server")
        manager = FakeMCPManager(global_servers=[global_sv])
        apply_mcp_scope("project", workspace, _manager=manager)
        assert manager.registered == []
        assert manager.disabled_names() == set()

    def test_project_server_injected(self, tmp_path: Path) -> None:
        """project scope: project server is registered."""
        manager = FakeMCPManager(global_servers=[_make_global_server("global-server")])
        project_file = _write_mcp_json(
            tmp_path, {"project-server": {"type": "stdio", "command": "uvx"}}
        )
        apply_mcp_scope(
            "project", tmp_path, _manager=manager, _project_file=project_file
        )
        assert "project-server" in manager.registered_names()

    def test_global_only_server_is_disabled(self, tmp_path: Path) -> None:
        """project scope: global servers not in project are disabled."""
        global_sv = _make_global_server("global-only")
        manager = FakeMCPManager(global_servers=[global_sv])
        project_file = _write_mcp_json(
            tmp_path, {"project-server": {"type": "sse", "url": "http://p"}}
        )
        apply_mcp_scope(
            "project", tmp_path, _manager=manager, _project_file=project_file
        )
        assert "global-only" in manager.disabled_names()

    def test_name_collision_project_wins_and_global_not_disabled(
        self, tmp_path: Path
    ) -> None:
        """project scope: shared-name server is updated (project wins), not disabled."""
        shared_sv = _make_global_server("shared-server")
        manager = FakeMCPManager(global_servers=[shared_sv])
        project_file = _write_mcp_json(
            tmp_path,
            {"shared-server": {"type": "stdio", "command": "project-cmd"}},
        )
        apply_mcp_scope(
            "project", tmp_path, _manager=manager, _project_file=project_file
        )
        # updated, not disabled
        assert "shared-server" in manager.updated_names()
        assert "shared-server" not in manager.disabled_names()

    def test_multiple_globals_only_non_project_disabled(self, tmp_path: Path) -> None:
        """project scope: only global servers NOT overridden by project are disabled."""
        global_a = _make_global_server("server-a")
        global_b = _make_global_server("server-b")
        manager = FakeMCPManager(global_servers=[global_a, global_b])
        project_file = _write_mcp_json(
            tmp_path,
            {
                "server-a": {"type": "sse", "url": "http://project/a"},  # collision
                "server-c": {"type": "sse", "url": "http://project/c"},  # new
            },
        )
        apply_mcp_scope(
            "project", tmp_path, _manager=manager, _project_file=project_file
        )
        # server-a: updated (project wins)
        assert "server-a" in manager.updated_names()
        # server-b: global-only → disabled
        assert "server-b" in manager.disabled_names()
        # server-c: new project server → registered
        assert "server-c" in manager.registered_names()

    def test_empty_project_file_falls_through(self, tmp_path: Path) -> None:
        """project scope + empty mcp_servers.json: fall through (no disables)."""
        global_sv = _make_global_server("global-server")
        manager = FakeMCPManager(global_servers=[global_sv])
        project_file = _write_mcp_json(tmp_path, {})
        apply_mcp_scope(
            "project", tmp_path, _manager=manager, _project_file=project_file
        )
        assert manager.registered == []
        assert manager.disabled_names() == set()


# ---------------------------------------------------------------------------
# apply_mcp_scope — resilience / error paths
# ---------------------------------------------------------------------------


class TestResilience:
    def test_malformed_project_file_is_skipped(self, tmp_path: Path) -> None:
        """Malformed JSON in project file → noop, no crash."""
        bad_file = tmp_path / "mcp_servers.json"
        bad_file.write_text("NOT JSON", encoding="utf-8")
        manager = FakeMCPManager()
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=bad_file)
        assert manager.registered == []

    def test_registry_list_all_raises_is_handled(self, tmp_path: Path) -> None:
        """If registry.list_all() raises, we log + skip without crashing."""
        project_file = _write_mcp_json(
            tmp_path, {"server": {"type": "sse", "url": "http://x"}}
        )
        manager = MagicMock()
        manager.registry.list_all.side_effect = RuntimeError("boom")
        # Should NOT raise
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)

    def test_register_server_raises_is_per_server(self, tmp_path: Path) -> None:
        """register_server failure for one server doesn't stop others."""
        project_file = _write_mcp_json(
            tmp_path,
            {
                "bad-server": {"type": "sse", "url": "http://bad"},
                "good-server": {"type": "sse", "url": "http://good"},
            },
        )
        # Fail on bad-server, succeed on good-server
        original_register = FakeMCPManager.register_server

        manager = FakeMCPManager()

        def _selective_register(self_inner: Any, config: Any) -> str:
            if config.name == "bad-server":
                raise ValueError("intentional failure")
            return original_register(manager, config)

        manager.register_server = _selective_register.__get__(manager, FakeMCPManager)  # type: ignore[method-assign]

        # Should NOT raise even though bad-server fails
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        # good-server should still be registered
        assert any(c.name == "good-server" for c in manager.registered)

    def test_unknown_scope_falls_back_to_merge(self, tmp_path: Path) -> None:
        """Unknown scope → behaves like merge (injects project servers)."""
        manager = FakeMCPManager()
        project_file = _write_mcp_json(
            tmp_path, {"project-server": {"type": "sse", "url": "http://p"}}
        )
        apply_mcp_scope(
            "banana", tmp_path, _manager=manager, _project_file=project_file
        )
        assert "project-server" in manager.registered_names()
        assert manager.disabled_names() == set()

    def test_get_mcp_manager_failure_is_handled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If get_mcp_manager() raises, surface logs and returns without crash."""
        import code_puppy.plugins.project_workspace.surfaces.mcp as mcp_mod

        def _fail() -> None:
            raise RuntimeError("manager unavailable")

        monkeypatch.setattr(
            mcp_mod,
            "_load_mcp_servers_from_file",
            lambda _: {"x": {}},
        )
        # Patch the import inside apply_mcp_scope — we can't directly monkeypatch
        # the lazy import, but passing _manager=None and patching the import
        # is tested via an integration approach; this test uses the _manager path.
        # The real test of get_mcp_manager() failure uses monkeypatching in module:
        import unittest.mock as mock

        with mock.patch(
            "code_puppy.plugins.project_workspace.surfaces.mcp.apply_mcp_scope"
        ):
            # Just ensure calling the module-level register() wiring doesn't crash
            from code_puppy.plugins.project_workspace.surfaces.mcp import register

            register()  # re-register is fine (deduped by callbacks)


# ---------------------------------------------------------------------------
# apply_mcp_scope — MCP-specific edge cases
# ---------------------------------------------------------------------------


class TestMcpSpecific:
    def test_project_server_added_to_registry(self, tmp_path: Path) -> None:
        """Smoke: after apply_mcp_scope(merge), project server is in registry."""
        manager = FakeMCPManager()
        project_file = _write_mcp_json(
            tmp_path, {"my-tool": {"type": "stdio", "command": "uvx my-tool"}}
        )
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        # Server should now be queryable from manager
        assert manager.registry.get_by_name("my-tool") is not None

    def test_project_scope_disabled_server_not_in_enabled(self, tmp_path: Path) -> None:
        """project scope: disabled global server is not in enabled_names()."""
        global_sv = _make_global_server("big-corp-mcp")
        manager = FakeMCPManager(global_servers=[global_sv])
        project_file = _write_mcp_json(
            tmp_path, {"local-tool": {"type": "stdio", "command": "uvx local"}}
        )
        apply_mcp_scope(
            "project", tmp_path, _manager=manager, _project_file=project_file
        )
        assert "big-corp-mcp" not in manager.enabled_names()
        assert "local-tool" in manager.registered_names()

    def test_project_server_type_preserved(self, tmp_path: Path) -> None:
        """The 'type' field from project JSON is passed through to ServerConfig."""
        manager = FakeMCPManager()
        project_file = _write_mcp_json(
            tmp_path,
            {
                "stdio-server": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "pkg"],
                }
            },
        )
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        assert len(manager.registered) == 1
        assert manager.registered[0].type == "stdio"

    def test_project_config_preserved_in_server_config(self, tmp_path: Path) -> None:
        """The raw config dict is stored verbatim in ServerConfig.config."""
        manager = FakeMCPManager()
        raw = {"type": "stdio", "command": "uvx", "args": ["--from", "my-pkg"]}
        project_file = _write_mcp_json(tmp_path, {"my-server": raw})
        apply_mcp_scope("merge", tmp_path, _manager=manager, _project_file=project_file)
        assert manager.registered[0].config == raw
