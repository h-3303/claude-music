# nicotine-mcp

Lets Claude (Claude Code, or Claude Desktop) drive a running Nicotine+ client: search Soulseek, inspect results, queue files or whole folders, and manage downloads.

```
Claude Code ──stdio──▶ nicotine-mcp (uv script) ──Unix socket──▶ MCP Bridge plugin ──▶ Nicotine+ core
```

Tested against Nicotine+ 3.3.10, 3.3.11 and 3.4.0.dev2, with MCP Python SDK 2.x.

## Install

```bash
sudo pacman -S --needed nicotine+ uv
./install.sh
```

The installer does four things:

- copies `nicotine-plugin/mcp_bridge` into `~/.local/share/nicotine/plugins/` (or the Flatpak data directory, if that's your install);
- installs the server as `~/.local/bin/nicotine-mcp`;
- pre-fetches its dependencies;
- runs `claude mcp add --scope user nicotine -- uv run --script ~/.local/bin/nicotine-mcp`.

After that, open Nicotine+ → Preferences → Plugins, enable plugins, and tick **MCP Bridge**. The Nicotine+ log should show `MCP bridge listening on …`. Then `claude mcp list` should report `nicotine` as connected.

For Claude Desktop or any other MCP client, register the command `uv run --script ~/.local/bin/nicotine-mcp` as a stdio server.

## Tools

| Tool | Purpose |
|---|---|
| `nicotine_status` | Connection, username, download folder, counts by status, tracked searches, folder-request outcomes |
| `search` | Start a search, wait `wait_seconds`, return results grouped by user and folder, best first |
| `get_search_results` | Re-read a search as more results arrive; filter by `lossless_only`, `extensions`, `min_bitrate`, `free_slot_only`, `username`, `path_contains` |
| `list_searches` / `stop_search` | Manage tracked searches (the oldest are dropped past the configured limit) |
| `download_files` | Queue files by result id. By default the remote album folder name is kept |
| `download_folder` | Queue a whole remote folder, optionally including its subfolders |
| `list_downloads` | Status, progress (0–1), speed and queue position, each with a `download_id` |
| `cancel_downloads` / `retry_downloads` / `clear_downloads` | Act on downloads by id. `clear_downloads` requires ids or statuses and never deletes files from disk |

## Plugin settings (Preferences → Plugins → MCP Bridge)

- **Socket path**: leave empty for `$XDG_RUNTIME_DIR/nicotine-mcp.sock`. Flatpak installs automatically use `$XDG_RUNTIME_DIR/app/org.nicotine_plus.Nicotine/nicotine-mcp.sock`. If you set a custom path, export the same path as `NICOTINE_MCP_SOCKET` for the server, for example with `claude mcp add --env NICOTINE_MCP_SOCKET=…`.
- **Allow downloads**: switch this off for search-only access.
- **Max results per search / max searches kept**: memory caps.

## Security model

- **Access control:** the socket file is `0600`, and the plugin also rejects any peer whose UID (from `SO_PEERCRED`) doesn't match its own. There is no TCP listener, so browsers and other local users can't reach it.
- **Stability:** all requests are run on Nicotine+'s main loop. Errors are caught before they reach Nicotine+'s event bus, because an uncaught exception in a main-thread callback makes Nicotine+ quit.

## Development

Tests run a headless Nicotine+ core in-process with fake peers, load the bridge through Nicotine+'s
real plugin loader, and drive the MCP server over stdio. Nothing touches the network or the real
Soulseek network. The Nicotine+ source is cloned once (needs the network the first time) into
`~/.cache/claude-music/nicotine-plus/<ref>`.

```bash
uv sync                                   # dev deps: pytest, mcp
uv run pytest                             # against Nicotine+ 3.3.10 (default)
NICOTINE_PLUS_REF=master uv run pytest    # against current Nicotine+ master
tests/run_matrix.sh                       # both, one process per version
NICOTINE_PLUS_SRC=/usr/lib/python3.14/site-packages uv run pytest   # the installed package
```

`NICOTINE_PLUS_OFFLINE=1` forbids cloning (tests skip if the ref is not cached);
`NICOTINE_PLUS_UPDATE=1` pulls the latest commit for branch refs.

## Notes

- **Search results:** they trickle in from peers for a minute or more. Call `get_search_results` again for a fuller picture.
- **Folder downloads:** these ask the peer for the folder listing first, so the files appear in `list_downloads` a few seconds later. Requests that get no answer are reported as timed out in `nicotine_status` after 3 minutes.
- **Nicotine+ 3.3 large folders:** for folders over 100 files, Nicotine+ also shows its own confirmation dialog. The bridge queues the files regardless, and duplicate queue entries are ignored.
- **Headless use:** `nicotine --headless` runs Nicotine+ without a GUI. Enable the plugin once from the GUI first, since the setting persists.
- **Protocol:** one JSON request per connection, of the form `{"method": ..., "params": {...}}\n`. See the `HANDLERS` table in the plugin to add methods.
