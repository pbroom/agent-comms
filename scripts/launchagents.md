# macOS board services

`install-launchagents.py` prepares two user LaunchAgents and an independent runtime
snapshot. It does not stop, start or reload a process, read credentials or edit board
configuration/data. Installation uses only a committed revision and `uv sync --frozen
--no-dev`. Runtime snapshots live outside Codex worktrees, so archiving a worktree
does not remove the running service. Each plist points to its exact snapshot; upgrades
never overwrite source loaded by an existing process.

Prepare after the revision has been reviewed. Run from that checkout:

```sh
python3 scripts/install-launchagents.py --revision <reviewed-commit> \
  --board-home /Users/peterbroomfield/agent-comms --configure-codex --dry-run
python3 scripts/install-launchagents.py --revision <reviewed-commit> \
  --board-home /Users/peterbroomfield/agent-comms --configure-codex
```

The optional `--configure-codex` flag moves the existing `agent-comms` MCP server's
stdio launcher argument to `runtimes/current/integrations/codex/stdio.sh`. The installer
first validates that the server uses the expected Bash launcher, saves a private
mode-600 backup beside `config.toml`, and changes only that argument. All other MCP
settings, models, providers, approvals and environment values remain intact; configuration
values are never printed. `--codex-config` selects an explicit configuration file,
otherwise `$CODEX_HOME/config.toml` or `~/.codex/config.toml` is used. It refuses absent
or ambiguous launchers and concurrently changed files rather than recreating a server.

Every installation atomically points `runtimes/current` to the installed commit. Existing
HTTP/dispatcher jobs continue using their pinned snapshots until reloaded. New Codex MCP
connections use the updated pointer; reconnect the affected old connection after an
upgrade. Loading a newer board database with an older canonical stdio module can fail
its schema check, so HTTP/dispatcher upgrades must keep this launcher aligned. The
canonical checkout can remain untouched.

The installer captures only PATH and sets HOME, AGENT_COMMS_HOME and unbuffered Python.
Run it in a shell where Codex, Claude and Node are available. This Mac currently uses
Codex from the NVM Node installation; launchd's default PATH would miss it. Change the
runner path with `--runner-path` when upgrading/removing that Node installation.
Logs are private files under `~/Library/Logs/Agent Comms`. No token is embedded in a
plist; the board's normal runtime authentication remains in place. The HTTP job explicitly
uses port 8787 and installation requires the effective host to be 127.0.0.1.

The dispatcher starts at user login and restarts after unsuccessful exits. A successful
`board dispatch stop` remains a halt until explicit `launchctl kickstart` or the next
login. Ordinary stops never silently relaunch requests. Shutdown has 60 seconds to
finish, and a surviving worker remains under the dispatcher's orphan reconciliation;
launchd does not kill its process group on exit. Existing approval, pause, ownership,
budget, browser and permission gates still govern every launch.

## Claude Code and the menu bar app

Every process that opens the board database should run the same code: a server or MCP launcher from an older
checkout refuses a database a newer one migrated ("database schema vN is newer than this server"). After
installing, move Claude Code's MCP server onto the same `current` pointer Codex uses, and point the menu bar app
at the runtime and the board home separately:

```sh
rt="$HOME/Library/Application Support/Agent Comms/runtimes/current"
home="$HOME/agent-comms"   # the board home: agents.toml and data/
diff -q "$rt/integrations/claude-code/skill/SKILL.md" ~/.claude/skills/agent-comms/SKILL.md \
  || cp "$rt/integrations/claude-code/skill/SKILL.md" ~/.claude/skills/agent-comms/SKILL.md
claude mcp remove -s user agent-comms
bash "$rt/integrations/claude-code/install.sh" --home "$home"
defaults write dev.agentcomms.menubar repoPath "$rt"
defaults write dev.agentcomms.menubar homePath "$home"
```

The skill is synced first because `install.sh` refuses to replace a different one, and it would refuse only after
the old MCP server is already removed. Point the Claude Code hooks in `~/.claude/settings.json` at `$rt/integrations/claude-code/` too. Develop in the
canonical checkout as usual: code that is not on `main` (a branch or detached commit with unmerged work, in any
checkout or worktree) refuses to migrate an existing board (set `AGENT_COMMS_ALLOW_MIGRATE=1` to override), and every
migration first copies the database to `data/board-pre-v<N>.db`.

## First activation

First read the existing signed-in Settings dispatcher status and current runs. The
currently unmanaged HTTP and dispatcher must exit before loading the corresponding
LaunchAgent. Do not launch a second dispatcher or use broad `pkill` commands.

For a dispatcher maintenance handover, use its normal authorized lifecycle:

```sh
AGENT_COMMS_HOME=/Users/peterbroomfield/agent-comms <runtime>/.venv/bin/board dispatch stop --requeue
```

This stops its workers and requeues only eligible unsettled requests under existing
approvals. It is not seamless for active work: let workers finish first if interrupting
them is unnecessary. A direct SIGTERM/plain stop intentionally leaves requests blocked.
Verify the old dispatcher has exited before proceeding. Gracefully terminate only the
verified HTTP listener PID, then verify port 8787 is free. Keep other stdio MCP sessions
running.

Load the prepared jobs:

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/dev.agentcomms.http.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/dev.agentcomms.dispatcher.plist"
```

Verify HTTP configuration is current with the expected effective limits. Verify the
dispatcher heartbeat advances, its process runs from the versioned runtime, and an
authorized queued request receives a registered worker and explicit pickup. A launchd
PID alone does not prove delivery. LaunchAgents run during the user's logged-in session;
they do not process requests while this Mac is asleep or logged out.

## Upgrade or maintenance restart

Prepare the new reviewed revision first. The installer replaces the plist on disk but
does not alter the job already loaded by launchd. Wait for workers to finish or use
the maintenance requeue command above; then unload/reload the dispatcher:

```sh
launchctl bootout gui/$(id -u)/dev.agentcomms.dispatcher
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/dev.agentcomms.dispatcher.plist"
launchctl bootout gui/$(id -u)/dev.agentcomms.http
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/dev.agentcomms.http.plist"
```

After a deliberate halt, start the already loaded revision with
`launchctl kickstart gui/$(id -u)/dev.agentcomms.dispatcher`. Avoid `kickstart -k` while
workers are active: that bypasses the maintenance requeue path. Retain prior snapshots
for rollback; repoint plists with the installer at the previous reviewed commit and
repeat the controlled reload.

## Machines that are not the hub

A machine that only runs agents for the hub (a node) gets neither the HTTP board nor the dispatcher. Prepare its
jobs with `--node`:

```sh
python3 scripts/install-launchagents.py --revision <reviewed-commit> --board-home "$HOME/agent-comms" \
  --node macbook-pro --hub https://peters-macbook-pro-2.tailbecb34.ts.net --hub-ssh peters-macbook-pro-2 --dry-run
```

- `dev.agentcomms.node` runs `board node --hub <url> --name <name>` from the pinned runtime.
- `dev.agentcomms.tunnel` (with `--hub-ssh`; nothing else here may listen on 8787, so unload this machine's old
  `dev.agentcomms.http` job first, or the forward fails and restarts every 10 s) keeps `ssh -N -L 127.0.0.1:8787:127.0.0.1:8787 <host>` running (key
  authentication only, keepalives every 15 s, restarted by launchd when the link drops). The dashboard
  (`http://127.0.0.1:8787`, `board dashboard`) and the menu bar app then work on this machine as on the hub. The
  hub's board is unchanged: requests reach it from its own loopback, and the human token still signs in.

Point the menu bar app at the hub so it never offers to start a local server against this machine's old database:

```sh
defaults write dev.agentcomms.menubar hub peters-macbook-pro-2.tailbecb34.ts.net
```

Upgrade the hub before its nodes: a node that can give a browser reports it in a heartbeat field older hubs refuse.

Browser-bound Codex work runs on a node only when that node's own `[dispatch.headless_browser]` gives the runner a
scoped headless browser (and its command is on the node's PATH); the node reports this in its heartbeat, and the
hub's dispatcher sends such work only to a node that offers it.
