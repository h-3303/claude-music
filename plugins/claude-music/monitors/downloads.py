#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Download-completion monitor: polls the Nicotine+ MCP Bridge and prints one line per transfer that finishes
or fails after the monitor started. Silent otherwise. Stdlib only; never exits on its own.

Started by Claude Code as a plugin monitor (monitors/monitors.json) the first time playlist-sync is invoked.
Every stdout line becomes a notification in the session, so lines are rare and self-contained. When the
transfer belongs to a claude-music playlist (its download id is in state.db) the line names the playlist so
the session knows to call sync_downloads.

Socket path: $NICOTINE_MCP_SOCKET, else <data>/bridge_socket (written by the library server when a custom
path is configured; monitors cannot read user config themselves), else the default locations.
"""

import json
import os
import socket
import sqlite3
import sys
import time

POLL_S = float(os.environ.get("CLAUDE_MUSIC_MONITOR_POLL_S") or 20)
FAILED = {"Cancelled", "Filtered", "User logged off", "Connection closed", "Connection timeout",
          "Download folder error", "Local file error"}


def socket_candidates(data_dir):
    if os.environ.get("NICOTINE_MCP_SOCKET"):
        return [os.environ["NICOTINE_MCP_SOCKET"]]

    candidates = []

    if data_dir:
        try:
            with open(os.path.join(data_dir, "bridge_socket"), encoding="utf-8") as handle:
                configured = handle.read().strip()

            if configured:
                candidates.append(configured)
        except OSError:
            pass

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nicotine-mcp-{os.getuid()}"
    candidates.append(os.path.join(runtime_dir, "nicotine-mcp.sock"))
    candidates.append(os.path.join(runtime_dir, "app", "org.nicotine_plus.Nicotine", "nicotine-mcp.sock"))
    return candidates


def rpc(path, method, **params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(path)
        conn.sendall((json.dumps({"method": method, "params": params}) + "\n").encode("utf-8"))
        data = b""

        while b"\n" not in data:
            chunk = conn.recv(1 << 20)

            if not chunk:
                break

            data += chunk

    response = json.loads(data.split(b"\n", 1)[0])

    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "bridge error")

    return response["result"]


def list_downloads(candidates):
    last_error = None

    for path in candidates:
        try:
            return path, rpc(path, "list_downloads", limit=2000)["downloads"]
        except (OSError, RuntimeError, ValueError) as error:
            last_error = error

    raise OSError(str(last_error) if last_error else "no socket")


def playlist_of(data_dir, download_id):
    """Playlist id and name for a claude-music transfer, or None. Read-only; tolerant of a missing db."""
    if not data_dir:
        return None

    db = os.path.join(data_dir, "state.db")

    if not os.path.exists(db):
        return None

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)

        try:
            row = conn.execute(
                "SELECT p.id, p.name FROM matches m JOIN tracks t ON t.id = m.track_id JOIN playlists p ON p.id = t.playlist_id "
                "WHERE m.download_id = ? LIMIT 1", (download_id,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    return {"id": row[0], "name": row[1]} if row else None


def emit(line):
    print(line, flush=True)


def describe(transfer, data_dir):
    name = transfer["path"].rpartition("\\")[2]
    playlist = playlist_of(data_dir, transfer["download_id"])
    suffix = f" (playlist {playlist['id']} \"{playlist['name']}\": call sync_downloads)" if playlist else ""
    return name, suffix


class Monitor:
    """One poll at a time; remembers every transfer's last status so only changes are printed."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.candidates = socket_candidates(data_dir)
        self.known = None            # download_id -> status, seeded silently on the first successful poll
        self.reachable = None
        self.announced_idle = True

    def poll(self):
        try:
            path, downloads = list_downloads(self.candidates)
        except OSError:
            if self.reachable:
                emit("claude-music: Nicotine+ bridge no longer reachable; download monitor waiting for it to come back")

            self.reachable = False
            return

        if self.reachable is False:
            emit(f"claude-music: Nicotine+ bridge back at {path}")

        self.reachable = True
        current = {d["download_id"]: d for d in downloads}

        if self.known is not None:
            for download_id, transfer in current.items():
                before = self.known.get(download_id)
                status = transfer["status"]

                if status == before:
                    continue

                if status == "Finished":
                    name, suffix = describe(transfer, self.data_dir)
                    emit(f"claude-music: finished {name!r} from {transfer['user']} -> {transfer.get('folder') or 'download folder'}{suffix}")
                elif status in FAILED and before not in FAILED:
                    name, suffix = describe(transfer, self.data_dir)
                    emit(f"claude-music: {status.lower()}: {name!r} from {transfer['user']}{suffix}")

        active = [d for d in current.values() if d["status"] not in FAILED and d["status"] != "Finished"]

        if self.known is not None and not active and not self.announced_idle:
            emit("claude-music: no downloads in progress; sync_downloads will settle the playlist state")
            self.announced_idle = True
        elif active:
            self.announced_idle = False

        self.known = {download_id: d["status"] for download_id, d in current.items()}


def run(data_dir, poll_s=POLL_S):
    monitor = Monitor(data_dir)

    while True:
        monitor.poll()
        time.sleep(poll_s)


if __name__ == "__main__":
    try:
        run(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLAUDE_PLUGIN_DATA", ""))
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - a monitor must not spam; one line and stop
        emit(f"claude-music: download monitor stopped: {error}")
