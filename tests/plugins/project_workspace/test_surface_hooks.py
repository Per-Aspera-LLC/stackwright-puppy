"""Tests for code_puppy.plugins.project_workspace.surfaces.hooks.

Covers:
- _load_project_hooks: missing file, valid bare format, valid wrapped format,
  malformed JSON, non-dict top-level value, unreadable file
- apply_hooks_scope / merge: project hooks added to existing engine
- apply_hooks_scope / merge: collision (same event) → additive (both hooks in engine)
- apply_hooks_scope / merge: no workspace root → noop (engine unchanged)
- apply_hooks_scope / merge: no hooks file → noop (engine unchanged)
- apply_hooks_scope / merge: engine is None → no crash
- apply_hooks_scope / project: engine reloaded with only project hooks
- apply_hooks_scope / project: no workspace root → noop (engine unchanged)
- apply_hooks_scope / project: no hooks file → noop (engine unchanged)
- apply_hooks_scope / project: engine is None → no crash
- apply_hooks_scope / global: noop regardless of hooks file content
- apply_hooks_scope / unknown scope: falls back to merge (adds project hooks)
- apply_hooks_scope: malformed JSON in hooks file → logged + skipped, no crash
- apply_hooks_scope: hooks file with malformed hook command → logged + skipped,
  no crash (subprocess safety — load-time path is safe)
- apply_hooks_scope: reload_config raises → logged + skipped, no crash
- apply_hooks_scope / merge: wrapped-format hooks file parsed correctly
- apply_hooks_scope / merge: disabled hook in project file is still added
  to engine (config copy, not runtime filter)
- apply_hooks_scope / project: falls through gracefully with no config
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


from code_puppy.hook_engine import HookConfig, HookEngine
from code_puppy.plugins.project_workspace.surfaces.hooks import (
    _add_hooks_from_config,
    _get_cch_engine,
    _load_project_hooks,
    apply_hooks_scope,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_hook_config(
    event_type: str,
    *,
    matcher: str = "*",
    command: str = "echo test",
    timeout: int = 5000,
    enabled: bool = True,
) -> dict[str, Any]:
    """Return a minimal hooks.json snippet for one event type / hook."""
    return {
        event_type: [
            {
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": timeout,
                        "enabled": enabled,
                    }
                ],
            }
        ]
    }


def _make_hooks_file(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    wrapped: bool = False,
) -> Path:
    """Write a hooks.json to tmp_path and return its path."""
    hooks_dir = tmp_path / ".code_puppy"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    file_path = hooks_dir / "hooks.json"
    payload = {"hooks": config} if wrapped else config
    file_path.write_text(json.dumps(payload), encoding="utf-8")
    return file_path


def _engine_with_global_hooks() -> HookEngine:
    """Return a HookEngine pre-loaded with one global PreToolUse hook."""
    global_config = _make_hook_config("PreToolUse", command="echo global", matcher="*")
    return HookEngine(global_config, strict_validation=False)


# ---------------------------------------------------------------------------
# _load_project_hooks
# ---------------------------------------------------------------------------


class TestLoadProjectHooks:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = _load_project_hooks(tmp_path / "nonexistent.json")
        assert result is None

    def test_valid_bare_format(self, tmp_path: Path) -> None:
        config = _make_hook_config("PreToolUse", command="echo ok")
        fp = _make_hooks_file(tmp_path, config)
        result = _load_project_hooks(fp)
        assert result is not None
        assert "PreToolUse" in result

    def test_valid_wrapped_format(self, tmp_path: Path) -> None:
        config = _make_hook_config("PostToolUse", command="echo wrapped")
        fp = _make_hooks_file(tmp_path, config, wrapped=True)
        result = _load_project_hooks(fp)
        assert result is not None
        assert "PostToolUse" in result

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        fp = tmp_path / "bad.json"
        fp.write_text("{not: valid json}", encoding="utf-8")
        result = _load_project_hooks(fp)
        assert result is None

    def test_non_dict_top_level_returns_none(self, tmp_path: Path) -> None:
        fp = tmp_path / "array.json"
        fp.write_text('["a", "b"]', encoding="utf-8")
        result = _load_project_hooks(fp)
        assert result is None

    def test_empty_object_returns_empty_dict(self, tmp_path: Path) -> None:
        fp = tmp_path / "empty.json"
        fp.write_text("{}", encoding="utf-8")
        result = _load_project_hooks(fp)
        # Empty dict is returned (truthy-ness checked by caller)
        assert result == {}

    def test_read_error_returns_none(self, tmp_path: Path) -> None:
        fp = tmp_path / "hooks.json"
        fp.write_text("{}", encoding="utf-8")
        with patch("pathlib.Path.read_text", side_effect=OSError("nope")):
            result = _load_project_hooks(fp)
        assert result is None


# ---------------------------------------------------------------------------
# _get_cch_engine
# ---------------------------------------------------------------------------


class TestGetCchEngine:
    def test_returns_override_when_provided(self) -> None:
        fake_engine = MagicMock()
        result = _get_cch_engine(fake_engine)
        assert result is fake_engine

    def test_returns_none_when_override_is_none_and_import_fails(self) -> None:
        with patch.dict("sys.modules", {"code_puppy.plugins.claude_code_hooks": None}):
            # Simulate import error
            with patch(
                "code_puppy.plugins.project_workspace.surfaces.hooks._get_cch_engine"
            ) as mock_fn:
                mock_fn.return_value = None
                result = _get_cch_engine(None)
        assert result is None

    def test_returns_none_when_engine_attr_missing(self) -> None:
        fake_module = MagicMock(spec=[])  # no _hook_engine attribute
        with patch.dict(
            "sys.modules",
            {"code_puppy.plugins.claude_code_hooks.register_callbacks": fake_module},
        ):
            result = _get_cch_engine(None)
        # getattr with default None returns None when attr doesn't exist
        assert result is None


# ---------------------------------------------------------------------------
# _add_hooks_from_config
# ---------------------------------------------------------------------------


class TestAddHooksFromConfig:
    def test_adds_hooks_to_engine(self) -> None:
        engine = HookEngine({}, strict_validation=False)
        config = _make_hook_config("PreToolUse", command="echo add")
        count = _add_hooks_from_config(engine, config)
        assert count == 1
        assert engine.count_hooks("PreToolUse") == 1

    def test_adds_multiple_event_types(self) -> None:
        engine = HookEngine({}, strict_validation=False)
        config = {
            **_make_hook_config("PreToolUse", command="echo pre"),
            **_make_hook_config("PostToolUse", command="echo post"),
        }
        count = _add_hooks_from_config(engine, config)
        assert count == 2
        assert engine.count_hooks("PreToolUse") == 1
        assert engine.count_hooks("PostToolUse") == 1

    def test_skips_invalid_hook_entries_no_crash(self) -> None:
        engine = HookEngine({}, strict_validation=False)
        # This entry has no "command" — build_registry_from_config skips it
        bad_config: dict[str, Any] = {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": ""}],
                }
            ]
        }
        count = _add_hooks_from_config(engine, bad_config)
        # Invalid hook skipped — no crash
        assert count == 0

    def test_engine_add_hook_raises_logs_and_continues(self) -> None:
        """Per-hook add errors are caught; other hooks in the config continue."""
        engine = HookEngine({}, strict_validation=False)
        config: dict[str, Any] = {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo ok"},
                        {"type": "command", "command": "echo also-ok"},
                    ],
                }
            ]
        }
        original_add = engine.add_hook
        call_count = [0]

        def flaky_add(event_type: str, hook: HookConfig) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated failure")
            original_add(event_type, hook)

        engine.add_hook = flaky_add  # type: ignore[method-assign]
        count = _add_hooks_from_config(engine, config)
        # First call raised, second succeeded
        assert count == 1

    def test_build_registry_raises_returns_zero(self) -> None:
        engine = HookEngine({}, strict_validation=False)
        with patch(
            "code_puppy.hook_engine.registry.build_registry_from_config",
            side_effect=RuntimeError("boom"),
        ):
            count = _add_hooks_from_config(engine, {"PreToolUse": []})
        assert count == 0


# ---------------------------------------------------------------------------
# apply_hooks_scope — merge
# ---------------------------------------------------------------------------


class TestApplyHooksScopeMerge:
    def test_merge_adds_project_hooks(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks("PreToolUse")

        project_config = _make_hook_config("PreToolUse", command="echo project")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # Additive: global hook + project hook
        assert engine.count_hooks("PreToolUse") == initial_count + 1

    def test_merge_different_event_types(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()  # has PreToolUse
        project_config = _make_hook_config("PostToolUse", command="echo post-project")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # PreToolUse unchanged; PostToolUse added
        assert engine.count_hooks("PreToolUse") == 1
        assert engine.count_hooks("PostToolUse") == 1

    def test_merge_same_event_is_additive_not_replace(self, tmp_path: Path) -> None:
        """Both global hook and project hook coexist (merge = additive)."""
        engine = _engine_with_global_hooks()  # 1 PreToolUse hook
        project_config = _make_hook_config("PreToolUse", command="echo project-pre")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # 2 hooks after merge (NOT a replacement)
        assert engine.count_hooks("PreToolUse") == 2

    def test_merge_no_workspace_root_noop(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        apply_hooks_scope("merge", None, _cch_engine=engine)

        assert engine.count_hooks() == initial_count

    def test_merge_no_hooks_file_noop(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()
        absent_file = tmp_path / ".code_puppy" / "hooks.json"

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=absent_file,
        )

        assert engine.count_hooks() == initial_count

    def test_merge_engine_none_no_crash(self, tmp_path: Path) -> None:
        project_config = _make_hook_config("PreToolUse", command="echo test")
        project_file = _make_hooks_file(tmp_path, project_config)

        # Should not raise even with no engine
        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=None,
            _project_file=project_file,
        )

    def test_merge_wrapped_format_hooks(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        project_config = _make_hook_config("SessionStart", command="echo session")
        project_file = _make_hooks_file(tmp_path, project_config, wrapped=True)

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        assert engine.count_hooks("SessionStart") == 1

    def test_merge_disabled_hook_still_added_to_registry(self, tmp_path: Path) -> None:
        """Disabled hooks are part of the config — they should be copied."""
        engine = HookEngine({}, strict_validation=False)
        # A disabled hook — HookConfig(enabled=False)
        project_config: dict[str, Any] = {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo disabled",
                            "enabled": False,
                        }
                    ],
                }
            ]
        }
        project_file = tmp_path / "hooks.json"
        project_file.write_text(json.dumps(project_config), encoding="utf-8")

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # count_hooks counts ALL hooks (enabled or not)
        assert engine.count_hooks("PreToolUse") == 1


# ---------------------------------------------------------------------------
# apply_hooks_scope — project
# ---------------------------------------------------------------------------


class TestApplyHooksScopeProject:
    def test_project_replaces_engine_with_project_hooks(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()  # global hook: "echo global"
        project_config = _make_hook_config("PreToolUse", command="echo project-only")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "project",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        hooks = engine.registry.get_hooks_for_event("PreToolUse")
        assert len(hooks) == 1
        assert hooks[0].command == "echo project-only"

    def test_project_scope_suppresses_global_hooks(self, tmp_path: Path) -> None:
        """After reload, the old global hook should be gone."""
        global_config = {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo global-hook"}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo global-post"}],
                }
            ],
        }
        engine = HookEngine(global_config, strict_validation=False)
        assert engine.count_hooks("PostToolUse") == 1  # sanity

        project_config = _make_hook_config("PreToolUse", command="echo project-pre")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "project",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # PreToolUse: only project hook
        pre_hooks = engine.registry.get_hooks_for_event("PreToolUse")
        assert len(pre_hooks) == 1
        assert pre_hooks[0].command == "echo project-pre"
        # PostToolUse: gone (was global, not in project)
        assert engine.count_hooks("PostToolUse") == 0

    def test_project_no_workspace_root_noop(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        apply_hooks_scope("project", None, _cch_engine=engine)

        assert engine.count_hooks() == initial_count

    def test_project_no_hooks_file_fallthrough_noop(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()
        absent_file = tmp_path / ".code_puppy" / "hooks.json"

        apply_hooks_scope(
            "project",
            tmp_path,
            _cch_engine=engine,
            _project_file=absent_file,
        )

        assert engine.count_hooks() == initial_count

    def test_project_engine_none_no_crash(self, tmp_path: Path) -> None:
        project_config = _make_hook_config("PreToolUse", command="echo test")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "project",
            tmp_path,
            _cch_engine=None,
            _project_file=project_file,
        )

    def test_project_reload_config_raises_no_crash(self, tmp_path: Path) -> None:
        engine = MagicMock()
        engine.reload_config.side_effect = RuntimeError("boom")

        project_config = _make_hook_config("PreToolUse", command="echo test")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "project",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )
        # No exception propagated


# ---------------------------------------------------------------------------
# apply_hooks_scope — global
# ---------------------------------------------------------------------------


class TestApplyHooksScopeGlobal:
    def test_global_is_noop(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        project_config = _make_hook_config("PreToolUse", command="echo project")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "global",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        assert engine.count_hooks() == initial_count

    def test_global_no_workspace_still_noop(self) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        apply_hooks_scope("global", None, _cch_engine=engine)

        assert engine.count_hooks() == initial_count


# ---------------------------------------------------------------------------
# apply_hooks_scope — error handling + safety
# ---------------------------------------------------------------------------


class TestApplyHooksScopeErrorHandling:
    def test_malformed_json_no_crash(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{this is not json}", encoding="utf-8")

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=bad_file,
        )

        # Engine unchanged — malformed file silently skipped
        assert engine.count_hooks() == initial_count

    def test_subprocess_safety_malformed_command_no_crash(self, tmp_path: Path) -> None:
        """Malformed hook command in hooks.json must not crash at load time.

        Our load-time path uses build_registry_from_config which silently
        skips entries with empty commands.  The subprocess runner only sees
        hooks at execution time (not our concern here).
        """
        engine = HookEngine({}, strict_validation=False)
        bad_hook_config: dict[str, Any] = {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        # Empty command — build_registry_from_config skips this
                        {"type": "command", "command": ""},
                        # Valid command alongside bad one
                        {"type": "command", "command": "echo valid"},
                    ],
                }
            ]
        }
        project_file = tmp_path / "hooks.json"
        project_file.write_text(json.dumps(bad_hook_config), encoding="utf-8")

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # Valid hook was added; bad one was skipped — no crash
        assert engine.count_hooks("PreToolUse") == 1

    def test_unknown_scope_falls_back_to_merge(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        project_config = _make_hook_config("PreToolUse", command="echo fallback")
        project_file = _make_hooks_file(tmp_path, project_config)

        apply_hooks_scope(
            "unknown-scope",
            tmp_path,
            _cch_engine=engine,
            _project_file=project_file,
        )

        # Fell back to merge → hook was added
        assert engine.count_hooks("PreToolUse") == initial_count + 1

    def test_empty_hooks_file_noop(self, tmp_path: Path) -> None:
        engine = _engine_with_global_hooks()
        initial_count = engine.count_hooks()

        empty_file = tmp_path / "hooks.json"
        empty_file.write_text("{}", encoding="utf-8")

        apply_hooks_scope(
            "merge",
            tmp_path,
            _cch_engine=engine,
            _project_file=empty_file,
        )

        # Empty config → no hooks added
        assert engine.count_hooks() == initial_count


# ---------------------------------------------------------------------------
# apply_hooks_scope — inject via _project_file (no real filesystem for scope)
# ---------------------------------------------------------------------------


class TestApplyHooksScopeWithExplicitFile:
    """Verify that _project_file overrides workspace_root resolution."""

    def test_explicit_project_file_takes_precedence_over_workspace_root(
        self, tmp_path: Path
    ) -> None:
        engine = HookEngine({}, strict_validation=False)
        config = _make_hook_config("SessionStart", command="echo explicit")
        explicit_file = tmp_path / "explicit_hooks.json"
        explicit_file.write_text(json.dumps(config), encoding="utf-8")

        # workspace_root points somewhere else; _project_file is authoritative
        apply_hooks_scope(
            "merge",
            Path("/some/other/root"),
            _cch_engine=engine,
            _project_file=explicit_file,
        )

        assert engine.count_hooks("SessionStart") == 1
