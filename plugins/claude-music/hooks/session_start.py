#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""SessionStart hook: is the Nicotine+ bridge reachable, and is the library server's venv in sync?

Prints one JSON object with additionalContext. Never fails the session; stdlib only.
"""

import json
import os
import shutil
import socket
import subprocess
import sys


def bridge_socket():
    if os.environ.get("NICOTINE_MCP_SOCKET"):
        return os.environ["NICOTINE_MCP_SOCKET"]

    configured = os.environ.get("CLAUDE_PLUGIN_OPTION_BRIDGE_SOCKET")

    if configured:
        return configured

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nicotine-mcp-{os.getuid()}"
    candidates = [
        os.path.join(runtime_dir, "nicotine-mcp.sock"),
        os.path.join(runtime_dir, "app", "org.nicotine_plus.Nicotine", "nicotine-mcp.sock"),
    ]
    return next((c for c in candidates if os.path.exists(c)), candidates[0])


def bridge_plugin_installed():
    home = os.path.expanduser("~")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    candidates = [
        os.path.join(data_home, "nicotine", "plugins", "mcp_bridge"),
        os.path.join(home, ".var", "app", "org.nicotine_plus.Nicotine", "data", "nicotine", "plugins", "mcp_bridge"),
    ]
    return any(os.path.isdir(c) for c in candidates)


def check_bridge():
    path = bridge_socket()

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(path)
            conn.sendall(b'{"method": "status", "params": {}}\n')
            data = b""

            while b"\n" not in data:
                chunk = conn.recv(65536)

                if not chunk:
                    break

                data += chunk
    except OSError as error:
        hint = "Start Nicotine+ and enable the MCP Bridge plugin before playlist sync."

        if not bridge_plugin_installed():
            hint = (
                "The MCP Bridge plugin is not in Nicotine+'s plugin folder yet: run install.sh from the repository "
                "(after a marketplace install it is at ~/.claude/plugins/marketplaces/claude-music/install.sh), "
                "then tick MCP Bridge in Nicotine+ -> Preferences -> Plugins."
            )

        return f"Nicotine+ bridge NOT reachable at {path} ({error.strerror or error}). {hint}"

    try:
        response = json.loads(data.split(b"\n", 1)[0])
        result = response.get("result") or {}
    except ValueError:
        return f"Nicotine+ bridge at {path} gave an unreadable reply."

    if not response.get("ok"):
        return f"Nicotine+ bridge error: {response.get('error')}"

    protocol = result.get("protocol")
    state = "online" if result.get("online") else "OFFLINE (not logged in to Soulseek)"
    note = f"Nicotine+ {result.get('nicotine_version')} bridge protocol v{protocol}, {state}, user {result.get('username')}."

    if protocol == 1:
        note += " The Nicotine+ plugin is protocol v1: no search rate limiting or folder browsing; run install.sh again and re-enable the plugin."

    return note


def check_venv(plugin_root, data_dir):
    if not data_dir:
        return "Plugin data dir unknown; the library server will create its venv on first use."

    project = os.path.join(plugin_root, "servers", "library")
    venv = os.path.join(data_dir, "venv")
    uv = shutil.which("uv")

    if uv is None:
        return "uv is not installed; the MCP servers cannot start (pacman -S uv)."

    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": venv}

    try:
        proc = subprocess.run(
            [uv, "sync", "--project", project, "--frozen", "--quiet"], env=env, capture_output=True, text=True, timeout=50
        )
    except subprocess.TimeoutExpired:
        return "Library server venv sync timed out; it will be retried when the server starts."

    if proc.returncode != 0:
        return "Library server venv sync failed: " + (proc.stderr.strip().splitlines() or ["unknown error"])[-1]

    return "Library server venv is in sync."


def main():
    plugin_root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CLAUDE_PLUGIN_DATA", "")
    lines = ["claude-music:", check_bridge(), check_venv(plugin_root, data_dir)]
    print(json.dumps({"additionalContext": " ".join(lines)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - a hook must never break the session
        print(json.dumps({"additionalContext": f"claude-music session hook error: {error}"}))
