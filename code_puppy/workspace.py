"""code_puppy.workspace — project-workspace root discovery.

Public API
----------
    discover_root(cwd: Path | None = None) -> Path | None

Walk-up algorithm
-----------------
Starting from *cwd* (default: CWD at import time), walk up the directory
tree looking for a ``.code_puppy/`` subdirectory.

Stopping rules
~~~~~~~~~~~~~~
* **Found**: `.code_puppy/` is a directory in the current candidate → return
  the candidate directory.
* **Git boundary**: ``.git/`` is found in the current candidate WITHOUT
  ``.code_puppy/`` → return ``None``.  If both are present in the SAME
  directory, ``.code_puppy/`` check runs first so the root IS returned.
* **Filesystem root**: reached ``/`` without finding either → return ``None``.

Module-level constant
---------------------
``_INITIAL_CWD`` is captured once at import time.  ``discover_root()``
defaults to this, making workspace detection stable even if ``os.chdir()``
is called later in the session.

Examples
--------
::

    # .code_puppy/ lives at the repo root, CWD is a nested sub-dir
    >>> discover_root(Path("/home/user/repo/src/deep"))
    Path('/home/user/repo')

    # No .code_puppy/ anywhere above CWD
    >>> discover_root(Path("/tmp/no-project"))
    None
"""

from __future__ import annotations

from pathlib import Path

# Captured once at import time — before any os.chdir() could be called.
_INITIAL_CWD: Path = Path.cwd()


def discover_root(cwd: Path | None = None) -> Path | None:
    """Return the nearest ancestor directory that contains a ``.code_puppy/`` dir.

    Args:
        cwd: Starting directory for the walk.  Defaults to ``_INITIAL_CWD``
             (the process CWD at the time this module was first imported).

    Returns:
        The directory that contains ``.code_puppy/`` (a ``Path`` object), or
        ``None`` if no such directory is found before the ``.git/`` boundary
        or the filesystem root.
    """
    start = (cwd if cwd is not None else _INITIAL_CWD).resolve()
    current = start

    while True:
        code_puppy_dir = current / ".code_puppy"
        git_dir = current / ".git"

        # Check .code_puppy/ first — handles the case where both .code_puppy/
        # and .git/ exist in the SAME directory (repo root with a workspace).
        if code_puppy_dir.is_dir():
            return current

        # Git boundary: don't cross into a parent repo.
        if git_dir.exists():
            return None

        parent = current.parent
        if parent == current:
            # Reached filesystem root — no .code_puppy/ found anywhere.
            return None

        current = parent
