"""Plugins surface — Phase E documentation stub.

See docs/workspace-plugin-design.md § Surface Integration Plan → 3. Plugins Surface
and beads ticket code_puppy-rv8.

True plugin-tier scoping (suppressing the user-plugin tier) is NOT achievable
from within a builtin plugin because ``plugins/__init__.py`` loads all tiers
unconditionally before any ``register_callbacks.py`` has a chance to run.

The proposed core change (RISK-2 in the design doc) deferred to Phase E:
  - Add ``CODE_PUPPY_SKIP_USER_PLUGINS=1`` env-var check in
    ``plugins/__init__.py`` before ``_load_user_plugins()`` (~2–4 LOC)
  - Add a pre-plugin config reader in ``__main__.py`` / ``main.py`` (~5–10 LOC)

Decision: deferred — see RISK-2 and Phase E ticket.
"""
