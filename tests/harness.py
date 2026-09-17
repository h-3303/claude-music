"""Headless Nicotine+ harness for testing the MCP bridge plugin.

One Nicotine+ core per process (pynicotine uses module-level singletons), so the harness is
created once per test session. The Nicotine+ "main thread" is a pump thread that drains
events.process_thread_events(); tests touch the core only through on_main(), and inject fake
peers by emitting messages onto the main thread, exactly as the network thread would.

Keep everything that touches the core inside functions: Nicotine+'s share scanner uses
multiprocessing "spawn", which re-imports the entry module in a child process.
"""

import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SOURCE = REPO_ROOT / "nicotine-plugin" / "mcp_bridge"
PLUGIN_NAME = "mcp_bridge"

COMPONENTS = {"users", "downloads", "search", "network_filter", "pluginhandler", "shares", "uploads", "userbrowse"}


class BridgeError(Exception):
    """The bridge answered {"ok": false, ...}."""

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class NicotineHarness:

    def __init__(self, source_dir: Path, data_dir: Path, username="tester"):
        self.source_dir = Path(source_dir)
        self.data_dir = Path(data_dir)
        self.username = username
        self.socket_path = str(self.data_dir / "bridge.sock")
        self.plugin_dir = self.data_dir / "plugins" / PLUGIN_NAME
        self.quit_events = 0
        self.version = None
        self.is_34 = False
        self._pump_thread = None
        self._stop = threading.Event()
        self._pump_errors = []
        self._core = None
        self._events = None

    # Lifecycle #

    def start(self):
        if str(self.source_dir) not in sys.path:
            sys.path.insert(0, str(self.source_dir))

        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["NICOTINE_MCP_SOCKET"] = self.socket_path

        import pynicotine
        from pynicotine.config import config
        from pynicotine.core import core
        from pynicotine.events import events
        from pynicotine.slskmessages import UserStatus

        self.version = pynicotine.__version__
        self.is_34 = tuple(int(p) for p in self.version.split(".")[:2]) >= (3, 4)
        self._core = core
        self._events = events

        config.set_data_folder(str(self.data_dir))
        config.set_config_file(str(self.data_dir / "config"))
        core.init_components(enabled_components=set(COMPONENTS))

        transfers = config.sections["transfers"]
        transfers["rescanonstartup"] = False
        transfers["shared"] = []
        transfers["buddyshared"] = []
        transfers["downloaddir"] = str(self.data_dir / "downloads")
        transfers["incompletedir"] = str(self.data_dir / "incomplete")
        transfers["uploaddir"] = str(self.data_dir / "received")
        config.sections["server"]["login"] = self.username
        config.sections["server"]["passw"] = "not-a-real-password"

        events.connect("quit", self._on_quit)
        core.start()
        core.users.login_status = UserStatus.ONLINE
        core.users.login_username = self.username

        self.install_plugin()

        self._stop.clear()
        self._pump_thread = threading.Thread(target=self._pump, name="NicotineMainLoop", daemon=True)
        self._pump_thread.start()

    def stop(self):
        try:
            if self.bridge_loaded:
                self.disable_bridge()
        except Exception:
            pass

        self._stop.set()

        if self._pump_thread is not None:
            self._pump_thread.join(timeout=5)

        try:
            self._core.quit()
            self._events.process_thread_events()
        except Exception:
            pass

    def _on_quit(self):
        self.quit_events += 1

    def _pump(self):
        while not self._stop.is_set():
            try:
                self._events.process_thread_events()
            except Exception:
                # Real Nicotine+ would have quit here; record it so a test can assert on it.
                self._pump_errors.append(traceback.format_exc())

            time.sleep(0.005)

    @property
    def pump_alive(self):
        return self._pump_thread is not None and self._pump_thread.is_alive()

    @property
    def pump_errors(self):
        return list(self._pump_errors)

    @property
    def core(self):
        return self._core

    @property
    def events(self):
        return self._events

    # Main-thread marshalling #

    def on_main(self, function, *args, timeout=10, **kwargs):
        """Run function on the Nicotine+ main loop and return its result (re-raising exceptions)."""
        done = threading.Event()
        box = {}

        def runner():
            try:
                box["result"] = function(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001 - re-raised in the caller
                box["error"] = error
            finally:
                done.set()

        self._events.invoke_main_thread(runner)

        if not done.wait(timeout):
            raise TimeoutError("Nicotine+ main loop did not run the callback in time")

        if "error" in box:
            raise box["error"]

        return box.get("result")

    def barrier(self):
        """Wait until every event queued so far has been processed."""
        self.on_main(lambda: None)

    # Plugin management #

    def install_plugin(self, source=PLUGIN_SOURCE):
        if self.plugin_dir.exists():
            shutil.rmtree(self.plugin_dir)

        shutil.copytree(source, self.plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))

    @property
    def _loaded_plugins(self):
        handler = self._core.pluginhandler
        return handler.loaded_plugins if hasattr(handler, "loaded_plugins") else handler.enabled_plugins

    @property
    def bridge_loaded(self):
        return PLUGIN_NAME in self._loaded_plugins

    @property
    def plugin(self):
        return self._loaded_plugins[PLUGIN_NAME]

    def enable_bridge(self):
        self.on_main(self._core.pluginhandler.enable_plugin, PLUGIN_NAME)
        return self.bridge_loaded

    def disable_bridge(self):
        self.on_main(self._core.pluginhandler.disable_plugin, PLUGIN_NAME)
        return not self.bridge_loaded

    def set_plugin_setting(self, key, value):
        def apply():
            self.plugin.settings[key] = value

        self.on_main(apply)

    # Raw socket client #

    def call_raw(self, payload: bytes, timeout=20) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(self.socket_path)
            conn.sendall(payload)
            buffer = bytearray()

            while b"\n" not in buffer:
                chunk = conn.recv(1 << 20)

                if not chunk:
                    break

                buffer += chunk

        if not buffer:
            raise ConnectionError("bridge closed the connection without replying")

        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))

    def call(self, method, **params) -> dict:
        """Full response dict ({"ok": ..., ...})."""
        return self.call_raw(json.dumps({"method": method, "params": params}).encode("utf-8") + b"\n")

    def rpc(self, method, **params):
        """Result of a successful call; raises BridgeError on {"ok": false}."""
        response = self.call(method, **params)

        if not response.get("ok"):
            raise BridgeError(response.get("error"), response)

        return response["result"]

    # Fake peers #

    def make_attrs(self, bitrate=None, duration=None, vbr=None, sample_rate=None, bit_depth=None):
        """Build file attributes the way this Nicotine+ version represents them."""
        if self.is_34:
            from pynicotine.slskmessages import FileAttributes
            return FileAttributes(bitrate=bitrate, length=duration, vbr=vbr, sample_rate=sample_rate, bit_depth=bit_depth)

        attrs = {}

        for code, value in ((0, bitrate), (1, duration), (2, vbr), (4, sample_rate), (5, bit_depth)):
            if value is not None:
                attrs[code] = value

        return attrs

    def make_file(self, path, size=10_000_000, **attrs):
        """A (code, path, size, ext, attrs) tuple as found in FileSearchResponse.list."""
        ext = path.rpartition(".")[2] if "." in path.rpartition("\\")[2] else ""
        return (1, path, size, ext, self.make_attrs(**attrs))

    def send_search_response(self, token, username, files, free_slots=1, speed=500_000, queue=0,
                             private_files=None, addr=("10.0.0.1", 2234)):
        from pynicotine.slskmessages import FileSearchResponse

        msg = FileSearchResponse(
            search_username=self.username, token=token, shares=list(files),
            freeulslots=free_slots, ulspeed=speed, inqueue=queue, private_shares=private_files
        )
        msg.username = username
        msg.addr = addr
        self._events.emit_main_thread("file-search-response", msg)
        self.barrier()
        return msg

    def send_folder_contents(self, username, folder_path, listing, token=1):
        """listing: {folder_path: [(code, basename, size, ext, attrs), ...]}."""
        from pynicotine.slskmessages import FolderContentsResponse

        msg = FolderContentsResponse(directory=folder_path, token=token, shares=dict(listing))
        msg.username = username
        self._events.emit_main_thread("folder-contents-response", msg)
        self.barrier()
        return msg

    # State inspection / reset #

    def transfers(self):
        return self.on_main(lambda: list(self._core.downloads.transfers.values()))

    def set_online(self, online=True):
        from pynicotine.slskmessages import UserStatus

        def apply():
            self._core.users.login_status = UserStatus.ONLINE if online else UserStatus.OFFLINE

        self.on_main(apply)

    def reset(self):
        """Drop every search and download so tests start from a clean core."""
        def apply():
            core = self._core

            for token in list(core.search.searches):
                core.search.remove_search(token)

            transfers = list(core.downloads.transfers.values())

            if transfers:
                core.downloads.clear_downloads(downloads=transfers)

            if self.bridge_loaded:
                plugin = self.plugin
                plugin._searches.clear()
                plugin._pending_folders.clear()
                plugin._folder_log.clear()

        self.on_main(apply)
        self.set_online(True)
