"""Helpers for library-server tests: synthetic tagged FLACs and a canned MusicBrainz."""

import struct
import urllib.parse

from pathlib import Path

from claude_music_library.textnorm import normalize


def make_flac(path: Path, seconds=200, sample_rate=44100, **tags):
    """Write a FLAC file with only a STREAMINFO block (mutagen reads length and accepts Vorbis tags)."""
    from mutagen.flac import FLAC

    total = int(seconds * sample_rate)
    packed = (sample_rate << 44) | ((2 - 1) << 41) | ((16 - 1) << 36) | total
    body = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    header = bytes([0x80]) + len(body).to_bytes(3, "big")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + header + body)

    if tags:
        audio = FLAC(str(path))

        for key, value in tags.items():
            audio[key] = str(value)

        audio.save()

    return path


def recording(rec_id, title, artist, length_ms, release_id=None, release_title=None, track_count=None, score=100):
    entry = {
        "id": rec_id, "title": title, "length": length_ms, "score": score,
        "artist-credit": [{"name": artist, "artist": {"id": "ar-" + rec_id, "name": artist}}],
        "releases": [],
    }

    if release_id:
        entry["releases"].append({
            "id": release_id, "title": release_title or title, "status": "Official", "date": "2001-01-01",
            "release-group": {"primary-type": "Album", "secondary-types": []},
            "media": [{"format": "CD", "position": 1, "track-count": track_count}],
        })

    return entry


class FakeMusicBrainz:
    """fetch(url, user_agent) replacement: answers recording searches and ISRC lookups from a table."""

    def __init__(self, recordings: list[dict], isrcs: dict | None = None):
        self.by_title = {}

        for rec in recordings:
            self.by_title.setdefault(normalize(rec["title"]), []).append(rec)

        self.isrcs = isrcs or {}
        self.urls: list[str] = []

    def __call__(self, url, user_agent):
        self.urls.append(url)
        assert "claude-music/" in user_agent
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        if "/isrc/" in parsed.path:
            isrc = parsed.path.rsplit("/", 1)[1]
            return {"isrc": isrc, "recordings": self.isrcs.get(isrc, [])}

        query = params.get("query", [""])[0]
        wanted = query.split('recording:"', 1)[1].split('"', 1)[0].replace("\\", "")
        found = []

        for title_norm, recs in self.by_title.items():
            if title_norm and (title_norm in normalize(wanted) or normalize(wanted) in title_norm):
                found.extend(recs)

        return {"count": len(found), "recordings": found}
