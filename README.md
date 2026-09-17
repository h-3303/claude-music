# claude-music

**Site:** https://claude-music-liard.vercel.app · **Repo:** https://github.com/h-3303/claude-music ·
**Install:** `/plugin marketplace add h-3303/claude-music` then `/plugin install claude-music@claude-music`

A Claude Code plugin that gets a streaming playlist onto disk: import the playlist file, canonicalise
it against MusicBrainz, see what you already have, fetch the rest from Soulseek through your own running
Nicotine+ (one identity, your shares intact), and write an M3U in the original order. Nothing is
downloaded until you have seen the totals and said yes. Afterwards, `/music-tidy` puts the library in
order: tags normalised, lossy duplicates of FLACs dropped, everything filed as `Artist/Album/NN - Title`.

```
Claude Code ─┬─stdio─▶ library server (playlists · MusicBrainz · local index · matching)
             │                                   │ Unix socket
             └─stdio─▶ nicotine server ──────────┴─▶ MCP Bridge plugin ──▶ Nicotine+ core
```

Everything runs locally. The only network traffic is MusicBrainz lookups (1 request/s, cached) and
Soulseek traffic through Nicotine+. No telemetry, no hosted services.

Tested against Nicotine+ 3.3.10, 3.3.11 and master (3.4.0.dev2) with MCP Python SDK 2.x.

## Install

Two halves: the Claude Code plugin, and a small bridge plugin that runs inside Nicotine+. Any distro with
Nicotine+ 3.3 or newer and [uv](https://docs.astral.sh/uv/) works.

**1. The plugin, from inside Claude Code:**

```
/plugin marketplace add h-3303/claude-music
/plugin install claude-music@claude-music
```

**2. The Nicotine+ bridge, from a shell** (the marketplace add above cloned the repository):

```bash
sudo pacman -S --needed nicotine+ uv          # Arch; use your distro's packages elsewhere
bash ~/.claude/plugins/marketplaces/claude-music/install.sh
```

Or do both at once from a checkout of your own:

```bash
git clone https://github.com/h-3303/claude-music ~/src/claude-music && cd ~/src/claude-music
./install.sh
```

The installer:

- copies `nicotine-plugin/mcp_bridge` into Nicotine+'s plugin folder (native or Flatpak);
- installs the standalone `nicotine-mcp` server to `~/.local/bin` for Claude Desktop and other MCP clients;
- registers the repository as a Claude Code marketplace if it is not one already, and installs or updates
  the `claude-music` plugin from it;
- builds the library server's venv in the plugin's persistent data directory.

Then: Nicotine+ → Preferences → Plugins → enable plugins → tick **MCP Bridge** (re-tick it after
upgrading: the plugin is only reloaded when toggled). The Nicotine+ log shows
`MCP bridge listening on …`. In Claude Code, `/plugin` should list `claude-music` and every session
starts with a one-line health check of the bridge and the venv; if the bridge is missing, the check
says which command to run.

To update later: `/plugin marketplace update claude-music` then `/plugin update claude-music@claude-music`
for the Claude side, and `install.sh` again for the Nicotine+ side (then re-tick the plugin).

Plugin settings (`/plugin` → configure): music library folder (default `~/Music`), a MusicBrainz
contact (email or URL, sent in the User-Agent as their API terms ask), and the bridge socket path if
you changed it in Nicotine+.

## Use

Say `/playlist-sync` or just hand Claude a playlist file. The skill walks through:

1. `import_playlist_file` — Spotify data export (`Playlist1.json`, `YourLibrary.json`), Exportify CSV,
   generic CSV, M3U/M3U8, JSPF, XSPF — or `import_remote_playlist` straight from TIDAL, Deezer or
   YouTube Music (see [Services](#services)). Each playlist becomes a JSPF file under the plugin data dir.
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

### Tidy

Say `/music-tidy` (or "sort the new downloads"). The `music-tidy` skill drives two tools on the library
server and one decisions file:

1. `tidy_analyse` — a dry run. Writes `<library>/.tidy/report.txt` and `plan.json`, returns counts and
   the open questions, changes nothing. Refuses to be applied while audio files are still being written.
2. Open questions are settled in `<library>/.tidy/approved.py`, which is persistent and cumulative:
   canonical artist spellings, album fills, date picks, playlist dumps to delete, duplicate edits to
   drop, booklets and cover art to file with their album. Claude proposes, you decide, the file remembers.
3. `tidy_apply(confirm=True)` — only after the plan (every deletion by name) has been shown and approved.
   Backs up every raw tag to `.tidy/backups/`, writes tags, deletes, moves, files download reports into
   `.tidy/reports/`, prunes empty folders, and logs each action with its rule to `.tidy/tag_changes.log`.

Target state: `Artist/Album/NN - Title.ext`; ARTIST, ALBUMARTIST, ALBUM, TITLE, TRACKNUMBER on every
file; DATE as `YYYY` or `YYYY-MM-DD` and identical across an album; guests as `Primary feat. Guest` in
ARTIST only; one copy per song with FLAC beating lossy. The rules the planner applies on its own (trim
whitespace, normalise `feat.`, set ALBUMARTIST, unify album spelling, propagate dates, integer track
numbers, FLAC beats lossy, move, prune) are listed in the skill; everything else waits for a decision.
FLAC, MP3, M4A, Ogg and Opus are handled. The same planner runs from a shell as
`uv run --project plugins/claude-music/servers/library claude-music-tidy analyse|apply [root]`.

### Services

`connect_service`, `service_status`, `list_remote_playlists`, `import_remote_playlist` and
`disconnect_service` talk to streaming services directly. Everything stays local: tokens and cookies are
0600 files under the plugin data dir (`auth/`), never in the repo or in `settings.json`.

- **TIDAL** — the official API (`openapi.tidal.com/v2`), Authorization Code with PKCE, redirect to a
  loopback listener. You need your own app: at [developer.tidal.com/dashboard](https://developer.tidal.com/dashboard)
  create one, add the redirect URI `http://127.0.0.1:43117/callback` and the scopes `playlists.read`
  and `user.read`, then put its client id in the plugin's `tidal_client_id` setting (`/plugin` →
  claude-music → configure, or `CLAUDE_MUSIC_TIDAL_CLIENT_ID`). `connect_service("tidal")` opens the
  login page; call it again after approving. Playlists arrive with ISRCs and durations. Another redirect
  port: set `CLAUDE_MUSIC_TIDAL_REDIRECT_URI` to a matching `http://127.0.0.1:<port>/...`.
- **Deezer** — public playlists by id or share URL, no login (`api.deezer.com`). ISRCs and durations
  included. `list_remote_playlists("deezer", user=<user id or profile URL>)` lists someone's public
  playlists. OAuth for private playlists is not implemented.
- **YouTube Music** — through [ytmusicapi](https://github.com/sigma67/ytmusicapi), which is unofficial
  and authenticates with request headers copied from a logged-in browser tab (`service_status` prints
  the steps; `connect_service("youtube-music", headers_raw=...)` stores them). Titles, artists, albums and
  durations only; MusicBrainz fills in the rest. It breaks when YouTube changes its pages: update
  `ytmusicapi` (`uv sync` in `servers/library`) or reconnect.

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
stdio server. The bridge and this server are also published on their own, without the playlist and
library tooling, as [nicotine-mcp](https://github.com/h-3303/nicotine-mcp).

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
(the Claude Code plugin: manifest, `.mcp.json`, `servers/nicotine_mcp.py`, `servers/library/` with the
service connectors under `connectors/`, the `playlist-sync` and `music-tidy` skills, agent, hook), `tests/`, `docs/ROADMAP.md`, `site/` (the static site, deployed to
Vercel from that folder). Licence: GPL-3.0-or-later.
