"""File permissions surface — Phase H implementation placeholder.

See docs/workspace-plugin-design.md § Surface Integration Plan → 5. File Permissions Surface
and beads ticket code_puppy-9w1.

This module will register a ``file_permission`` callback that:
  - For ``project`` scope: auto-restricts to the project root directory
  - Optionally loads ``.code_puppy/file_policy.json`` for explicit allow/deny rules
  - Returns ``False`` (deny) for out-of-scope paths, ``True`` (allow) otherwise
"""
