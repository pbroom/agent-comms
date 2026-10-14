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
