# Changes from upstream code-puppy

Base: `code-puppy@0.0.574` — https://github.com/mpfaffenberger/code_puppy

## Fork identity only

The only divergence from upstream is in `pyproject.toml` — package name
(`stackwright-puppy`), description, repository URLs
(`Per-Aspera-LLC/stackwright-pro`), and the `raft-puppy` entry point alias.
No logic changes.

## Version detection cascade

`code_puppy/__init__.py` and `code_puppy/pydantic_patches.py` try
`importlib.metadata.version("stackwright-puppy")` first, then fall back to
`"code-puppy"`, then `"0.0.0-dev"`. Upstream only has the `"code-puppy"` lookup.

When rebasing onto a new upstream release, these two files may be clobbered.
Cherry-pick the commit titled
`fix: version cascade for stackwright-puppy fork identity` to restore correct
`raft-puppy --version` output.

## Future work

A project-workspace plugin (v2 — replacing the abandoned PR #355 cherry-picks)
is planned. See beads issues tagged `workspace-v2` and the `bd memories
workspace-v2-design` note for the full architecture design.
