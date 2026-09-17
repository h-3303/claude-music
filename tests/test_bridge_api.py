"""Bridge API methods against a headless Nicotine+ with fake peers."""

import pytest

from harness import BridgeError

ALBUM = "@@music\\Test Artist\\Album (2001)"


def album_files(nicotine, count=3, ext="flac"):
    return [
        nicotine.make_file(f"{ALBUM}\\{i:02d} - Track {i}.{ext}", size=20_000_000 + i,
                           duration=180 + i, sample_rate=44100, bit_depth=16)
        for i in range(1, count + 1)
    ]


def test_search_starts_tracked_search(bridge):
    result = bridge.rpc("search", query="test artist album")
    token = result["search_id"]

    assert result == {"search_id": token, "query": "test artist album", "mode": "global"}
    assert token in bridge.on_main(lambda: dict(bridge.core.search.searches))

    searches = bridge.rpc("list_searches")
    assert [s["search_id"] for s in searches] == [token]
    assert searches[0]["files"] == 0 and searches[0]["users"] == 0


@pytest.mark.parametrize("params, message", [
    ({"query": "x", "mode": "bogus"}, "mode must be one of"),
    ({"query": "x", "mode": "user"}, "requires a list of usernames"),
    ({"query": "x", "mode": "rooms"}, "requires a room name"),
])
def test_search_validates_parameters(bridge, params, message):
    with pytest.raises(BridgeError, match=message):
        bridge.rpc("search", **params)


def test_search_results_record_version_specific_attributes(bridge, search):
    files = [
        bridge.make_file(f"{ALBUM}\\01 - One.flac", size=30_000_000, duration=200, sample_rate=44100, bit_depth=16),
        bridge.make_file(f"{ALBUM}\\02 - Two.mp3", size=8_000_000, duration=201, bitrate=320, vbr=False),
    ]
    bridge.send_search_response(search, "peer1", files, free_slots=1, speed=1_000_000, queue=0)

    data = bridge.rpc("search_results", search_id=search)
    assert data["total_files"] == 2
    assert data["total_users"] == 1
    assert data["matched"] == 2
    assert data["truncated"] is False

    flac, mp3 = data["results"]
    assert flac["path"].endswith("01 - One.flac")
    assert (flac["duration"], flac["sample_rate"], flac["bit_depth"], flac["bitrate"]) == (200, 44100, 16, None)
    assert (mp3["bitrate"], mp3["duration"], mp3["vbr"]) == (320, 201, False)
    assert flac["free_slot"] is True and flac["speed"] == 1_000_000 and flac["queue"] == 0
    assert flac["private"] is False
    assert "_attrs" not in flac, "internal attrs object must not leak into responses"


def test_private_results_are_flagged(bridge, search):
    public = [bridge.make_file(f"{ALBUM}\\01 - One.flac")]
    private = [bridge.make_file(f"{ALBUM}\\02 - Two.flac")]
    bridge.send_search_response(search, "peer1", public, private_files=private)

    results = bridge.rpc("search_results", search_id=search)["results"]
    assert [r["private"] for r in sorted(results, key=lambda r: r["path"])] == [False, True]


def test_responses_for_unknown_or_stopped_tokens_are_ignored(bridge, search):
    bridge.send_search_response(424242, "peer1", [bridge.make_file(f"{ALBUM}\\01 - One.flac")])
    assert bridge.rpc("search_results", search_id=search)["total_files"] == 0

    bridge.rpc("stop_search", search_id=search)
    bridge.send_search_response(search, "peer1", [bridge.make_file(f"{ALBUM}\\01 - One.flac")])

    with pytest.raises(BridgeError, match="unknown search_id"):
        bridge.rpc("search_results", search_id=search)


def test_search_results_filters_and_ranking(bridge, search):
    bridge.send_search_response(search, "busy", [
        bridge.make_file(f"{ALBUM}\\01 - One.flac", sample_rate=44100, bit_depth=16),
        bridge.make_file(f"{ALBUM}\\01 - One.mp3", bitrate=128),
    ], free_slots=0, speed=2_000_000, queue=12)
    bridge.send_search_response(search, "free", [
        bridge.make_file("@@shares\\Other\\Test Artist - One.mp3", bitrate=320),
        bridge.make_file("@@shares\\Other\\Test Artist - One.ogg", bitrate=192),
    ], free_slots=1, speed=100_000, queue=0)
    bridge.send_search_response(search, "slow", [
        bridge.make_file("@@a\\Test Artist\\One.flac"),
    ], free_slots=1, speed=10, queue=3)

    def paths(**filters):
        data = bridge.rpc("search_results", search_id=search, **filters)
        return [(r["user"], r["path"].rpartition("\\")[2]) for r in data["results"]]

    # free slot first, then shortest queue, then fastest
    assert [u for u, _ in paths()] == ["free", "free", "slow", "busy", "busy"]
    assert paths(lossless_only=True) == [("slow", "One.flac"), ("busy", "01 - One.flac")]
    assert paths(extensions=[".MP3"]) == [("free", "Test Artist - One.mp3"), ("busy", "01 - One.mp3")]
    assert paths(min_bitrate=256) == [("free", "Test Artist - One.mp3"), ("slow", "One.flac"), ("busy", "01 - One.flac")]
    assert paths(free_slot_only=True) == [("free", "Test Artist - One.mp3"), ("free", "Test Artist - One.ogg"), ("slow", "One.flac")]
    assert paths(username="busy") == [("busy", "01 - One.flac"), ("busy", "01 - One.mp3")]
    assert paths(path_contains="album 2001 flac") == [("busy", "01 - One.flac")]

    data = bridge.rpc("search_results", search_id=search, offset=1, limit=2)
    assert data["matched"] == 5 and data["offset"] == 1 and len(data["results"]) == 2


def test_result_cap_marks_search_truncated(bridge, search):
    bridge.set_plugin_setting("max_results_per_search", 100)
    try:
        files = [bridge.make_file(f"{ALBUM}\\{i:03d}.flac") for i in range(150)]
        bridge.send_search_response(search, "peer1", files)
        data = bridge.rpc("search_results", search_id=search, limit=5000)
        assert data["total_files"] == 100
        assert data["truncated"] is True
        assert [s["truncated"] for s in bridge.rpc("list_searches")] == [True]
    finally:
        bridge.set_plugin_setting("max_results_per_search", 5000)


def test_oldest_searches_are_evicted(bridge):
    bridge.set_plugin_setting("max_searches_kept", 2)
    try:
        tokens = [bridge.rpc("search", query=f"query {i}")["search_id"] for i in range(3)]
        kept = [s["search_id"] for s in bridge.rpc("list_searches")]
        assert kept == tokens[1:]
        core_tokens = bridge.on_main(lambda: set(bridge.core.search.searches))
        assert tokens[0] not in core_tokens and set(tokens[1:]) <= core_tokens
    finally:
        bridge.set_plugin_setting("max_searches_kept", 20)


def test_stop_search_removes_it_from_core(bridge, search):
    assert bridge.rpc("stop_search", search_id=search) == {"stopped": search}
    assert bridge.rpc("list_searches") == []
    assert search not in bridge.on_main(lambda: set(bridge.core.search.searches))


def test_download_results_enqueues_with_original_attrs(bridge, search):
    files = album_files(bridge, count=2)
    bridge.send_search_response(search, "peer1", files)

    result = bridge.rpc("download_results", search_id=search, result_ids=[0, 1, 7])
    assert [q["path"] for q in result["queued"]] == [f[1] for f in files]
    assert result["errors"] == [{"result_id": 7, "error": "no such result"}]
    assert all(len(q["download_id"]) == 12 for q in result["queued"])

    transfers = {t.virtual_path: t for t in bridge.transfers()}
    assert set(transfers) == {f[1] for f in files}

    for code, path, size, _ext, attrs in files:
        transfer = transfers[path]
        assert transfer.username == "peer1"
        assert transfer.size == size
        assert transfer.file_attributes is attrs, "the attrs object from the search response must be passed through"
        assert transfer.folder_path.endswith("Album (2001)")  # keep_folder_structure=True

    listed = bridge.rpc("list_downloads")
    assert listed["total"] == 2
    ids = {d["download_id"] for d in listed["downloads"]}
    assert ids == {q["download_id"] for q in result["queued"]}
    assert {d["status"] for d in listed["downloads"]} == {"Queued"}


def test_download_results_flat_when_not_keeping_structure(bridge, search):
    bridge.send_search_response(search, "peer1", album_files(bridge, count=1))
    bridge.rpc("download_results", search_id=search, result_ids=[0], keep_folder_structure=False)
    (transfer,) = bridge.transfers()
    assert transfer.folder_path in (None, "", bridge.on_main(bridge.core.downloads.get_default_download_folder))


def test_downloads_can_be_disabled(bridge, search):
    bridge.send_search_response(search, "peer1", album_files(bridge, count=1))
    bridge.set_plugin_setting("allow_downloads", False)

    for method, params in (
        ("download_results", {"search_id": search, "result_ids": [0]}),
        ("download_folder", {"username": "peer1", "folder_path": ALBUM}),
        ("cancel_downloads", {"download_ids": ["x"]}),
        ("retry_downloads", {"download_ids": ["x"]}),
        ("clear_downloads", {"statuses": ["Finished"]}),
    ):
        with pytest.raises(BridgeError, match="downloads are disabled"):
            bridge.rpc(method, **params)

    assert bridge.rpc("status")["allow_downloads"] is False
    assert bridge.transfers() == []


def test_download_folder_queues_files_when_peer_replies(bridge):
    result = bridge.rpc("download_folder", username="peer1", folder_path=ALBUM.replace("\\", "/") + "/")
    assert result["requested"] == {"user": "peer1", "folder": ALBUM}

    status = bridge.rpc("status")
    assert [(p["user"], p["folder"]) for p in status["pending_folder_requests"]] == [("peer1", ALBUM)]

    root = [(1, f"{i:02d} - Track {i}.flac", 25_000_000, "flac", bridge.make_attrs(duration=200)) for i in (1, 2)]
    sub = [(1, "cover.jpg", 300_000, "jpg", bridge.make_attrs())]
    bridge.send_folder_contents("peer1", ALBUM, {ALBUM: root, ALBUM + "\\Scans": sub})

    paths = sorted(t.virtual_path for t in bridge.transfers())
    assert paths == [f"{ALBUM}\\01 - Track 1.flac", f"{ALBUM}\\02 - Track 2.flac"]

    status = bridge.rpc("status")
    assert status["pending_folder_requests"] == []
    assert status["recent_folder_requests"][-1]["outcome"] == "queued 2 files"


def test_download_folder_with_subfolders(bridge):
    bridge.rpc("download_folder", username="peer1", folder_path=ALBUM, include_subfolders=True)
    root = [(1, "01 - Track 1.flac", 25_000_000, "flac", bridge.make_attrs(duration=200))]
    sub = [(1, "cover.jpg", 300_000, "jpg", bridge.make_attrs())]
    other = [(1, "unrelated.flac", 1, "flac", bridge.make_attrs())]
    bridge.send_folder_contents("peer1", ALBUM, {
        ALBUM: root, ALBUM + "\\Scans": sub, "@@music\\Test Artist\\Album (2001) Bonus": other,
    })

    transfers = {t.virtual_path: t for t in bridge.transfers()}
    assert set(transfers) == {f"{ALBUM}\\01 - Track 1.flac", f"{ALBUM}\\Scans\\cover.jpg"}
    assert transfers[f"{ALBUM}\\Scans\\cover.jpg"].folder_path.endswith("Scans")
    assert bridge.rpc("status")["recent_folder_requests"][-1]["outcome"] == "queued 2 files"


def test_empty_folder_reply_keeps_request_pending(bridge):
    bridge.rpc("download_folder", username="peer1", folder_path=ALBUM)
    bridge.send_folder_contents("peer1", ALBUM, {})
    assert bridge.transfers() == []
    assert len(bridge.rpc("status")["pending_folder_requests"]) == 1


def test_cancel_retry_clear_downloads(bridge, search):
    files = album_files(bridge, count=3)
    bridge.send_search_response(search, "peer1", files)
    queued = bridge.rpc("download_results", search_id=search, result_ids=[0, 1, 2])["queued"]
    ids = [q["download_id"] for q in queued]

    assert bridge.rpc("cancel_downloads", download_ids=ids[:2]) == {"cancelled": 2}
    by_id = {d["download_id"]: d for d in bridge.rpc("list_downloads")["downloads"]}
    assert by_id[ids[0]]["status"] == "Cancelled"
    assert by_id[ids[1]]["status"] == "Cancelled"
    assert by_id[ids[2]]["status"] == "Queued"

    assert bridge.rpc("list_downloads", statuses=["Cancelled"])["total"] == 2
    assert bridge.rpc("list_downloads", username="nobody")["total"] == 0
    assert len(bridge.rpc("list_downloads", limit=1)["downloads"]) == 1

    assert bridge.rpc("retry_downloads", download_ids=[ids[0]]) == {"retried": 1}
    by_id = {d["download_id"]: d for d in bridge.rpc("list_downloads")["downloads"]}
    assert by_id[ids[0]]["status"] != "Cancelled"

    assert bridge.rpc("clear_downloads", statuses=["Cancelled"]) == {"cleared": 1}
    assert bridge.rpc("clear_downloads", download_ids=[ids[2]]) == {"cleared": 1}
    assert bridge.rpc("list_downloads")["total"] == 1
    assert bridge.rpc("cancel_downloads", download_ids=["nonexistent"]) == {"cancelled": 0}

    counts = bridge.rpc("status")["downloads_by_status"]
    assert sum(counts.values()) == 1
