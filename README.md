# claude-music

A Claude Code plugin that gets a streaming playlist onto disk: import the playlist file, canonicalise
it against MusicBrainz, see what you already have, fetch the rest from Soulseek through your own running
Nicotine+ (one identity, your shares intact), and write an M3U in the original order. Nothing is
downloaded until you have seen the totals and said yes.

```
Claude Code ─┬─stdio─▶ library server (playlists · MusicBrainz · local index · matching)
             │                                   │ Unix socket
             └─stdio─▶ nicotine server ──────────┴─▶ MCP Bridge plugin ──▶ Nicotine+ core
```

Everything runs locally. The only network traffic is MusicBrainz lookups (1 request/s, cached) and
Soulseek traffic through Nicotine+. No telemetry, no hosted services.

Tested against Nicotine+ 3.3.10, 3.3.11 and master (3.4.0.dev2) with MCP Python SDK 2.x.

## Install

```bash
sudo pacman -S --needed nicotine+ uv          # Arch; any distro with Nicotine+ 3.3+ and uv works
git clone <this repo> ~/src/claude-music && cd ~/src/claude-music
./install.sh
```

The installer:

- copies `nicotine-plugin/mcp_bridge` into Nicotine+'s plugin folder (native or Flatpak);
- installs the standalone `nicotine-mcp` server to `~/.local/bin` for Claude Desktop and other MCP clients;
- adds this checkout as a local Claude Code marketplace and installs the `claude-music` plugin from it
  (after a `git pull`, run `./install.sh` again to pick up changes);
- builds the library server's venv in the plugin's persistent data directory.

Then: Nicotine+ → Preferences → Plugins → enable plugins → tick **MCP Bridge** (re-tick it after
upgrading: the plugin is only reloaded when toggled). The Nicotine+ log shows
`MCP bridge listening on …`. In Claude Code, `/plugin` should list `claude-music` and every session
starts with a one-line health check of the bridge and the venv.

Plugin settings (`/plugin` → configure): music library folder (default `~/Music`), a MusicBrainz
contact (email or URL, sent in the User-Agent as their API terms ask), and the bridge socket path if
you changed it in Nicotine+.

## Use

Say `/playlist-sync` or just hand Claude a playlist file. The skill walks through:

1. `import_playlist_file` — Spotify data export (`Playlist1.json`, `YourLibrary.json`), Exportify CSV,
   generic CSV, M3U/M3U8, JSPF, XSPF. Each playlist becomes a JSPF file under the plugin data dir.
2. `resolve_playlist` — MusicBrainz ids, release track counts and (for Spotify exports) durations.
3. `scan_library` + `diff_library` — what you already own, matched by MusicBrainz id, ISRC, then
   normalised artist + title + duration.
4. `match_playlist` — a background job that searches Soulseek per track, scores results and keeps the
   best few candidates. When three or more tracks of one release are missing it searches for the album,
   asks the peer for the folder listing, checks the track count, and proposes the whole folder.
5. `review_candidates` / `approve` / `skip_tracks` — with a `why` breakdown per candidate. For big
   playlists the `matcher` agent reviews in its own context and returns a shortlist.
6. `queue_approved` — shows track count, size and users; only `confirm=True` queues anything.
7. `sync_downloads` — maps transfers to done/failed, retries failed ones from the next candidate.
8. `write_m3u` — original order, missing tracks reported rather than dropped.

State lives in `~/.claude/plugins/data/claude-music@claude-music/state.db`; every step is resumable.

### Spotify

Spotify's developer terms forbid feeding API data to an AI model, so the supported Spotify route is the
account data export (Privacy settings → Download your data), or an Exportify CSV. Both are plain files.

## The `nicotine` server on its own

The bridge is useful without the rest: search Soulseek, inspect results grouped by user and folder,
browse a folder without downloading, queue files or folders, manage downloads.

| Tool | Purpose |
|---|---|
| `nicotine_status` | Connection, username, download folder, counts by status, search rate limit, tracked searches |
| `search` | Start a search, wait `wait_seconds`, return results grouped by user and folder, best first |
| `get_search_results` | Re-read a search as more results arrive; filter by `lossless_only`, `extensions`, `min_bitrate`, `free_slot_only`, `username`, `path_contains` |
| `list_searches` / `stop_search` | Manage tracked searches (the oldest are dropped past the configured limit) |
| `browse_folder` / `get_folder_contents` | Ask a user for a folder listing without downloading (protocol v2) |
| `download_files` | Queue files by result id. By default the remote album folder name is kept |
| `download_folder` | Queue a whole remote folder, optionally including its subfolders |
| `list_downloads` | Status, progress (0–1), speed and queue position, each with a `download_id` |
| `cancel_downloads` / `retry_downloads` / `clear_downloads` | Act on downloads by id. `clear_downloads` never deletes files from disk |

For Claude Desktop or any other MCP client, register `uv run --script ~/.local/bin/nicotine-mcp` as a
stdio server.

## Nicotine+ plugin settings (Preferences → Plugins → MCP Bridge)

- **Socket path**: leave empty for `$XDG_RUNTIME_DIR/nicotine-mcp.sock`. Flatpak installs use
  `$XDG_RUNTIME_DIR/app/org.nicotine_plus.Nicotine/nicotine-mcp.sock`. A custom path must also be set
  in the plugin's `bridge_socket` setting (or `NICOTINE_MCP_SOCKET` for other clients).
- **Allow downloads**: switch off for search-only access.
- **Search rate limit / window**: default 34 searches per 220 s, enforced in the plugin for every client.
  Soulseek bans an account for 30 minutes when it is exceeded; the MCP clients back off when the plugin
  answers `rate_limited`.
- **Max results per search / max searches kept**: memory caps.

## Security model

- **Access control:** the socket file is `0600` and the plugin rejects any peer whose UID (from
  `SO_PEERCRED`) differs from its own. There is no TCP listener.
- **Stability:** all requests run on Nicotine+'s main loop; errors are caught before they reach the
  event bus, because an uncaught exception in a main-thread callback makes Nicotine+ quit.
- **Protocol:** one JSON request per connection, `{"method": ..., "params": {...}}\n`. Version 2 keeps
  every v1 method and response shape and adds `folder_contents`, `folder_contents_result`,
  `download_file`, a `rate_limit` field in `status` and the `rate_limited` error. See the `HANDLERS`
  table in the plugin.

## Notes

- **Search results** trickle in from peers for a minute or more; the matcher harvests for 10 s per
  query by default (`harvest_seconds`).
- **Folder downloads** ask the peer for the listing first, so files appear a few seconds later.
  Requests without an answer are reported as timed out after 3 minutes.
- **Nicotine+ 3.3 large folders:** for folders over 100 files Nicotine+ also shows its own confirmation
  dialog. The bridge queues the files regardless; duplicate queue entries are ignored.
- **Headless use:** `nicotine --headless` works once the plugin has been enabled from the GUI.

## Development

Tests run a headless Nicotine+ core in-process with fake peers, load the bridge through Nicotine+'s
real plugin loader, drive both MCP servers over stdio, and run the whole pipeline synthetically
(fixture Spotify export → canned MusicBrainz → generated tagged FLACs → fake Soulseek peers → M3U).
Nothing touches the network or the real Soulseek network. The Nicotine+ source is cloned once into
`~/.cache/claude-music/nicotine-plus/<ref>`.

```bash
uv sync                                   # dev deps: pytest, mcp, the library server (editable)
uv run pytest                             # against Nicotine+ 3.3.10 (default)
NICOTINE_PLUS_REF=master uv run pytest    # against current Nicotine+ master
tests/run_matrix.sh                       # both, one process per version
NICOTINE_PLUS_SRC=/usr/lib/python3.14/site-packages uv run pytest   # the installed package
claude plugin validate plugins/claude-music --strict
```

`NICOTINE_PLUS_OFFLINE=1` forbids cloning (tests skip if the ref is not cached);
`NICOTINE_PLUS_UPDATE=1` pulls the latest commit for branch refs. Dev loop for the plugin itself:
`claude --plugin-dir plugins/claude-music`, then `/reload-plugins` after edits.

Layout: `nicotine-plugin/mcp_bridge` (Nicotine+ plugin, stdlib only), `plugins/claude-music`
(the Claude Code plugin: manifest, `.mcp.json`, `servers/nicotine_mcp.py`, `servers/library/`, skill,
agent, hook), `tests/`, `docs/ROADMAP.md`. Licence: GPL-3.0-or-later.
