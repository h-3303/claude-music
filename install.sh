#!/usr/bin/env bash
# Installs the Nicotine+ MCP bridge for the current user.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -d ~/.var/app/org.nicotine_plus.Nicotine ]] && ! command -v nicotine >/dev/null; then
  PLUGIN_DIR=~/.var/app/org.nicotine_plus.Nicotine/data/nicotine/plugins   # Flatpak
else
  PLUGIN_DIR=${XDG_DATA_HOME:-~/.local/share}/nicotine/plugins
fi

mkdir -p "$PLUGIN_DIR" ~/.local/bin
rm -rf "$PLUGIN_DIR/mcp_bridge"
cp -r plugin/mcp_bridge "$PLUGIN_DIR/"
install -m 755 server/nicotine_mcp.py ~/.local/bin/nicotine-mcp
uv sync --script ~/.local/bin/nicotine-mcp   # pre-fetch the MCP SDK so first launch is instant

if command -v claude >/dev/null; then
  claude mcp remove --scope user nicotine >/dev/null 2>&1 || true
  claude mcp add --scope user nicotine -- uv run --script "$HOME/.local/bin/nicotine-mcp"
else
  echo "Claude Code not found; register manually: uv run --script $HOME/.local/bin/nicotine-mcp"
fi

echo "Plugin installed to $PLUGIN_DIR/mcp_bridge"
echo "Now: Nicotine+ → Preferences → Plugins → enable plugins → tick 'MCP Bridge'."
