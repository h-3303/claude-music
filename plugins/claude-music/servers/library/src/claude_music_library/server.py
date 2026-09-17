# SPDX-License-Identifier: GPL-3.0-or-later
"""Stdio MCP server entry point (tool surface is filled in during Phase 1)."""

from mcp.server.mcpserver import MCPServer

from . import __version__

mcp = MCPServer(name="library", instructions="claude-music library server")


@mcp.tool()
def library_version() -> dict:
    """Version of the claude-music library server."""
    return {"version": __version__}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
