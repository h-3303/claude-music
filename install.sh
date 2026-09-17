#!/usr/bin/env bash
# Installs the Nicotine+ MCP bridge plugin and the claude-music Claude Code plugin for the current user.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$(pwd)

if [[ -d ~/.var/app/org.nicotine_plus.Nicotine ]] && ! command -v nicotine >/dev/null; then
  PLUGIN_DIR=~/.var/app/org.nicotine_plus.Nicotine/data/nicotine/plugins   # Flatpak
else
  PLUGIN_DIR=${XDG_DATA_HOME:-~/.local/share}/nicotine/plugins
fi

command -v uv >/dev/null || { echo "uv is required (pacman -S uv)"; exit 1; }

# 1. Nicotine+ plugin
mkdir -p "$PLUGIN_DIR" ~/.local/bin
rm -rf "$PLUGIN_DIR/mcp_bridge"
cp -r nicotine-plugin/mcp_bridge "$PLUGIN_DIR/"
echo "Nicotine+ plugin installed to $PLUGIN_DIR/mcp_bridge"

# 2. Standalone server for Claude Desktop and other MCP clients
install -m 755 plugins/claude-music/servers/nicotine_mcp.py ~/.local/bin/nicotine-mcp
uv sync --script ~/.local/bin/nicotine-mcp   # pre-fetch the MCP SDK so first launch is instant
echo "Standalone server installed as ~/.local/bin/nicotine-mcp"

# 3. Claude Code plugin (local marketplace, loaded in place from this checkout)
if command -v claude >/dev/null; then
  # The plugin ships its own "nicotine" server; drop the hand-registered one from earlier versions.
  claude mcp remove --scope user nicotine >/dev/null 2>&1 || true

  if ! claude plugin marketplace add "$REPO" >/dev/null 2>&1; then
    claude plugin marketplace update claude-music >/dev/null
  fi

  claude plugin install claude-music@claude-music
  # Build the library server's venv now (it lives in the plugin's persistent data dir)
  DATA_DIR=~/.claude/plugins/data/claude-music@claude-music
  mkdir -p "$DATA_DIR"
  UV_PROJECT_ENVIRONMENT="$DATA_DIR/venv" uv sync --project plugins/claude-music/servers/library >/dev/null
  echo "Claude Code plugin 'claude-music' installed from $REPO"
else
  echo "Claude Code not found; for other MCP clients register: uv run --script $HOME/.local/bin/nicotine-mcp"
fi

echo
echo "Now: Nicotine+ → Preferences → Plugins → enable plugins → tick 'MCP Bridge'."
echo "Then in Claude Code: /plugin to check 'claude-music', and /playlist-sync to start."
