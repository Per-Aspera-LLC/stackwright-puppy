"""MCP surface — Phase G implementation placeholder.

See docs/workspace-plugin-design.md § Surface Integration Plan → 4. MCP Surface
and beads ticket code_puppy-z1q.

This module will register a ``startup`` callback that:
  - Loads ``.code_puppy/mcp_servers.json``
  - Injects project MCP servers into the MCPManager registry
  - For ``project`` scope: disables global servers via registry.update()
"""
