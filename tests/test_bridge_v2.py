"""Protocol v2 additions: search rate limiter, listing-only folder requests, status fields."""

import time

import pytest

from harness import BridgeError

ALBUM = "@@music\\Test Artist\\Album (2001)"


@pytest.fixture
def limited(bridge):
    """A tiny token bucket: 3 searches per 2 seconds, starting full."""
    bridge.set_plugin_setting("search_rate_limit", 3)
    bridge.set_plugin_setting("search_rate_window", 2)

    def reset_bucket():
        bridge.plugin._rate_tokens = None
        bridge.plugin._rate_updated = time.monotonic()

    bridge.on_main(reset_bucket)
    yield bridge
    bridge.set_plugin_setting("search_rate_limit", 34)
    bridge.set_plugin_setting("search_rate_window", 220)
    bridge.on_main(reset_bucket)


def test_status_reports_protocol_and_rate_limit(bridge):
    status = bridge.rpc("status")
    assert status["protocol"] == 2
    assert status["rate_limit"] == {"available": 34, "capacity": 34, "window_s": 220}


def test_rate_limiter_refuses_then_refills(limited):
    for i in range(3):
        limited.rpc("search", query=f"query {i}")

    assert limited.rpc("status")["rate_limit"]["available"] == 0

    response = limited.call("search", query="one too many")
    assert response["ok"] is False
    assert response["error"] == "rate_limited"
    assert 0 < response["retry_after"] <= 1.0
    assert len(limited.rpc("list_searches")) == 3, "a refused search must not be started"
    assert limited.rpc("status")["online"] is True

    time.sleep(response["retry_after"] + 0.1)
    limited.rpc("search", query="after refill")
    assert len(limited.rpc("list_searches")) == 4


def test_rate_limit_only_counts_started_searches(limited):
    with pytest.raises(BridgeError, match="mode must be one of"):
        limited.rpc("search", query="x", mode="bogus")

    assert limited.rpc("status")["rate_limit"]["available"] == 3


def test_folder_contents_lists_without_downloading(bridge):
    result = bridge.rpc("folder_contents", username="peer1", folder_path=ALBUM.replace("\\", "/"))
    assert result["requested"] == {"user": "peer1", "folder": ALBUM}

    pending = bridge.rpc("folder_contents_result", username="peer1", folder_path=ALBUM)
    assert pending["status"] == "pending"

    root = [
        (1, "01 - Track 1.flac", 25_000_000, "flac", bridge.make_attrs(duration=200, sample_rate=44100, bit_depth=16)),
        (1, "02 - Track 2.flac", 26_000_000, "flac", bridge.make_attrs(duration=210, sample_rate=44100, bit_depth=16)),
    ]
    sub = [(1, "cover.jpg", 300_000, "jpg", bridge.make_attrs())]
    bridge.send_folder_contents("peer1", ALBUM, {ALBUM: root, ALBUM + "\\Scans": sub})

    ready = bridge.rpc("folder_contents_result", username="peer1", folder_path=ALBUM)
    assert ready["status"] == "ready"
    assert ready["total_files"] == 2
    assert list(ready["folders"]) == [ALBUM]
    first = ready["folders"][ALBUM][0]
    assert first == {
        "name": "01 - Track 1.flac", "path": f"{ALBUM}\\01 - Track 1.flac", "size": 25_000_000,
        "bitrate": None, "duration": 200, "vbr": None, "sample_rate": 44100, "bit_depth": 16,
    }

    assert bridge.transfers() == [], "a listing request must never queue downloads"
    status = bridge.rpc("status")
    assert status["pending_folder_requests"] == []
    assert status["recent_folder_requests"][-1]["outcome"] == "listed 2 files"


def test_folder_contents_with_subfolders(bridge):
    bridge.rpc("folder_contents", username="peer1", folder_path=ALBUM, include_subfolders=True)
    bridge.send_folder_contents("peer1", ALBUM, {
        ALBUM: [(1, "01.flac", 1, "flac", bridge.make_attrs())],
        ALBUM + "\\CD2": [(1, "01.flac", 2, "flac", bridge.make_attrs())],
        "@@music\\Test Artist\\Other": [(1, "x.flac", 3, "flac", bridge.make_attrs())],
    })
    ready = bridge.rpc("folder_contents_result", username="peer1", folder_path=ALBUM)
    assert ready["status"] == "ready"
    assert set(ready["folders"]) == {ALBUM, ALBUM + "\\CD2"}
    assert ready["total_files"] == 2
    assert bridge.transfers() == []


def test_folder_contents_unknown_and_timed_out(bridge):
    unknown = bridge.rpc("folder_contents_result", username="ghost", folder_path=ALBUM)
    assert unknown["status"] == "unknown"

    bridge.rpc("folder_contents", username="ghost", folder_path=ALBUM)

    def age_request():
        bridge.plugin._pending_folders[("ghost", ALBUM)]["requested"] -= 10_000

    bridge.on_main(age_request)
    assert bridge.rpc("folder_contents_result", username="ghost", folder_path=ALBUM)["status"] == "timed_out"


def test_download_folder_still_downloads(bridge):
    """v1 behaviour is unchanged: download_folder queues files when the listing arrives."""
    bridge.rpc("download_folder", username="peer1", folder_path=ALBUM)
    bridge.send_folder_contents("peer1", ALBUM, {ALBUM: [(1, "01.flac", 1, "flac", bridge.make_attrs())]})
    assert [t.virtual_path for t in bridge.transfers()] == [f"{ALBUM}\\01.flac"]
    assert bridge.rpc("folder_contents_result", username="peer1", folder_path=ALBUM)["status"] == "unknown"
