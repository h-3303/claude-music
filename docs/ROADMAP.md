# claude-music — development brief for Claude Code

You are continuing development of this repository. Read this whole file before doing anything. It is both the project roadmap and your instructions. Facts marked "verified 2026-09-16" were checked against live sources on that date; re-check the linked source before depending on a detail, because these APIs change often.

## First steps (do these in order, then stop and report)

1. Inspect the repo: `git log --oneline`, file tree, README. Confirm what's described under "Current state" matches reality; report any drift.
2. Rebuild the test harness described under "Testing" so the existing bridge and MCP server have automated tests before you change them. Get them passing against Nicotine+ 3.3.10 (what Arch ships) and the current Nicotine+ master.
3. Propose the repo restructure (see "Target layout") as a short plan and wait for my go-ahead before moving files.
4. After approval, implement Phase 1 only. Stop at the end of Phase 1 with a summary, test results, and a single copy-pasteable command block for me to try it.

Working rules:
- Commit in small, logical commits with clear messages. Never push without asking me.
- Ask before anything that would break the existing `install.sh` flow or the Nicotine+ bridge protocol for existing clients.
- Any setup/terminal instructions you give me: one copy-pasteable block unless steps genuinely need to be separate.
- No telemetry, no third-party hosted services, no uploading my data anywhere. Everything runs locally.
- Tests never touch the real network or the real Soulseek network; use fixtures and mocks.

## Goal

Turn the existing Nicotine+ MCP bridge into a full Claude Code plugin ("claude-music") that:
1. imports tracklists from my streaming-service playlists (file exports first, APIs later),
2. canonicalises tracks against MusicBrainz,
3. works out which tracks I already have locally,
4. finds and queues the missing ones on Soulseek through my running Nicotine+ (one Soulseek identity, my shares intact), with my approval,
5. tags the results and writes M3U playlists in the original order.

## Current state (what already exists)

- `plugin/mcp_bridge/` — a Nicotine+ plugin (stdlib only, GPL-3.0-or-later). Listens on a Unix socket, default `$XDG_RUNTIME_DIR/nicotine-mcp.sock` (Flatpak: `$XDG_RUNTIME_DIR/app/org.nicotine_plus.Nicotine/nicotine-mcp.sock`, overridable via plugin setting or `NICOTINE_MCP_SOCKET`). Socket is 0600 and peers are checked with `SO_PEERCRED`.
  - Wire protocol v1: one request per connection, `{"method": ..., "params": {...}}\n` → `{"ok": true, "result": ...}\n` or `{"ok": false, "error": "..."}\n`.
  - Methods (`HANDLERS` table): `status`, `list_searches`, `search`, `search_results`, `stop_search`, `download_results`, `download_folder`, `list_downloads`, `cancel_downloads`, `retry_downloads`, `clear_downloads`.
  - Settings: `socket_path`, `allow_downloads`, `max_results_per_search` (5000), `max_searches_kept` (20).
- `server/nicotine_mcp.py` — single-file stdio MCP server run with `uv run --script`, inline deps `mcp>=2.2,<3`. Groups search results by (user, folder), ranks by free slot / queue / speed, labels quality (e.g. `flac 16bit 44.1kHz`).
- `install.sh` — copies the Nicotine+ plugin, installs the server to `~/.local/bin/nicotine-mcp`, runs `uv sync --script`, registers with `claude mcp add --scope user nicotine`.

Hard-won implementation facts (keep these true):
- **MCP Python SDK 2.x** renamed FastMCP: use `from mcp.server.mcpserver import MCPServer`. Raise `mcp.server.mcpserver.exceptions.ToolError` for expected failures; any other exception reaches the model only as a generic "Error executing tool". `ToolAnnotations` fields are snake_case (`read_only_hint`, etc.).
- **Nicotine+ threading**: all core calls must run on the main loop. The bridge marshals every handler through `events.invoke_main_thread`. An exception escaping a `thread-callback` makes Nicotine+ **quit** (the event bus attributes it to `pynicotine`, not the plugin), so every callback must trap all exceptions.
- **Search token capture**: `do_search()` doesn't return the token and the attribute name differs by version (`core.search.token` in 3.3, `_token` in 3.4). The bridge captures it by listening to the `add-search` event during the call; keep that approach. Pass `switch_page=False`.
- **Version differences handled**: file attributes are a dict keyed by codes (0 bitrate, 1 duration, 2 vbr, 4 sample rate, 5 bit depth) in 3.3 but a `FileAttributes` object (`bitrate`, `length`, `vbr`, `sample_rate`, `bit_depth`) in 3.4. Always pass the original attrs object back to `enqueue_download`. Folder requests are `downloads.enqueue_folder()` in 3.3 and `downloads.request_folder()` in 3.4; the bridge listens to `folder-contents-response` itself and enqueues files (including subfolders when asked), so behaviour is identical on both.
- `BasePlugin.log(msg, msg_args)` takes a single args value, not varargs.
- `plugin.init()` runs before the plugin is registered; the bridge starts the socket first so a bind failure fails the load cleanly.
- Each bridge search opens a tab in the Nicotine+ GUI. Batch matching must call `stop_search` after harvesting results.

## Verified external facts (2026-09-16)

**Spotify Web API** — https://developer.spotify.com/documentation/web-api/references/changes/february-2026 and the migration guide.
- Dev-mode apps: owner needs active Premium; new apps limited to 1 client ID and 5 users.
- Playlist contents come from `GET /playlists/{id}/items` (fields renamed `tracks → items`, `track → item`). Contents are only returned for the current user's own playlists; other playlists return metadata only. `GET /users/{id}/playlists` was removed; use `GET /me/playlists`.
- `external_ids` (ISRC) removal was reverted, so ISRCs are available.
- Redirect URIs must use `http://127.0.0.1:<port>/...`, never `localhost`. Use Authorization Code with PKCE.
- **Developer Policy clause 14 / Terms IV.2.a.i** prohibit ingesting Spotify Content from the platform into an AI model. Therefore: the Spotify **account data export** ("Download your data": playlist JSON with playlist name, track, artist and album names) is the primary Spotify path. A Spotify API connector is optional, Phase 4, disabled by default, and I decide whether to build it.
- Exportify CSV (self-hostable, runs in the browser) columns include Track URI, Track Name, Artist Name(s), Album Name, Track Duration (ms), ISRC.

**TIDAL** — official JSON:API at `https://openapi.tidal.com/v2` (developer.tidal.com), OAuth 2 PKCE, scope `playlists.read` for user playlists. `tidalapi` (Python, unofficial) is the fallback.

**Deezer** — public REST API; public playlists/catalog/search need no auth; user-scoped data needs OAuth.

**YouTube Music** — no official route. `ytmusicapi` (Python) authenticates with browser cookie headers; its OAuth mode needs my own YouTube Data API client ID/secret. Treat it as fragile: wrap in version checks and clear errors.

**Apple Music** — library access needs a MusicKit developer token (Apple Developer Program, $99/yr). Out of scope except for parsing Apple's privacy data export if I provide one.

**Sockseek** (formerly sldl / slsk-batchdl, AGPL-3.0) — https://github.com/fiso64/sockseek. Do not vendor its code (licence and a separate Soulseek account requirement). Copy its lessons:
- Soulseek bans a user for 30 minutes after too many searches in a short window; Sockseek defaults to 34 searches per 220 s.
- Default duration tolerance is 3 s, but CD rips often differ from streaming lengths by more.
- For albums, the expected track count is the cleanest correctness guard.
- It keeps an index file so re-runs skip completed items.
- Query cleaning: remove "feat."/"ft." and bracketed text; strict title/artist/album path checks as preferences.

**Troi** (ListenBrainz, `pip install troi`) — can scan a local collection and resolve MBID-based JSPF playlists to local files / M3U, but only for MusicBrainz-tagged collections. Optional helper, not a hard dependency.

**MusicBrainz API** — max 1 request/second per client and a meaningful `User-Agent` (`claude-music/<version> ( <contact> )`) are required. ISRC lookup: `/ws/2/isrc/<isrc>?inc=releases+artist-credits&fmt=json`. Cache responses in SQLite.

**Claude Code plugins** — https://code.claude.com/docs/en/plugins-reference
- Plugin root contains `.claude-plugin/plugin.json` (only the manifest goes in that folder), plus `skills/`, `agents/`, `hooks/hooks.json`, `.mcp.json`, `monitors/monitors.json` (experimental).
- `${CLAUDE_PLUGIN_ROOT}` = install dir (ephemeral across updates); `${CLAUDE_PLUGIN_DATA}` = persistent dir (`~/.claude/plugins/data/<id>/`) for venvs, caches, databases. Both are substituted in `.mcp.json` `command`/`args`/`env`.
- `userConfig` prompts for values at enable time; `${user_config.KEY}` works in MCP config. On Linux without a supported keychain, `sensitive` values land in `~/.claude/.credentials.json`, so store OAuth refresh tokens in our own 0600 file under `${CLAUDE_PLUGIN_DATA}` instead.
- Plugin-shipped agents cannot declare `hooks`, `mcpServers`, or `permissionMode`.
- Components can't reference paths outside the plugin root.
- Dev loop: `claude --plugin-dir <path>`, `/reload-plugins`, `claude plugin validate <path> --strict`.
- A repo can be a marketplace via `.claude-plugin/marketplace.json` at its root.

## Target layout

```
<repo root>
├── .claude-plugin/marketplace.json      # lists ./plugins/claude-music
├── plugins/claude-music/
│   ├── .claude-plugin/plugin.json       # name, version, userConfig (music_dir, bridge socket, TIDAL client id, ...)
│   ├── .mcp.json                        # "nicotine" + "library" servers
│   ├── servers/nicotine_mcp.py          # existing server, moved
│   ├── servers/library/                 # uv project: pyproject.toml, src/claude_music_library/...
│   ├── skills/playlist-sync/SKILL.md    # workflow + guardrails
│   ├── agents/matcher.md                # reviews candidates for big playlists in its own context
│   ├── hooks/hooks.json                 # SessionStart: bridge reachable? venv in sync?
│   └── monitors/monitors.json           # Phase 3: download-completion notifications
├── nicotine-plugin/mcp_bridge/          # installed into Nicotine+ separately (install.sh)
├── tests/                               # pytest; fixtures; Nicotine+ harness
├── install.sh                           # installs Nicotine+ plugin + adds the marketplace/plugin
├── LICENSE (GPL-3.0)  README.md  docs/ROADMAP.md
```

Run the library server as a uv project with its venv in persistent storage, e.g. in `.mcp.json`:
`"command": "uv", "args": ["run", "--project", "${CLAUDE_PLUGIN_ROOT}/servers/library", "claude-music-library"], "env": {"UV_PROJECT_ENVIRONMENT": "${CLAUDE_PLUGIN_DATA}/venv"}`.
Verify this works with the installed uv before committing to it.

Keep `install.sh` working throughout; after the restructure it should install the Nicotine+ plugin and add the local marketplace / install the Claude plugin instead of registering the server by hand.

## Data model

Interchange format: **JSPF** (JSON XSPF with the MusicBrainz extension), one file per imported playlist under `${CLAUDE_PLUGIN_DATA}/playlists/`.

State: SQLite at `${CLAUDE_PLUGIN_DATA}/state.db`, migrations versioned from day one.
- `playlists(id, source, source_ref, name, imported_at, jspf_path, track_count)`
- `tracks(id, playlist_id, position, title, artist, album, duration_ms, isrc, mb_recording_id, mb_release_id, mb_release_track_count, source_uri)`
- `matches(track_id PK, status, local_path, candidate_json, confidence, bridge_search_id, download_id, attempts, last_error, updated_at)`
  - statuses: `pending, in_library, searching, candidates, approved, queued, downloading, done, not_found, failed, skipped`
- `mb_cache(key PK, json, fetched_at)`

Everything is resumable: a crash or restart mid-run must continue from the table state.

## Library MCP server — tool surface

Return compact summaries and IDs. Never dump whole tracklists into the conversation unless I ask for a specific slice.

- `import_playlist_file(path, format="auto")` → `{playlist_id, name, tracks, detected_format, warnings}`
  - Formats: Spotify data export (`Playlist*.json`, `YourLibrary.json`), Exportify CSV, generic CSV (column mapping), M3U/M3U8 with EXTINF, JSPF, XSPF.
  - If a Spotify export contains several playlists, import each (or let me pick by name).
  - The Spotify export has no durations; mark those tracks so MusicBrainz fills them in.
- `list_playlists()`, `playlist_status(playlist_id)` → counts by status, plus the active job if any.
- `resolve_playlist(playlist_id)` → MusicBrainz canonicalisation (ISRC first, then artist + title + duration search), rate-limited, cached; reports unresolved tracks.
- `scan_library(music_dir=None, rescan=False)` → indexes local files with mutagen (tags, duration, MBIDs if present) into SQLite.
- `diff_library(playlist_id)` → marks `in_library` with `local_path`. Match on MBID, then ISRC, then normalised artist + title + duration within tolerance.
- `match_playlist(playlist_id, prefer_formats=["flac"], allow_formats=[...], min_bitrate=None, album_mode="auto", max_tracks=None)` → starts a background job (asyncio task in the server process, state in SQLite) that searches via the bridge, scores candidates, stores the best few per track, and calls `stop_search` after harvesting. Returns a job id.
- `review_candidates(playlist_id, status="candidates", min_confidence=None, limit=25, offset=0)`
- `approve(track_ids=None, playlist_id=None, min_confidence=None)` → shows what will be approved.
- `queue_approved(playlist_id, confirm=False)` → without `confirm=True`, returns the count, total size and users involved; with it, queues via the bridge (`download_results`, or `download_folder` for album mode).
- `sync_downloads(playlist_id)` → maps bridge `download_id`s to statuses and marks `done` with the local path.
- `write_m3u(playlist_id, path=None, relative_to=None)` → original order; tracks not yet owned are reported, not silently dropped.

The `playlist-sync` skill must tell Claude to:
- always show `queue_approved` totals and wait for my explicit OK;
- prefer album mode when most of an album is missing;
- never raise rate limits without asking.

## Matching design

- **Query building:** artist + title, cleaned (feat./ft., bracketed text, punctuation — Soulseek matches words against the full path). Fallbacks: title + album, then title only, with a stricter artist check on results.
- **Score components:**
  - title tokens in filename;
  - artist in path;
  - album in folder;
  - duration within tolerance (default 3 s, widen to ~10 s for lossless/CD rips, configurable);
  - format preference;
  - bitrate / sample rate / bit depth;
  - free slot, queue length, speed.
- **Confidence:** a 0–1 value with a documented formula, plus a unit-tested table of example cases.
- **Album mode:** when ≥ N tracks (default 3) of one MusicBrainz release are missing, search for the album, check candidate folders against `mb_release_track_count` (inspect the folder listing first; see bridge v2), and queue the whole folder.
- **Retries:** failed or stalled downloads retry the next candidate up to a limit; users who fail twice are downranked for the rest of the job.

## Bridge protocol v2 (Nicotine+ plugin)

Keep every v1 method and response shape working; add fields only. Bump `PROTOCOL_VERSION` to 2 and have the MCP servers accept 1 or 2 where possible.

- **Search rate limiter in the plugin** (authoritative, since any client can call it): token bucket, default 34 searches / 220 s, both configurable in plugin settings. When exhausted, return `{"ok": false, "error": "rate_limited", "retry_after": <s>}` rather than blocking the main loop. The library server backs off accordingly and reports "waiting on rate limit" in `playlist_status`.
- **`folder_contents(username, folder_path, include_subfolders)`:** requests the listing without downloading. Results are stored for `folder_contents_result(...)` polling, reusing the existing `folder-contents-response` handler with a mode flag.
- **`status`:** add `rate_limit: {available, capacity, window_s}`.
- Keep all callbacks exception-proof and all core calls on the main loop.

## Phases and acceptance criteria

**Phase 1 — offline pipeline (do this first)**
- Repo restructure (after my approval), plugin manifest, marketplace file, `.mcp.json`, `playlist-sync` skill, SessionStart hook.
- File importers, JSPF + SQLite model, MusicBrainz resolver with cache and 1 req/s limit, library scan + diff, matcher, `queue_approved` confirmation flow, `sync_downloads`, `write_m3u`.
- Bridge v2 with the rate limiter and `folder_contents`.
- Done when:
  - pytest passes (unit + harness integration on Nicotine+ 3.3.10 and master);
  - `claude plugin validate plugins/claude-music --strict` passes;
  - a synthetic end-to-end run (fixture Spotify export → resolve with mocked MusicBrainz → diff against a temp library of generated tagged files → match against the harness with fake peers → queue → M3U) produces the expected M3U;
  - README updated;
  - `install.sh` works from a clean checkout.

**Phase 2 — service connectors**
- TIDAL (official API, PKCE, loopback redirect on 127.0.0.1), Deezer (public playlists by URL/ID; OAuth only if I ask), YouTube Music (`ytmusicapi`, browser-header auth file under `${CLAUDE_PLUGIN_DATA}`, 0600).
- Common interface: `list_remote_playlists(service)`, `import_remote_playlist(service, id_or_url)`.
- Tokens stored 0600 under `${CLAUDE_PLUGIN_DATA}/auth/`, never in the repo or in settings.json.
- Recorded fixtures for tests; no live calls in CI.

**Phase 3 — finishing**
- Optional `beets` integration: detect `beet`, run `beet import` on completed album folders with MusicBrainz IDs as hints. Never move files without a dry-run summary first.
- Download-completion monitor (experimental component) that polls the bridge and emits one line per finished item.
- Optional Troi resolver if my library is MusicBrainz-tagged.
- Marketplace polish and versioning.

**Phase 4 — optional Spotify API connector (only if I explicitly ask)**
- Own dev app, PKCE, `/me/playlists` and `/playlists/{id}/items` only, disabled by default. Before building it, summarise the policy clause above and get my decision on how (or whether) API-fetched data may be used.

## Testing

Recreate this harness under `tests/` (it existed ad hoc during initial development):
- Clone Nicotine+ at tag `3.3.10` and at master into a cache dir (skip if offline), add to `sys.path`.
- Set the data folder and config file to a temp dir, then:
  `core.init_components(enabled_components={"users","downloads","search","network_filter","pluginhandler","shares","uploads","userbrowse"})`, `core.start()`, `core.users.login_status = UserStatus.ONLINE`.
- `shares` uses multiprocessing spawn, which re-imports the test entry module. Keep harness code under `if __name__ == "__main__":` or inside functions so the child doesn't re-run setup (it previously deleted the temp data dir and the socket).
- Pump the main loop with `events.process_thread_events()` in a loop.
- Inject fake peers:
  - search results: `events.emit_main_thread("file-search-response", msg)` with a `FileSearchResponse` (`msg.username`, `msg.addr`, `freeulslots`, `ulspeed`, `inqueue`, `list` of `(code, path, size, ext, attrs)`);
  - folder listings: `FolderContentsResponse` (`msg.dir`, `msg.username`, `msg.list` dict of folder → file tuples).
  - Build attrs as a dict on 3.3 and `FileAttributes` on 3.4.
- Also load the plugin through `core.pluginhandler.enable_plugin("mcp_bridge")` from `<data>/plugins/` to test the real loader. Check:
  - socket mode 0600;
  - duplicate enable is rejected;
  - error responses for bad JSON, unknown method, bad kwargs and offline state;
  - Nicotine+ keeps running after each error;
  - disable removes the socket;
  - re-enable works.
- Drive the MCP servers end-to-end with `mcp.client.stdio` + `ClientSession`.

## Environment

- CachyOS (Arch). Nicotine+ from pacman (`nicotine+`, 3.3.10 at time of writing), `uv` installed, Claude Code CLI.
- The Nicotine+ plugin runs in the system Python, so it must stay stdlib-only.
- Python ≥ 3.11 for the servers.
- Licence: GPL-3.0-or-later for the repo (the Nicotine+ plugin imports GPL code).
