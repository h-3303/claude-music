"""The Claude Code plugin and marketplace manifests must validate strictly."""

import json
import shutil
import subprocess

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "claude-music"

needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")


@needs_claude
@pytest.mark.parametrize("target", [PLUGIN, REPO])
def test_manifests_validate_strictly(target):
    proc = subprocess.run(
        ["claude", "plugin", "validate", str(target), "--strict"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_marketplace_points_at_plugin():
    marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = marketplace["plugins"]
    assert entry["name"] == "claude-music"
    assert (REPO / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_mcp_config_references_existing_files():
    config = json.loads((PLUGIN / ".mcp.json").read_text())
    servers = config["mcpServers"]
    assert set(servers) == {"nicotine", "library"}

    for server in servers.values():
        for arg in server["args"]:
            if "${CLAUDE_PLUGIN_ROOT}" in arg:
                assert (PLUGIN / arg.replace("${CLAUDE_PLUGIN_ROOT}/", "")).exists(), arg
