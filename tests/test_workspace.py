"""Tests for code_puppy.workspace.discover_root().

All tests use tmp_path for isolation and plant a .git/ boundary
at tmp_path so the walk never escapes into the real filesystem.
"""

from __future__ import annotations

from pathlib import Path


from code_puppy.workspace import discover_root


def test_discover_root_in_cwd(tmp_path: Path) -> None:
    """.code_puppy/ exists in the start directory → returns that directory."""
    (tmp_path / ".git").mkdir()  # boundary guard
    (tmp_path / ".code_puppy").mkdir()
    result = discover_root(tmp_path)
    assert result == tmp_path


def test_discover_root_three_levels_up_with_git_at_same_level(
    tmp_path: Path,
) -> None:
    """.code_puppy/ and .git/ are co-located at the ancestor → returns ancestor.

    .code_puppy/ check happens first, so both coexisting should be found.
    """
    (tmp_path / ".code_puppy").mkdir()
    (tmp_path / ".git").mkdir()  # same level as .code_puppy/ — should NOT block
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    result = discover_root(nested)
    assert result == tmp_path


def test_discover_root_blocked_by_git_boundary(tmp_path: Path) -> None:
    """.git/ is 1 level up, .code_puppy/ is at the root → returns None.

    The .git/ boundary is between the start dir and the .code_puppy/ location,
    so discovery should stop and return None.
    """
    # .code_puppy/ at tmp_path (top level)
    (tmp_path / ".code_puppy").mkdir()

    # .git/ at the mid level — acts as a boundary
    mid = tmp_path / "a"
    mid.mkdir()
    (mid / ".git").mkdir()

    # CWD is nested below mid — must not cross mid's .git/ to reach root
    nested = mid / "b" / "c"
    nested.mkdir(parents=True)

    result = discover_root(nested)
    assert result is None


def test_discover_root_no_code_puppy_anywhere(tmp_path: Path) -> None:
    """No .code_puppy/ in the tree → returns None."""
    # .git/ at tmp_path stops the walk so we never escape to the real FS
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "x" / "y"
    nested.mkdir(parents=True)
    result = discover_root(nested)
    assert result is None


def test_discover_root_file_not_directory(tmp_path: Path) -> None:
    """.code_puppy exists but is a FILE, not a directory → returns None."""
    (tmp_path / ".git").mkdir()  # boundary guard
    (tmp_path / ".code_puppy").write_text("not a dir")
    result = discover_root(tmp_path)
    assert result is None
