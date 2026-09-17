# SPDX-License-Identifier: GPL-3.0-or-later
"""Paths and settings, all local. Environment variables come from the plugin's .mcp.json."""

import os

from pathlib import Path

from . import __version__


def data_dir() -> Path:
    configured = os.environ.get("CLAUDE_MUSIC_DATA")

    if configured:
        return Path(configured).expanduser()

    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "claude-music"


def music_dir() -> Path:
    return Path(os.environ.get("CLAUDE_MUSIC_DIR") or "~/Music").expanduser()


def db_path() -> Path:
    return data_dir() / "state.db"


def playlists_dir() -> Path:
    return data_dir() / "playlists"


def user_agent() -> str:
    contact = os.environ.get("CLAUDE_MUSIC_CONTACT") or "contact-not-configured"
    return f"claude-music/{__version__} ( {contact} )"


def bridge_socket_path() -> str:
    if os.environ.get("NICOTINE_MCP_SOCKET"):
        return os.environ["NICOTINE_MCP_SOCKET"]

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nicotine-mcp-{os.getuid()}"
    candidates = [
        os.path.join(runtime_dir, "nicotine-mcp.sock"),
        os.path.join(runtime_dir, "app", "org.nicotine_plus.Nicotine", "nicotine-mcp.sock"),
    ]
    return next((c for c in candidates if os.path.exists(c)), candidates[0])
