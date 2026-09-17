"""Loading, unloading and error handling of the bridge plugin through Nicotine+'s real plugin loader."""

import json
import os
import stat

import pytest

from harness import PLUGIN_NAME


def test_plugin_loads_and_socket_is_private(nicotine):
    if nicotine.bridge_loaded:
        nicotine.disable_bridge()

    assert nicotine.enable_bridge()
    assert nicotine.bridge_loaded

    mode = os.stat(nicotine.socket_path).st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o600

    status = nicotine.rpc("status")
    assert status["protocol"] == 1
    assert status["nicotine_version"] == nicotine.version
    assert status["online"] is True
    assert status["username"] == nicotine.username


def test_duplicate_enable_is_rejected(nicotine):
    if not nicotine.bridge_loaded:
        nicotine.enable_bridge()

    first = nicotine.plugin
    nicotine.enable_bridge()

    assert nicotine.plugin is first, "enabling twice must not create a second instance"
    assert sum(1 for name in nicotine._loaded_plugins if name == PLUGIN_NAME) == 1
    assert nicotine.rpc("status")["online"] is True


def test_second_instance_cannot_bind_the_same_socket(nicotine):
    if not nicotine.bridge_loaded:
        nicotine.enable_bridge()

    plugin_class = type(nicotine.plugin)

    def try_second():
        other = plugin_class()
        other.settings = dict(nicotine.plugin.settings)
        try:
            other.init()
        except RuntimeError as error:
            return str(error)
        finally:
            other.disable()
        return None

    message = nicotine.on_main(try_second)
    assert message and "already serving" in message
    assert nicotine.rpc("status")["online"] is True  # first instance unaffected


@pytest.mark.parametrize("payload, expected", [
    (b"{not json\n", "Expecting"),
    (b'{"method": "nope", "params": {}}\n', "unknown method 'nope'"),
    (b'{"method": "search", "params": {"nope": 1}}\n', "unexpected keyword argument"),
    (b'{"method": "search", "params": {"query": ""}}\n', "query must not be empty"),
    (b'{"method": "search_results", "params": {"search_id": 999999}}\n', "unknown search_id"),
    (b'{"method": "clear_downloads", "params": {}}\n', "refusing to clear everything"),
    (b'{"params": {}}\n', "unknown method None"),
    (b'"just a string"\n', "AttributeError"),
])
def test_bad_requests_get_error_responses(bridge, payload, expected):
    response = bridge.call_raw(payload)
    assert response["ok"] is False
    assert expected in response["error"]
    # Nicotine+ is still alive and serving afterwards
    assert bridge.rpc("status")["online"] is True


def test_oversized_request_is_rejected(bridge):
    payload = json.dumps({"method": "status", "params": {"pad": "x" * (300 * 1024)}}).encode() + b"\n"
    response = bridge.call_raw(payload)
    assert response["ok"] is False
    assert "request too large" in response["error"]
    assert bridge.rpc("status")["online"] is True


def test_offline_state_is_reported(bridge):
    bridge.set_online(False)
    try:
        assert bridge.rpc("status")["online"] is False
        response = bridge.call("search", query="anything")
        assert response["ok"] is False
        assert "not connected" in response["error"]
        response = bridge.call("download_folder", username="peer", folder_path="@@x\\a")
        assert response["ok"] is False
        assert "not connected" in response["error"]
    finally:
        bridge.set_online(True)


def test_disable_removes_socket_and_reenable_works(nicotine):
    if not nicotine.bridge_loaded:
        nicotine.enable_bridge()

    assert nicotine.disable_bridge()
    assert not os.path.lexists(nicotine.socket_path)
    assert not nicotine.bridge_loaded

    assert nicotine.enable_bridge()
    assert os.path.lexists(nicotine.socket_path)
    assert nicotine.rpc("status")["protocol"] == 1


def test_stale_socket_file_is_replaced(nicotine):
    import socket

    if nicotine.bridge_loaded:
        nicotine.disable_bridge()

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(nicotine.socket_path)
    stale.close()  # leaves a socket file nobody is listening on
    assert os.path.lexists(nicotine.socket_path)

    assert nicotine.enable_bridge()
    assert nicotine.rpc("status")["online"] is True


def test_non_socket_file_in_the_way_fails_load_cleanly(nicotine):
    if nicotine.bridge_loaded:
        nicotine.disable_bridge()

    with open(nicotine.socket_path, "w") as handle:
        handle.write("not a socket")

    try:
        nicotine.enable_bridge()
        assert not nicotine.bridge_loaded, "plugin must refuse to load when the path is a regular file"
        assert nicotine.pump_alive
    finally:
        os.unlink(nicotine.socket_path)

    assert nicotine.enable_bridge()
    assert nicotine.rpc("status")["online"] is True
