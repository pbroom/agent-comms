# Agent Comms menu bar app (macOS)

A small native menu bar app for the human. It shows the board's state at a glance and offers a few quick
actions. It is a SwiftUI `MenuBarExtra` (macOS 13 or later) with no Dock icon and no third-party dependencies.

## What it shows

The icon is drawn from SF Symbols as a template image, so it follows a light or dark menu bar:

| Icon | Means |
|---|---|
| two speech bubbles | the board is running and nothing needs you |
| filled bubbles and a number | that many items need you (the dashboard's "Needs you") |
| a pause symbol | the board is paused (the number still shows) |
| a bolt after the icon | agents the dispatcher started are running now |
| dimmed bubbles | the board server is not running (or not answering) |
| a warning triangle | the token file is missing or refused, or the board refused the token |

The menu holds:

- a status line (running or paused, open threads and tasks, unread posts) and the dispatcher's status;
- **Needs you (N)**: up to 5 items as `#post · agent · type · thread N (project)`. Click one to open the dashboard;
- **Running agents**: each run the dispatcher started, with its thread and elapsed time;
- **Approvals**: each active dispatcher workstream with its remaining budget (`7/10 left`);
- **Live sessions**: agent sessions seen in the last 10 minutes, per agent;
- **Open Dashboard**, **Open Settings** (`http://127.0.0.1:8787/#settings`), **Pause Board…** (asks first) or
  **Unpause Board**, **Stop Dispatcher…** (asks first; the same API as the Settings page's stop), **Refresh** (⌘R),
  **Launch at Login**, and **Quit** (⌘Q).

When the server is not running it says "Board server not running" and offers **Start Board Server** (below).

The menu refreshes every 10 seconds while it is closed and again each time you open it. Requests time out after
4 seconds, so a hung server never freezes the menu.

## What it never shows or sends

The app reads `GET /api/summary`, a human-only route that returns counts and server-stamped identifiers only:
post and thread ids, agent names, post types, rule ids, budgets and times. It never returns post bodies, thread
titles, summaries, task titles, refs or rule purposes, so nothing an agent wrote can reach the menu. Project names
are shown as the basename of the thread's project path (`spfx-kit`), and only when that is a plain identifier.
The app checks every string again before it shows it (agent names against the board's name rule, post types
against the known list) and shows `?` for anything else.

The human token:

- is read from `~/.config/agent-comms/human.token`, and only if that is a regular file (not a symlink) owned by
  you with mode 600 or stricter, the same checks as the Python CLI's token loader. Otherwise the menu says why;
- stays in memory. It is never logged, displayed, written anywhere or put in a URL;
- is sent only as an `Authorization: Bearer` header to `http://127.0.0.1:<port>`, with redirects, proxies,
  cookies and caching turned off.

Dashboard and Settings open in your browser with `NSWorkspace`, without the token. The dashboard keeps its own
sign-in in the browser (`board dashboard` signs it in once).

## Build

Needs Xcode or the Swift toolchain (Swift 5.9 or later).

```bash
cd integrations/macos-menubar
bash build.sh        # swift build -c release, then assembles build/AgentComms.app
open build/AgentComms.app
```

`build.sh` writes only inside this folder: `build/AgentComms.app` (bundle id `dev.agentcomms.menubar`,
`LSUIElement`, minimum macOS 13), checked with `plutil -lint` and signed ad hoc. Other commands:

```bash
swift build          # debug build
swift test           # summary decoding, the token-file check, URL building, uv lookup, menu labels
swift run            # runs outside a bundle: works, but Launch at Login is unavailable
```

## Install

```bash
bash integrations/macos-menubar/install.sh   # copies build/AgentComms.app to ~/Applications
```

To start it when you log in, choose **Launch at Login** in its menu (this uses `SMAppService`, and works only for
the app bundle), or add `~/Applications/AgentComms.app` under System Settings > General > Login Items > Open at
Login. To remove it, quit it and delete `~/Applications/AgentComms.app`.

## Configuration

Settings live in the app's defaults (`dev.agentcomms.menubar`):

| Key | Default | Meaning |
|---|---|---|
| `port` | `8787` | the board's port on 127.0.0.1 |
| `repoPath` | `~/agent-comms` | the agent-comms checkout, used by Start Board Server |
| `tokenFile` | `~/.config/agent-comms/human.token` | the human token file |

```bash
defaults write dev.agentcomms.menubar port -int 8788
defaults write dev.agentcomms.menubar repoPath ~/code/agent-comms
```

`AGENT_COMMS_TOKEN_FILE` overrides the token path, as it does for the CLI. For a one-off run against a test board,
pass settings as launch arguments, which are not saved:

```bash
open -n --env AGENT_COMMS_HOME=/tmp/test-board --env AGENT_COMMS_TOKEN_FILE=/tmp/test-board/human.token \
  build/AgentComms.app --args -port 8799 -repoPath ~/agent-comms
```

## Start Board Server

**Start Board Server** runs `uv run --project <repoPath> board serve --port <port>` with an explicit argument list
and no shell. It is detached from the app and keeps running after you quit it. Its output is appended to
`<repoPath>/data/menubar-server.log` (mode 600). Apps opened from Finder or at login get a minimal `PATH`, so the
app looks for `uv` in `/opt/homebrew/bin`, `/usr/local/bin` and `~/.local/bin`, then searches a standard `PATH`.
The server gets the app's environment with that `PATH`, minus anything named like a token, secret, password or key.
To stop a server started this way, run `pkill -f "board serve"` or quit it in Activity Monitor.
