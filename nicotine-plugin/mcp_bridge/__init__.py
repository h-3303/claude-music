# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP Bridge for Nicotine+.

Exposes a tiny JSON API on a Unix domain socket so an external MCP server can
search Soulseek and manage downloads through this running Nicotine+ instance.

Wire protocol (v2; every v1 method and response shape is unchanged, v2 only adds):
    client -> {"method": "<name>", "params": {...}}\\n
    server -> {"ok": true, "result": ...}\\n   or   {"ok": false, "error": "..."}\\n
    A search refused by the rate limiter answers {"ok": false, "error": "rate_limited", "retry_after": <s>}.

Threading model: the socket server runs in background threads, but every
handler is marshalled onto Nicotine+'s main loop via events.invoke_main_thread,
so all plugin state and all core calls happen on the main thread only.
"""

import hashlib
import json
import os
import socket
import stat
import struct
import threading
import time
import traceback

from collections import OrderedDict

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin

PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 256 * 1024
MAIN_THREAD_TIMEOUT = 15
PENDING_FOLDER_TTL = 180
MAX_FOLDER_LISTINGS = 50
UINT32_LIMIT = 2 ** 32 - 1

# Soulseek file attribute codes (pynicotine.slskmessages.FileAttribute)
ATTR_CODES = {"bitrate": 0, "duration": 1, "vbr": 2, "sample_rate": 4, "bit_depth": 5}
# Nicotine+ 3.4+ uses a FileAttributes object instead of a dict
ATTR_NAMES_34 = {"bitrate": "bitrate", "duration": "length", "vbr": "vbr",
                 "sample_rate": "sample_rate", "bit_depth": "bit_depth"}

LOSSLESS_EXTENSIONS = {"flac", "wav", "ape", "wv", "aif", "aiff", "dsf", "dff", "tak", "tta"}
OFFLINE = 0  # pynicotine.slskmessages.UserStatus.OFFLINE


def _download_id(username, virtual_path):
    return hashlib.sha1(f"{username}\0{virtual_path}".encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _read_attrs(attrs):
    if isinstance(attrs, dict):
        return {key: attrs.get(code) for key, code in ATTR_CODES.items()}

    return {key: getattr(attrs, name, None) for key, name in ATTR_NAMES_34.items()}


def _extension(path):
    basename = path.rpartition("\\")[2]
    return basename.rpartition(".")[2].lower() if "." in basename else ""


class RateLimited(ValueError):
    """Raised when the search token bucket is empty; retry_after is in seconds."""

    def __init__(self, retry_after):
        super().__init__("rate_limited")
        self.retry_after = retry_after


class Plugin(BasePlugin):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settings = {
            "socket_path": "",
            "allow_downloads": True,
            "max_results_per_search": 5000,
            "max_searches_kept": 20,
            "search_rate_limit": 34,
            "search_rate_window": 220,
        }
        self.metasettings = {
            "socket_path": {
                "description": "Socket path (empty = $XDG_RUNTIME_DIR/nicotine-mcp.sock). Re-enable plugin after changing.",
                "type": "string"
            },
            "allow_downloads": {
                "description": "Allow the MCP client to queue, cancel, retry and clear downloads",
                "type": "bool"
            },
            "max_results_per_search": {
                "description": "Max files kept in memory per search",
                "type": "int", "minimum": 100
            },
            "max_searches_kept": {
                "description": "Max searches tracked at once (oldest are removed)",
                "type": "int", "minimum": 1
            },
            "search_rate_limit": {
                "description": "Max searches the MCP client may start per window (Soulseek bans for 30 min if exceeded)",
                "type": "int", "minimum": 1
            },
            "search_rate_window": {
                "description": "Length of the search rate window in seconds",
                "type": "int", "minimum": 1
            },
        }

        self._searches = OrderedDict()   # token -> dict
        self._pending_folders = {}       # (username, folder_path) -> dict
        self._folder_listings = OrderedDict()  # (username, folder_path) -> listing (listing-only requests)
        self._folder_log = []            # recent folder request outcomes
        self._listing_token = 0
        self._rate_tokens = None         # search token bucket (None = full)
        self._rate_updated = time.monotonic()
        self._captured_tokens = None
        self._server_socket = None
        self._server_thread = None
        self._stopping = threading.Event()
        self._socket_path = None
        self._connected_events = []

    # Lifecycle #

    def init(self):
        # Start the socket first: if it fails, the plugin fails to load without leaving handlers behind
        self._start_server()

        for event_name, callback in (
            ("add-search", self._on_add_search),
            ("file-search-response", self._on_file_search_response),
            ("folder-contents-response", self._on_folder_contents_response),
        ):
            events.connect(event_name, callback)
            self._connected_events.append((event_name, callback))

    def disable(self):
        self._stop_server()

        for event_name, callback in self._connected_events:
            try:
                events.disconnect(event_name, callback)
            except ValueError:
                pass

        self._connected_events.clear()

    def shutdown_notification(self):
        self._stop_server()

    # Socket server #

    def _resolve_socket_path(self):
        env_path = os.environ.get("NICOTINE_MCP_SOCKET")
        configured = (self.settings.get("socket_path") or "").strip()

        if configured:
            return os.path.expanduser(configured)

        if env_path:
            return env_path

        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nicotine-mcp-{os.getuid()}"
        flatpak_id = os.environ.get("FLATPAK_ID")

        if flatpak_id:
            # Only $XDG_RUNTIME_DIR/app/<app-id> is shared between a Flatpak sandbox and the host
            return os.path.join(runtime_dir, "app", flatpak_id, "nicotine-mcp.sock")

        return os.path.join(runtime_dir, "nicotine-mcp.sock")

    def _start_server(self):
        path = self._resolve_socket_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise RuntimeError(f"{path} exists and is not a socket")

            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
            except OSError:
                os.unlink(path)  # stale socket from a previous run
            else:
                raise RuntimeError(f"another Nicotine+ instance is already serving {path}")
            finally:
                probe.close()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # socket file is created 0600

        try:
            server.bind(path)
        finally:
            os.umask(old_umask)

        server.listen(8)
        server.settimeout(0.5)

        self._server_socket = server
        self._socket_path = path
        self._stopping.clear()
        self._server_thread = threading.Thread(target=self._accept_loop, name="MCPBridgeServer", daemon=True)
        self._server_thread.start()
        self.log("MCP bridge listening on %s", path)

    def _stop_server(self):
        if self._server_socket is None:
            return

        self._stopping.set()

        if self._server_thread is not None:
            self._server_thread.join(timeout=2)

        try:
            self._server_socket.close()
        except OSError:
            pass

        try:
            if self._socket_path and os.path.lexists(self._socket_path):
                os.unlink(self._socket_path)
        except OSError:
            pass

        self._server_socket = None
        self._server_thread = None

    def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                conn, _addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn):
        with conn:
            try:
                conn.settimeout(MAIN_THREAD_TIMEOUT + 5)

                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", creds)

                if uid != os.getuid():
                    self._send(conn, {"ok": False, "error": "permission denied"})
                    return

                request = json.loads(self._read_line(conn))
                method = request.get("method")
                params = request.get("params") or {}
                handler = self.HANDLERS.get(method)

                if handler is None:
                    self._send(conn, {"ok": False, "error": f"unknown method {method!r}"})
                    return

                result = self._call_main_thread(handler, self, **params)
                self._send(conn, {"ok": True, "result": result})

            except Exception as error:  # never let a bad request touch Nicotine+
                if isinstance(error, (ValueError, PermissionError, LookupError, TimeoutError)) and error.args:
                    message = str(error.args[0])
                else:
                    message = f"{type(error).__name__}: {error}"

                payload = {"ok": False, "error": message}

                if isinstance(error, RateLimited):
                    payload["retry_after"] = round(error.retry_after, 1)

                try:
                    self._send(conn, payload)
                except OSError:
                    pass

    @staticmethod
    def _read_line(conn):
        buffer = bytearray()

        while b"\n" not in buffer:
            chunk = conn.recv(65536)

            if not chunk:
                break

            buffer += chunk

            if len(buffer) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")

        return buffer.split(b"\n", 1)[0].decode("utf-8")

    @staticmethod
    def _send(conn, payload):
        conn.sendall(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8") + b"\n")

    def _call_main_thread(self, function, *args, **kwargs):
        done = threading.Event()
        box = {}

        def runner():
            # Exceptions escaping a thread-callback make Nicotine+ quit, so trap everything.
            try:
                box["result"] = function(*args, **kwargs)
            except Exception as error:
                box["error"] = error
                box["trace"] = traceback.format_exc()
            finally:
                done.set()

        events.invoke_main_thread(runner)

        if not done.wait(MAIN_THREAD_TIMEOUT):
            raise TimeoutError("Nicotine+ main loop did not respond in time")

        if "error" in box:
            if not isinstance(box["error"], (ValueError, PermissionError, KeyError, LookupError)):
                self.log("Bridge handler error:\n%s", box["trace"])
            raise box["error"]

        return box["result"]

    # Event handlers (main thread) #

    def _on_add_search(self, token, *_args, **_kwargs):
        try:
            if self._captured_tokens is not None:
                self._captured_tokens.append(token)
        except Exception:
            pass

    def _on_file_search_response(self, msg):
        try:
            entry = self._searches.get(msg.token)

            if entry is None:
                return

            limit = int(self.settings.get("max_results_per_search") or 5000)
            username = msg.username
            entry["users"].add(username)
            entry["responses"] += 1

            for is_private, file_list in ((False, msg.list), (True, getattr(msg, "privatelist", None))):
                for item in file_list or ():
                    if len(entry["results"]) >= limit:
                        entry["truncated"] = True
                        return

                    _code, virtual_path, size, _ext, attrs, *_unused = item

                    entry["results"].append({
                        "id": len(entry["results"]),
                        "user": username,
                        "path": virtual_path,
                        "size": size,
                        **_read_attrs(attrs),
                        "free_slot": bool(msg.freeulslots),
                        "speed": msg.ulspeed or 0,
                        "queue": msg.inqueue or 0,
                        "private": is_private,
                        "_attrs": attrs,
                    })
        except Exception:
            self.log("Error recording search response:\n%s", traceback.format_exc())

    def _on_folder_contents_response(self, msg):
        try:
            requested_path = msg.dir
            key = (msg.username, requested_path)
            pending = self._pending_folders.get(key)

            if pending is None or not msg.list:
                # Unknown request, or empty response (Nicotine+ retries with legacy encoding)
                return

            del self._pending_folders[key]

            if pending.get("mode") == "list":
                self._store_folder_listing(key, msg, pending["include_subfolders"])
                return

            downloads = self.core.downloads
            queued = 0
            prefix = requested_path.rstrip("\\") + "\\"

            for folder_path, files in msg.list.items():
                is_root = folder_path == requested_path
                is_sub = pending["include_subfolders"] and folder_path.startswith(prefix)

                if not (is_root or is_sub):
                    continue

                destination = downloads.get_folder_destination(
                    msg.username, folder_path, root_folder_path=requested_path if is_sub else None
                )

                for _code, basename, size, _ext, attrs, *_unused in files:
                    virtual_path = folder_path.rstrip("\\") + "\\" + basename
                    downloads.enqueue_download(
                        msg.username, virtual_path, folder_path=destination, size=size, file_attributes=attrs
                    )
                    queued += 1

            self._log_folder(msg.username, requested_path, f"queued {queued} files")

        except Exception:
            self.log("Error handling folder contents:\n%s", traceback.format_exc())

    def _store_folder_listing(self, key, msg, include_subfolders):
        username, requested_path = key
        prefix = requested_path.rstrip("\\") + "\\"
        folders = {}
        total = 0

        for folder_path, files in msg.list.items():
            is_root = folder_path == requested_path
            is_sub = include_subfolders and folder_path.startswith(prefix)

            if not (is_root or is_sub):
                continue

            entries = []

            for _code, basename, size, _ext, attrs, *_unused in files:
                entries.append({
                    "name": basename,
                    "path": folder_path.rstrip("\\") + "\\" + basename,
                    "size": size,
                    **_read_attrs(attrs),
                })

            folders[folder_path] = entries
            total += len(entries)

        self._folder_listings[key] = {
            "user": username, "folder": requested_path, "received": time.time(),
            "folders": folders, "total_files": total,
        }

        while len(self._folder_listings) > MAX_FOLDER_LISTINGS:
            self._folder_listings.popitem(last=False)

        self._log_folder(username, requested_path, f"listed {total} files")

    def _log_folder(self, username, folder_path, outcome):
        self._folder_log.append({"user": username, "folder": folder_path, "outcome": outcome, "time": time.time()})
        del self._folder_log[:-20]

    def _expire_pending_folders(self):
        now = time.time()

        for key, pending in list(self._pending_folders.items()):
            if now - pending["requested"] > PENDING_FOLDER_TTL:
                del self._pending_folders[key]
                self._log_folder(key[0], key[1], "timed out (user offline or not responding)")

    # Search rate limiter (token bucket, main thread) #

    def _rate_capacity(self):
        return max(1, int(self.settings.get("search_rate_limit") or 34))

    def _rate_window(self):
        return max(1, int(self.settings.get("search_rate_window") or 220))

    def _refill_rate_bucket(self):
        capacity = self._rate_capacity()
        now = time.monotonic()

        if self._rate_tokens is None:
            self._rate_tokens = float(capacity)

        self._rate_tokens = min(float(capacity), self._rate_tokens + (now - self._rate_updated) * capacity / self._rate_window())
        self._rate_updated = now

    def _take_search_token(self):
        self._refill_rate_bucket()

        if self._rate_tokens >= 1:
            self._rate_tokens -= 1
            return

        per_second = self._rate_capacity() / self._rate_window()
        raise RateLimited((1 - self._rate_tokens) / per_second)

    def _rate_limit_status(self):
        self._refill_rate_bucket()
        return {"available": int(self._rate_tokens), "capacity": self._rate_capacity(), "window_s": self._rate_window()}

    # API methods (main thread) #

    def _is_online(self):
        return getattr(self.core.users, "login_status", OFFLINE) != OFFLINE

    def _require_downloads_allowed(self):
        if not self.settings.get("allow_downloads", True):
            raise PermissionError("downloads are disabled in the MCP Bridge plugin settings")

    def _get_search(self, search_id):
        entry = self._searches.get(int(search_id))

        if entry is None:
            raise KeyError(f"unknown search_id {search_id} (it may have been removed)")

        return entry

    def api_status(self):
        from pynicotine import __version__

        self._expire_pending_folders()
        counts = {}

        for transfer in self.core.downloads.transfers.values():
            status = transfer.status or "Unknown"
            counts[status] = counts.get(status, 0) + 1

        return {
            "protocol": PROTOCOL_VERSION,
            "nicotine_version": __version__,
            "online": self._is_online(),
            "username": getattr(self.core.users, "login_username", None),
            "allow_downloads": bool(self.settings.get("allow_downloads", True)),
            "download_folder": self.core.downloads.get_default_download_folder(),
            "downloads_by_status": counts,
            "rate_limit": self._rate_limit_status(),
            "searches": self.api_list_searches(),
            "pending_folder_requests": [
                {"user": user, "folder": folder, "age_seconds": round(time.time() - p["requested"])}
                for (user, folder), p in self._pending_folders.items()
            ],
            "recent_folder_requests": self._folder_log[-10:],
        }

    def api_list_searches(self):
        now = time.time()
        return [
            {
                "search_id": token,
                "query": entry["query"],
                "mode": entry["mode"],
                "age_seconds": round(now - entry["started"]),
                "files": len(entry["results"]),
                "users": len(entry["users"]),
                "truncated": entry["truncated"],
            }
            for token, entry in self._searches.items()
        ]

    def api_search(self, query, mode="global", room=None, users=None):
        query = (query or "").strip()

        if not query:
            raise ValueError("query must not be empty")

        if mode not in {"global", "buddies", "rooms", "user"}:
            raise ValueError("mode must be one of: global, buddies, rooms, user")

        if mode == "user" and not users:
            raise ValueError("mode 'user' requires a list of usernames")

        if mode == "rooms" and not room:
            raise ValueError("mode 'rooms' requires a room name")

        if not self._is_online():
            raise ValueError("Nicotine+ is not connected to the Soulseek server")

        self._take_search_token()
        self._captured_tokens = []

        try:
            self.core.search.do_search(query, mode, room=room, users=users, switch_page=False)
            tokens = self._captured_tokens
        finally:
            self._captured_tokens = None

        if not tokens:
            raise ValueError("Nicotine+ did not start the search (query may be empty after filtering)")

        token = tokens[-1]
        self._searches[token] = {
            "query": query, "mode": mode, "started": time.time(),
            "results": [], "users": set(), "responses": 0, "truncated": False,
        }

        max_kept = int(self.settings.get("max_searches_kept") or 20)

        while len(self._searches) > max_kept:
            old_token, _old = self._searches.popitem(last=False)
            self.core.search.remove_search(old_token)

        return {"search_id": token, "query": query, "mode": mode}

    def api_search_results(self, search_id, offset=0, limit=500, extensions=None, lossless_only=False,
                           min_bitrate=None, free_slot_only=False, username=None, path_contains=None):
        entry = self._get_search(search_id)
        extensions = {e.lower().lstrip(".") for e in extensions} if extensions else None
        words = [w.lower() for w in (path_contains or "").split()]
        matched = []

        for result in entry["results"]:
            ext = _extension(result["path"])

            if extensions and ext not in extensions:
                continue
            if lossless_only and ext not in LOSSLESS_EXTENSIONS:
                continue
            if min_bitrate and ext not in LOSSLESS_EXTENSIONS and (result["bitrate"] or 0) < min_bitrate:
                continue
            if free_slot_only and not result["free_slot"]:
                continue
            if username and result["user"] != username:
                continue
            if words and not all(w in result["path"].lower() for w in words):
                continue

            matched.append(result)

        matched.sort(key=lambda r: (not r["free_slot"], r["queue"], -r["speed"]))
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 5000))
        page = [{k: v for k, v in r.items() if not k.startswith("_")} for r in matched[offset:offset + limit]]

        return {
            "search_id": int(search_id),
            "query": entry["query"],
            "age_seconds": round(time.time() - entry["started"]),
            "total_files": len(entry["results"]),
            "total_users": len(entry["users"]),
            "truncated": entry["truncated"],
            "matched": len(matched),
            "offset": offset,
            "results": page,
        }

    def api_stop_search(self, search_id):
        token = int(search_id)
        self._searches.pop(token, None)
        self.core.search.remove_search(token)
        return {"stopped": token}

    def api_download_results(self, search_id, result_ids, keep_folder_structure=True):
        self._require_downloads_allowed()
        entry = self._get_search(search_id)
        downloads = self.core.downloads
        queued, errors = [], []

        for result_id in result_ids:
            try:
                result = entry["results"][int(result_id)]
            except (IndexError, ValueError, TypeError):
                errors.append({"result_id": result_id, "error": "no such result"})
                continue

            folder = None

            if keep_folder_structure:
                parent = result["path"].rpartition("\\")[0]
                folder = downloads.get_folder_destination(result["user"], parent) if parent else None

            downloads.enqueue_download(
                result["user"], result["path"], folder_path=folder,
                size=result["size"], file_attributes=result["_attrs"]
            )
            queued.append({
                "download_id": _download_id(result["user"], result["path"]),
                "user": result["user"], "path": result["path"],
            })

        return {"queued": queued, "errors": errors}

    def api_download_folder(self, username, folder_path, include_subfolders=False):
        self._require_downloads_allowed()

        if not self._is_online():
            raise ValueError("Nicotine+ is not connected to the Soulseek server")

        folder_path = folder_path.replace("/", "\\").rstrip("\\")
        self._pending_folders[(username, folder_path)] = {
            "requested": time.time(), "include_subfolders": bool(include_subfolders), "mode": "download"
        }

        downloads = self.core.downloads
        request = getattr(downloads, "request_folder", None)  # Nicotine+ 3.4+

        if request is not None:
            request(username, folder_path)
        else:
            downloads.enqueue_folder(username, folder_path)  # Nicotine+ 3.3

        return {"requested": {"user": username, "folder": folder_path},
                "note": "Files are queued once the user replies; check status or list_downloads."}

    def api_folder_contents(self, username, folder_path, include_subfolders=False):
        """Request a folder listing without downloading anything (protocol v2)."""

        if not self._is_online():
            raise ValueError("Nicotine+ is not connected to the Soulseek server")

        folder_path = folder_path.replace("/", "\\").rstrip("\\")
        key = (username, folder_path)
        self._folder_listings.pop(key, None)
        self._pending_folders[key] = {
            "requested": time.time(), "include_subfolders": bool(include_subfolders), "mode": "list"
        }

        downloads = self.core.downloads
        request = getattr(downloads, "request_folder", None)  # Nicotine+ 3.4+: request without downloading

        if request is not None:
            request(username, folder_path)
        else:
            # Nicotine+ 3.3 has no request-only API (enqueue_folder downloads), so send the
            # request ourselves; the core ignores replies for folders it did not request.
            from pynicotine.slskmessages import FolderContentsRequest

            # A stale entry from an earlier download_folder (e.g. a pending legacy retry) would make
            # the 3.3 core download the folder when this reply arrives, so drop it first.
            stale = getattr(downloads, "_requested_folders", {}).get(username, {}).pop(folder_path, None)

            if stale is not None and getattr(stale, "request_timer_id", None) is not None:
                events.cancel_scheduled(stale.request_timer_id)

            self._listing_token = (self._listing_token % UINT32_LIMIT) + 1
            self.core.send_message_to_peer(username, FolderContentsRequest(folder_path, self._listing_token))

        return {"requested": {"user": username, "folder": folder_path},
                "note": "Poll folder_contents_result until status is 'ready'."}

    def api_folder_contents_result(self, username, folder_path):
        self._expire_pending_folders()
        folder_path = folder_path.replace("/", "\\").rstrip("\\")
        key = (username, folder_path)
        now = time.time()
        pending = self._pending_folders.get(key)

        if pending is not None and pending.get("mode") == "list":
            return {"status": "pending", "user": username, "folder": folder_path,
                    "age_seconds": round(now - pending["requested"])}

        listing = self._folder_listings.get(key)

        if listing is not None:
            return {"status": "ready", "user": username, "folder": folder_path,
                    "age_seconds": round(now - listing["received"]),
                    "total_files": listing["total_files"], "folders": listing["folders"]}

        for entry in reversed(self._folder_log):
            if (entry["user"], entry["folder"]) == key and entry["outcome"].startswith("timed out"):
                return {"status": "timed_out", "user": username, "folder": folder_path}

        return {"status": "unknown", "user": username, "folder": folder_path}

    def _transfers_by_ids(self, download_ids):
        wanted = set(download_ids)
        return [t for t in self.core.downloads.transfers.values()
                if _download_id(t.username, t.virtual_path) in wanted]

    def api_list_downloads(self, statuses=None, username=None, limit=100):
        items = []

        for transfer in self.core.downloads.transfers.values():
            if statuses and transfer.status not in statuses:
                continue
            if username and transfer.username != username:
                continue

            size = transfer.size or 0
            done = transfer.current_byte_offset or 0
            items.append({
                "download_id": _download_id(transfer.username, transfer.virtual_path),
                "user": transfer.username,
                "path": transfer.virtual_path,
                "status": transfer.status,
                "size": size,
                "progress": round(done / size, 4) if size else None,
                "speed": transfer.speed,
                "queue_position": transfer.queue_position,
                "time_left": transfer.time_left,
                "folder": transfer.folder_path,
            })

        limit = max(1, min(int(limit), 2000))
        return {"total": len(items), "downloads": items[-limit:]}

    def api_cancel_downloads(self, download_ids):
        self._require_downloads_allowed()
        from pynicotine.transfers import TransferStatus

        transfers = self._transfers_by_ids(download_ids)
        self.core.downloads.abort_downloads(transfers, status=TransferStatus.CANCELLED)
        return {"cancelled": len(transfers)}

    def api_retry_downloads(self, download_ids):
        self._require_downloads_allowed()
        transfers = self._transfers_by_ids(download_ids)
        self.core.downloads.retry_downloads(transfers)
        return {"retried": len(transfers)}

    def api_clear_downloads(self, download_ids=None, statuses=None):
        self._require_downloads_allowed()

        if not download_ids and not statuses:
            raise ValueError("pass download_ids and/or statuses (refusing to clear everything)")

        transfers = self._transfers_by_ids(download_ids) if download_ids else None
        before = len(self.core.downloads.transfers)
        self.core.downloads.clear_downloads(downloads=transfers, statuses=statuses)
        return {"cleared": before - len(self.core.downloads.transfers)}

    HANDLERS = {
        "status": api_status,
        "list_searches": api_list_searches,
        "search": api_search,
        "search_results": api_search_results,
        "stop_search": api_stop_search,
        "download_results": api_download_results,
        "download_folder": api_download_folder,
        "folder_contents": api_folder_contents,
        "folder_contents_result": api_folder_contents_result,
        "list_downloads": api_list_downloads,
        "cancel_downloads": api_cancel_downloads,
        "retry_downloads": api_retry_downloads,
        "clear_downloads": api_clear_downloads,
    }
