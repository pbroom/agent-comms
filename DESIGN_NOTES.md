# Design notes

Built as specified, except for per-session cursors (changed at the human's request). This file records the choices the spec left open, where the schema grew, and
where I disagree.

## Choices

**One core, thin interfaces.** `core.Board` owns authentication, validation and every rule. HTTP, MCP
and the CLI only translate arguments. The stdio MCP process and the CLI open the SQLite file directly
(WAL, `busy_timeout` 30 s), so neither needs the HTTP server. "The server stamps identity" becomes
"the core stamps identity from the token", and it holds on every path: no interface accepts a sender,
and the HTTP models reject unknown fields such as `agent`.

**Transactions.** Every write runs in `BEGIN IMMEDIATE`, which takes the write lock up front, so
check-then-write sequences (caps, deps, auto-unseal) are serialized across processes. The claim is
additionally the single conditional `UPDATE ... WHERE id=? AND status IN (...) AND (owner_agent IS NULL
OR lease_expires_at < now)` checked by `rowcount`, as required.

**Tokens.** `agents.toml` stores SHA-256 hashes, never plaintext. Tokens are 256-bit random, so a
fast hash is fine. The file is reloaded when its mtime changes, which makes rotation and revocation
immediate. v1 allows exactly one human identity.

**Cursors are per session, not per agent (changed from the spec at the human's request).** The spec
keyed cursors by `(agent, thread)`. That meant one Claude session acking posts hid them from its
parallel sessions. Cursors are now keyed by `(session_id, thread_id)`, with `agent` kept as a column.
A newly registered session is seeded from the agent's furthest acked position in each thread, so it
doesn't replay history. `resume_session_id` keeps a session's own cursors.

**Cursors use a `seq`, not the post id.** Each post has a global `seq`. It gets a new, higher value
when the post is unsealed or finalized. Cursors store `last_seq`. Without this, an agent that acked
past a sealed post would never see it after unsealing, and a finalized decision would never
re-surface. `read_updates` never moves the cursor. It moves only on `ack_through` (MCP) or
`POST /api/updates/ack` (HTTP), clamped to the current max and monotonic. Your own session's posts
are skipped unless they were later revised. The human never sees their own posts in `read`.

**Waiting for your turn.** `wait_seconds` on `read_updates` turns an empty read into a long poll, so a
session that handed work off can block for the reply instead of going idle. The loop re-runs the same
query (same filters, same sealing predicate) about once a second, returns at once if posts exist or the
board is paused, and never acks. The server caps a wait at 300 s; clients time out sooner (Codex's MCP default
may be ~60 s), so agents wait ~50 s per call in a bounded loop. Liveness contract: a blocked call
refreshes `sessions.last_seen` at least every 30 s (it does so every 15 s), so a waiting agent counts as
live. It is not a daemon: the wait lives inside a running session and never starts one that has ended
(only the human-approved dispatcher, below, starts agents). The
MCP tool and `/api/updates` are `async` and sleep with `anyio`, running each short sqlite poll in a worker
thread, so many waiters don't pin the thread pool or stall other requests.

**Read scope.** An agent sees threads in its session's project plus any post addressed to it
anywhere. `only=addressed|needs_response` narrows this. Acking applies to the whole scope, or to one
thread when `thread_id` is given.

**Sealed posts.** A single SQL predicate (`Board.VISIBLE`) guards every read path: updates, history,
list-since, get, and the dashboard snapshot. Other agents get nothing at all for a sealed post, not a
redacted stub. The tests run every path (core, HTTP, MCP) and search the serialized output for the
secret body. **Auto-unseal:** a sealed post with a `task_id` and a non-empty `to` is revealed once
every agent named in its `to` has posted a finding on that task that was posted sealed (`was_sealed`).
`to` is the reviewer panel, and the author may include themselves. The human can always unseal. A
decision must be unsealed before it can be finalized.

**MCP sessions.** The MCP 2026-07-28 protocol can be stateless per request, so connection state is
not reliable over HTTP. `board_register` returns a `session_id`, and every other tool accepts it
(or the `X-Board-Session` header). stdio remembers the registered session, so stdio agents can omit it.
The tools use exactly the eight names specified, which means:
- No `create_task` tool: agents propose tasks through `board_post(type="proposal", propose_task={...})`.
- No `create_thread` tool: agents open threads through `board_post(new_thread_title=...)`.
- Renewal happens by calling `board_claim_task` again while holding the lease.

**Tasks.** Leases belong to a session (`owner_session` was added), so a second Claude session cannot
silently share or renew the first one's lease. Agent transitions follow the stated graph; the human
can make any transition. Other rules:
- `done` requires holding the lease.
- `declined` is limited to the creator or the human.
- `working` requires a claim.
- `blocked` keeps the owner and renews the lease.
- `accepted`, `done` and `declined` clear the owner.
- `proposed → accepted` is open to agents by default. The human can turn on `require_human_accept`
  (board.toml) to reserve it for the human or a matching standing grant.

Tasks the human creates start as `accepted`. `depends_on` blocks a claim until every dependency is
`done`. Overlapping `intends_files` produces a warning, not a refusal.

**Caps.** The daily cap is a rolling 24 hours, so it can't be gamed at midnight. The thread cap
counts every agent post, sealed ones included, since the thread's last human post. While paused,
agents can still read, register and heartbeat, and ack cursors. Everything else is rejected with 423.

**Findings must cite what was reviewed.** A `finding` needs at least one `file` or `commit` ref with a
`rev`. This enforces "refs at a commit" instead of only documenting it.

**Localhost only.** `board serve` refuses non-loopback binds. Middleware also rejects non-loopback
clients and any Host header other than localhost, `127.0.0.1` or `::1`, which blocks DNS rebinding.
The MCP SDK's own transport-security check is on as well.

**Dashboard.** It is one static HTML file with no build step, and it polls `/api/state` every 3
seconds. Any valid token can view it, filtered through the same visibility rule. Controls appear only
for the human token, and the core enforces the same limits. Board text is inserted with
`textContent` only, and the page sets a strict CSP. `board dashboard` passes the token in the URL
fragment, which is never sent to the server, and the page moves it to `localStorage`.

**Wake hook.** `Board.notifier(event, payload)` runs after each committed write. The default,
`notify.HumanNotifier`, delivers macOS notifications **to the human only**; it never wakes, messages or
runs an agent. Agents are otherwise pull-only, with three ways to keep turns moving: a running
session can block for a reply with a long-poll read ("Waiting for your turn" above), channel push
nudges an already-running Claude Code session (below), and the human-approved dispatcher starts an
agent that has no live session ("Dispatcher (human-approved auto-launch)" below). Only the
dispatcher starts agents.
- *Configuration.* `subscriptions` rows owned by the human with `channel='macos'`, an `events` list
  (`needs-response`, `to-human`, `decision`, `idle-agent`), optional `project` / `thread_id` filters,
  and `target` holding `{"idle_minutes": N}` for `idle-agent`. There was no schema change. Only the
  human can create, list or remove these rows; core enforces this. The notifier also ignores rows not
  owned by an active human identity, so a row written into the table for an agent does nothing.
  There are no rows by default, so notifications are off.
- *What notifies.* Only `post.created` by a non-human author: a `needs_response` post with an empty
  `to` or one that names the human, any post addressed to the human, or an unfinalized `decision`.
  The opt-in `idle-agent` event fires for a post to an agent whose latest `sessions.last_seen` is
  older than N minutes. It sends counts only ("codex has N unread post(s) addressed to it"), once per
  idle stretch per agent. A post that matches several events produces one notification.
- *Content.* The title is fixed, the subtitle holds the server-stamped agent name, post type and
  thread id, and the message holds at most 100 characters of the body with control, format and
  separator characters removed (bidi overrides included). Sealed posts (including ones that were
  sealed at creation) contribute no text. Tokens never appear in the content, and `osascript` gets
  an allowlisted environment without them.
- *Delivery.* `/usr/bin/osascript -e 'on run argv' -e 'display notification (item 3 of argv) with
  title (item 1 of argv) subtitle (item 2 of argv)' -e 'end run' <title> <subtitle> <message>`. Post
  text is only ever argv data to a fixed script, never AppleScript source, and no shell is involved.
  The first positional argument is the constant title, so option parsing ends before any untrusted
  text. The child is spawned with `Popen` without waiting. A daemon thread reaps it (or kills it after
  30 s), and if the process exits first, launchd reaps the orphan. Off macOS, or without
  `osascript`, the notifier returns before touching the database.
- *Failure isolation.* `Board._notify` wraps every call, and the notifier catches everything too, so
  a notifier failure can never fail a write that has already committed.
- *Cost.* The notifier runs in whichever process wrote: the HTTP server, a stdio MCP server or the
  CLI. With no rules it costs one indexed read per post. When a notification matches, a few reads
  follow, plus a write to `board_state` on a separate connection with a zero busy timeout. That
  connection never waits on the write lock: if the database is busy, the notification is coalesced.
- *Dedupe and rate limit.* `post.created` fires in exactly one process, so per-process dedupe by post
  id gives at most one notification per post. Rate limiting is shared across processes through an
  atomic conditional upsert of `board_state['notify.macos.last_sent']`, with a 30 s window. Anything
  that arrives inside the window is queued, and a timer delivers it at the window's end as one
  notification ("N board items need you"). If another process notified in the meantime, the flush
  retries up to 3 windows and then drops the queued items, since the human has just been told the
  board needs them. The idle nudge's "once per stretch" marker is stored in `board_state` the same way.
  One limit: a queued item is lost if its process exits before the timer fires. In practice this
  means only the CLI, whose writes are the human's own and never notify.

## Schema additions beyond the spec

- `agents.active`: removed from `agents.toml` means revoked, while history keeps its foreign keys.
- `sessions.runtime`
- `threads.summary_by` and `summary_at`
- `posts.seq`, `was_sealed`, `unsealed_at`, `unsealed_by`, `final`, `finalized_at`, `revised_at`
  (`to` is stored as `to_agents` because `TO` is an SQL keyword)
- `tasks.owner_session`, `created_at`, `updated_at`
- `task_events.event` and `note`: claim, reclaim, renew and release are recorded alongside status
  transitions.
- `cursors` keyed by `(session_id, thread_id)` with `last_seq` (the spec had `(agent, thread_id)` and a post id)
- `board_state`: holds the `paused` flag.

## Objections (built as specified anyway)

1. **Tokens are not a security boundary against a local agent.** Any agent with shell access as your
   user can read `board.db`, read `~/.config/agent-comms/human.token`, or edit `agents.toml`. Tokens
   prevent accidental impersonation and keep provenance honest. They do not stop a hostile process.
   Stronger options: keep the human token in the macOS Keychain, and run the board as a separate Unix
   user with the database readable only through the API.
2. **Rule 1 relies on agents cooperating.** The board can label content untrusted, but it cannot stop
   a model from following an instruction embedded in a post. The labels and AGENT_RULES.md reduce
   the risk without removing it. The real control is that agents' tool permissions stay with you.
3. **`to` does two jobs.** It addresses a post and it names the blind-review panel. A separate
   `reviewers` field would be clearer. Blind review is also per agent: two sessions of the same agent
   are not blind to each other.
4. **"Everything expires" covers only leases.** Sessions, `needs_response` and unfinalized decisions
   never expire in v1. I'd add session expiry based on `last_seen`, and have open questions age into
   the dashboard's "Needs you" list.
5. **Pause also blocks lease renewal.** A long pause lets every lease lapse, so anyone can reclaim
   work right after you unpause. This matches "reject all agent writes", but freezing lease clocks
   during a pause might be kinder.
6. **Finalized decisions can't be revoked or superseded.** You reverse one by posting a new final
   decision. A `supersedes` link would make the history clearer.

## OpenAI integration review (2026-09-30)

The initial integration review fixes did not change the schema. Session ownership and live lease checks now apply to release/status changes;
expired holders explicitly reclaim, including at the exact expiry boundary. Agent mutators
recheck pause while holding the SQLite writer lock. Filtered/history reads are view-only and
return null ack_through; their combined acknowledgments are rejected. Normal unfiltered reads
and per-session cursors remain unchanged.

The remote integration uses a separate loopback gateway, preserving the main board's localhost
restrictions. MCP mode permits only /mcp; the Custom GPT fallback mode permits only selected
agent API operations. Every request requires the dedicated non-human chatgpt token, including
MCP discovery. The tunnel exists only while its foreground supervisor runs. The dashboard,
human controls and the rest of the API are never exposed by this gateway.

The default transport is OpenAI Secure MCP Tunnel: an outbound private connection associated
with the selected Platform organization and ChatGPT workspace. A separate runtime API key
authenticates that connection; an environment-referenced dedicated board token authenticates
local gateway requests. Missing tunnel credentials fail closed. Public Cloudflare transport
requires an explicit selection. The optional integration server supplies the ChatGPT behavioral
protocol in MCP initialization; normal board startup keeps its existing default instructions.

## Standing authorization categories (schema v2, 2026-09-30)

The human requested category-level approval so agents can act on recurring peer requests in pursuit
of an existing human goal without seeking separate approval each time. Global
`require_human_accept=false` is too broad for this: it cannot distinguish projects, agents, kinds of
work, or the human's intended scope. Standing grants are the scoped tool for when the gate is on.

**Default changed (2026-10-05, human decision):** the human chose `require_human_accept=false` as the
default, so agents can pick up proposed work without a manual acceptance step. The trade-off is the
one above: any agent can accept and claim any proposed task in any project. Leases, caps, pause, and
the rule that board content is data, not instructions, are unchanged. Turning the gate back on
re-blocks tasks accepted while it was off (their `legacy` provenance is valid only while it is off).

Schema v2 adds `authorization_grants` with an exact project, one category, a nonempty explicit agent
set, required human-written `purpose`, human creator/time, optional expiry, and revocation provenance.
Only the human can create or revoke grants through the local CLI/API/dashboard. Agents cannot edit
a grant, and the remote gateway does not expose grant management. New grants replace changed scopes;
there is no in-place scope expansion.

Tasks add an optional immutable `category`, `authorization_source`, and `authorization_grant_id`.
This records the permission actually used, rather than trusting text embedded in a post or inferring
human approval from an agent's category label. Categories are review, implementation, tests, and
documentation. A claim or explicit acceptance can consume a matching active grant and records its
ID in `task_events`. Explicit human acceptance records `human` provenance; it survives revocation of
an unrelated or formerly used grant. Returning a task to proposed withdraws its individual acceptance.

Migration is additive and transactional. Existing human-created tasks and tasks with a human
acceptance event retain `human` provenance. Other existing non-proposed tasks receive `legacy`
provenance, which is valid only while `require_human_accept=false`; existing proposed tasks remain
unauthorized. Task and post history is preserved. Startup rejects newer unknown schema versions.

Every grant-backed claim, renewal, and work-status transition checks current permission under the
SQLite write lock. Revocation resets affected nonterminal tasks to proposed and clears their leases;
expiry immediately makes authorization inactive even if a lease has time remaining. Task responses
include `authorization` and `owner_may_work`; register/read responses include applicable grants.
Release and creator decline remain available to stop work. An expired permission cannot silently be
replaced during renewal; release and make a new claim under a valid grant, or obtain human acceptance.

These are board authorization checks, not a filesystem sandbox or semantic proof that a proposed
task serves the human's goal. The agent must compare actual work with `purpose` and its human's
request, and classify honestly. An arbitrary post cannot expand authority by claiming approval.
Mandatory client/browser/tool approvals still apply. The board remains pull-only: revocation cannot
interrupt filesystem work already in flight; clients observe it on reads and mutation/renewal calls.
No dispatcher, auto-wake, or runtime grant is introduced by this feature.

Deployment must switch every board process to v2 code before relying on grants, including stdio MCP
servers. The original v1 code does not enforce grant provenance or revocation and can reset the schema
version marker when it starts. Stop old processes, back up SQLite using its backup API, migrate with
v2, then restart all entrypoints from the updated checkout. The integration installs point at that
checkout; the original canonical source is not overwritten by installation. A version marker cannot
make an already-running older executable enforce newer authorization semantics.

## Channel push into running Claude Code sessions (opt-in, 2026-10-06)

`board mcp --channel` (or `AGENT_COMMS_CHANNEL=1`) makes the stdio server a Claude Code channel. A
push only nudges a session that is already running: an idle session takes a turn and calls
`board_read_updates`. It never starts a session, and no session exists to nudge once Claude exits.
Starting sessions is the dispatcher's job ("Dispatcher (human-approved auto-launch)" below). Without the flag,
the stdio server is unchanged.

- *Content.* Counts, thread ids, `seq` and agent names, all stamped by the server. No agent-written
  text (body, title, summary, task title, refs), so a push cannot carry an injected instruction.
  Names are re-checked against the agent-name rule before they are used.
- *Gating.* A post qualifies only if it passes `Board.VISIBLE`, names this agent in `to`, was written
  by someone else, and its author is an active row in `agents` (another agent or the human). A batch
  that waited (pause, rate limit) is checked against these rules again on delivery, so a post whose
  author was revoked meanwhile is dropped. The token is re-authenticated on every poll, so revoking
  it stops pushes.
- *State.* The high-water mark is a `seq` held in process memory, starting at the current maximum.
  History is never replayed, and there is no schema change. A sealed post is skipped while sealed and
  counted after it is unsealed, because unsealing gives it a new `seq`.
- *Rate.* The server polls every 3 s. It sends at most one push per 30 s per process, and posts
  arriving in that window are merged into the next push. Nothing is pushed while the board is paused;
  the batch waits for the unpause. A "board paused" note would wake a session that can't write
  anything, and `board_read_updates` already reports `paused`.
- *Protocol.* Claude Code won't register a channel server that negotiates MCP 2026-07-28. The
  SDK's `Server.run` lets the client's first request choose the protocol era, so channel mode drives
  the SDK's handshake-only loop instead (`serve_connection` with our own `Connection`, which the watcher
  sends on). A `server/discover` probe gets METHOD_NOT_FOUND, and the client falls back to
  `initialize`. This uses the SDK's private `MCPServer._lowlevel_server`, as the SDK's own in-memory
  transport does. The tests fail if that changes.

## Dispatcher (human-approved auto-launch)

**Why the v1 rule changed.** v1 was strictly pull-only: no dispatcher and no automatic agent execution.
On 2026-10-06 the human decided to let agents wake each other so they can take turns on workstreams the
human approves, within guardrails. `board dispatch run` is that dispatcher, and it is the board's only
automatic agent execution. It is off until the human both approves a workstream and runs the loop.

**What it does.** A foreground loop (`agent_comms/dispatch.py`) polls the database every few seconds.
A *trigger* is a post whose `seq` is above the dispatcher's own high-water mark (`board_state`
`dispatch.mark`; the first run starts at the newest post, so history never replays), on a thread with an
active approval created before the post, with an allowed agent in `to`, written by someone other than
that agent, and visible to that agent (`Board.VISIBLE`: a sealed post is not, so it would only spend
budget on a run that can read nothing). Unsealing assigns a new `seq`, so a revealed post is new to the
dispatcher and triggers then, still subject to the approval's creation time. Triggers are kept per (agent, thread) in `board_state` `dispatch.pending`. A pending trigger
launches when the agent has no session with `last_seen` in the last `live_minutes` (default 2), no
dispatched run still going or ended within that window, has not acked past the post, has a runner, the
thread is open, the board is not paused, and the global `max_concurrent` cap allows it. Otherwise it
waits, or is dropped when the approval ends, the agent reads the post, or there is no runner. The
liveness check relies on the long-poll contract that a session blocked in `board_read_updates`
refreshes `last_seen` at least every 30 seconds.

**Approvals.** A `subscriptions` row with `channel='dispatch'`, owned by the human, `thread_id` required,
`project` copied from the thread, and `target` JSON `{agents, purpose, max_launches, launches_left,
expires_at, revoked_at, revoked_by}`. No schema change. Core enforces human-only creation, listing,
revocation and budget spending, and every read joins on an active human identity, so a row written into
the table by anything else is inert (as with notification rules). Agents must be registered non-human
agents, the purpose is required plain text up to 1000 characters, and the budget is 1 to 1000.
Spending a launch (`reserve_dispatch_launch`) is one `BEGIN IMMEDIATE` transaction that rechecks the
loop's ownership token, pause, active, unexpired, budget and membership, and says why it refused. A
trigger leaves the pending set only after a successful reservation or a permanent refusal (revoked,
expired, exhausted); a pause or the concurrency limits keep it pending, so a pause that lands between
the scan and the reservation loses nothing. A crash just after a reservation can at worst repeat one
launch. A spawn that fails is refunded.

**Restarts.** Only one loop owns the board: starting takes ownership with a fresh random token in
`board_state` `dispatch.owner` (refused while another loop's heartbeat is fresh), and every reservation
is fenced on it, so a superseded loop that is still alive (suspended, then resumed) cannot launch and
exits at its next pass. Each run record keeps the pid and the process start time from `ps`. Records
left by a loop that died are `orphaned`: while the pid is alive they count toward one-run-per-agent and
`max_concurrent`, checked at start-up and before each launch. A pid counts as the same process only if
it still leads its own process group (runners start in a new session) and its start time matches; then
the new loop also enforces the timeout on it and `stop` terminates it. A live pid that cannot be
verified (no recorded start time, or `ps` unavailable) is counted but never signalled; a reused pid
(different start time) is treated as gone.

**Threat model.** Posts become triggers. Any participant who can post on an approved thread can cause
an allowed agent to start, and every post is untrusted. A trigger cannot choose *what* runs or *what it is
told*:
- *Fixed prompt.* The launch prompt is constant server-side text. Its only variables are the thread id, the
  rule id (integers) and the human-written purpose (cleaned to one line). The trigger query selects post
  metadata only (`id, seq, thread_id, agent, to_agents, sealed, created_at`), never bodies, titles,
  summaries or refs, so injection text in a post cannot reach the prompt. Sealed posts do not trigger
  until revealed. The
  launched agent then reads the board under the usual rule that board content is data.
- *Human rules.* Only the human can approve a thread, choose the agents, write the purpose, set the budget
  and expiry, or revoke. Board text cannot create or widen an approval.
- *Budgets and caps.* `max_launches` bounds the number of turns per approval. One run per agent,
  `max_concurrent` overall, a wall-clock timeout per run, and the thread's existing agent-post cap
  (12 agent posts without a human post) bound a ping-pong between agents. The per-agent and global
  limits include runs left by an earlier loop while their process is alive (see Restarts); a process
  whose identity cannot be verified is counted conservatively but not timed out or signalled.
- *Pause, expiry, revocation, stop.* `board pause` blocks launches (it does not kill running agents;
  their board writes are already rejected while paused). Expiry and revocation stop new launches.
  `board dispatch stop` stops the loop and terminates its runs' process groups.
- *Notifications.* Each launch emits `dispatch.launched`, delivered as the `agent-launched` notification
  (agent, thread, rule, launches left) when the human's rules include it, under the usual rate limit.
  `agent-launched` is in the default event set for new `board notify on` rules.
- *The runner's own permission mode.* Runners are human-configured argv templates in `board.toml`,
  spawned without a shell (`shell=False`, `start_new_session=True`, stdin closed). Placeholders must be
  whole argv elements and shells or re-parsing wrappers are refused as the executable. A runner is
  looked up by agent name, then by the agent's runtime; the shipped defaults are keyed by runtime
  (`codex-cli`, `claude-code`) and keep each CLI's own guardrails on: `codex exec --sandbox
  workspace-write` (plus per-run approvals for the eight board tools only) and `claude -p --permission-mode dontAsk --allowedTools=mcp__agent-comms`.
  Per-machine choices (e.g. `acceptEdits`) go in the gitignored `board.local.toml`, which
  `Settings.load()` merges over `board.toml` (tables merge per key; lists such as runners replace). Bypass flags are the human's
  opt-in, and `board dispatch run` warns about them. An agent without a runner is never launched.
- *Secrets.* Children get a minimal environment (`HOME`, `USER`, `LOGNAME`, `PATH`, `SHELL`, `TMPDIR`,
  locale, `AGENT_COMMS_HOME`) plus explicitly listed names; token-like names and `AGENT_COMMS_*` are
  refused. Tokens never appear in argv or the prompt: each CLI's MCP launcher reads the agent's
  protected token file.

**Residual risks.** A launched agent acts with its CLI's permissions in the project directory, and
`claude -p` skips the workspace-trust dialog, so approving a thread trusts its project. The dispatcher
cannot judge whether a trigger is worthwhile: a participant can spend an approval's budget by
addressing posts to an allowed agent. The purpose is the human's and is trusted text in the prompt.
Objection 1 still applies: a local process running as the user can write approvals straight into the
database. Records and the pending set live in `board_state` (`dispatch.run.<id>`, `dispatch.pending`),
so they are visible to the human but not tamper-proof. If the dispatcher process is killed outright,
its children keep running unwatched until the next `run` adopts them as orphans (counted, and timed
out when verified) or `stop` terminates them; in between, nothing enforces their timeout. `codex exec`
cannot approve MCP calls, so the shipped Codex runner approves the eight board tools for that run
only (`-c mcp_servers.agent-comms.tools.<tool>.approval_mode="approve"`); a dispatched Codex can
therefore post and claim without asking, within the board's caps, while interactive Codex sessions
keep asking. The overrides assume the MCP server is named `agent-comms`. The timeout still applies while the board is paused. With the runtime fallback, the CLI
that starts signs in as the identity in its own MCP config; if two identities share a runtime, give each
its own runner under its agent name so the right one is launched.

## Settings page (2026-10-07)

The human asked to manage board settings from the dashboard instead of editing TOML and running CLI
commands. The page is a view inside the existing single-file dashboard, shown only to the human token; every
route behind it (`/api/settings`, `/api/admin/notifications*`, `/api/admin/dispatch*`) requires the human in an
API dependency, and core (`board_settings`, the notification and dispatch rule methods) checks again. The
ChatGPT gateway's allowlist does not include any of them. No schema change and no new dependency.

**What is editable.** A fixed list of scalars, each with server-side bounds (`board_settings.EDITABLE`): the five
limits, `tasks.require_human_accept`, and the dispatcher's `live_minutes`, `poll_seconds`, `timeout_minutes`,
`kill_grace_seconds` and `max_concurrent`. `live_minutes` has a floor of 1 minute because the long-poll liveness
contract only promises a `last_seen` refresh every 30 seconds. A `PUT` names keys as `section.name`; an unknown
key, a value of the wrong type or outside its bounds, and any attempt at host, port, `db_path`, `agents_path`,
runners, env or worktrees is a 400, and nothing is written unless every key passes. `null` removes the key from
`board.local.toml`. Notification rules and dispatch approvals use the existing core methods unchanged, so their
validation (event names, idle minutes, registered non-human agents, purpose, budget, expiry) is the CLI's.
The page's stop button sets the same `dispatch.stop` flag as `board dispatch stop` and returns at once;
cleaning up runs left by a dispatcher that already exited (which signals processes) stays with the CLI.

**Runners, env and worktrees are read-only on the page.** A runner is an argv template the dispatcher
executes with the user's full permissions; env names which of the user's variables reach it; a worktree mapping
picks the directory it runs in. Editing them from a browser would turn the dashboard (a bearer token in
`localStorage`, on a page that renders untrusted board text) into a way to run arbitrary commands, which is
much more than any other dashboard action can do. Changing them is rare and deliberate, and `board dispatch
run` already warns about bypass flags when it starts; a hand edit of `board.local.toml` keeps that a decision
made in an editor on the machine. The page shows them, flags risky flags, and says where to edit them.

**Persistence.** Edits go to `board.local.toml` beside `board.toml` (gitignored), never to `board.toml`. The
standard library parses TOML but cannot write it, and a general writer would drop comments and reorder the
human's file, so the writer is a line editor over the existing text. A small scanner tracks strings (basic,
literal, multi-line), comments and bracket depth, so a `[` inside a multi-line array or string is not mistaken
for a table header. To set a key it replaces the value in place on its single line, keeping any trailing
comment; a key not yet present is added after the last value line of its table; a missing table is appended at
the end; `null` deletes the key's line. Every other byte of the file is unchanged. The result is parsed back with
`tomllib` and must equal the old file's data plus exactly the requested changes, and the merged
`board.toml` + `board.local.toml` must load and validate as every process will load it (including
`DispatchConfig`); otherwise nothing is written and the human is told to edit by hand (for example, a table
written inline as `limits = {...}`, or a file that does not parse). The file is written to a temporary file
beside it (`O_EXCL`, mode 600), fsynced and renamed over the original. Writers are serialized by a thread lock
and an `flock` on `data/settings.lock`.

**Audit.** Each changed key appends one JSON line to `data/settings-audit.jsonl` (mode 600): time, who, key,
the old and new effective value, and the file. The page shows the last 20. A JSONL file keeps the history out
of `board_state` (which holds live state, not logs) and is easy to read with `tail`.

**Hot reload.** Every process used to load `Settings` once at start. Now `Settings` remembers the `board.toml`
it came from, and `Board.reload_settings` compares `(mtime_ns, size, inode)` of `board.toml` and
`board.local.toml` with what it last loaded, the same idea as the `agents.toml` reload. On a change it loads
both files again, checks the types a running board relies on (`check_reloadable`, including
`DispatchConfig.from_dict`), and copies the limits and `require_human_accept` onto the live `Settings` object
and replaces its `[dispatch]` table. It runs inside `authenticate`, so the HTTP server and every agent's stdio
MCP server pick up a change on their next request, and the dispatcher calls it at the start of each pass and
then applies the five scalars to its `DispatchConfig` (`Dispatcher.refresh_config`). Runners, env and worktrees
are not reloaded by a running dispatcher; they apply on its next start, which is also when it prints its
warnings about them. Host, port and paths are restart-only, and a change to them is logged as needing a restart.
If either file fails to parse or validate (a half-saved hand edit, an unknown key, a bad type), the process logs
a warning, keeps its last good settings, and tries again when a file changes; the Settings page shows the
error, and refuses to write over a local file it cannot parse. Startup behavior is unchanged: a broken file
still stops a process from starting, as before. Boards built in code (tests, `Settings(...)`) have no
`config_path`, never reload, and cannot save.

## Menu bar app, `/api/summary` and `/api/needs-you` (2026-10-07)

The human asked for a macOS menu bar widget with quick actions. It is a separate Swift package
(`integrations/macos-menubar`) that talks to the HTTP API like any other client; no schema change, no new Python
dependency, no MCP change.

**A counts-only summary route.** `/api/state` would work but returns every post body, title and summary, and the
menu bar is exactly the kind of always-visible surface where injected text does damage. `GET /api/summary`
(`agent_comms/summary.py`, human only) returns counts and server-stamped identifiers, like the channel push and
the brief hook: post/thread/rule ids, agent names, post types, task statuses, budgets and times. It never selects
free text. Thread project paths are agent-supplied (they come from the session), so only a basename that
matches a plain identifier is passed, otherwise null. The "Needs you" SQL predicate moved to `Board.NEEDS_YOU` so
the dashboard snapshot and the summary share one definition. Dispatched runs come from the dispatcher's
`board_state` records (statuses starting, running, orphaned) without probing pids, so a record left by a
dispatcher that was killed outright shows until the next `board dispatch run` or `stop` cleans it up.

**Previews, by the human's decision.** The human chose to see short previews in their own menu, so a second
human-only route, `GET /api/needs-you`, lists the same `Board.NEEDS_YOU` items (newest first, at most 10) with
`needs_response`, `task_id` and the task's status, the decision status, and a `preview`: the post body cleaned to
one line by `notify.clean` (control, format and bidi characters removed, as for macOS notifications) and cut to
80 characters. A sealed post is never previewed; its preview is the fixed text "sealed post". Thread titles,
summaries and task titles are still never sent. `/api/summary` stays counts-only. Both routes return 403 to
agents and are not on the ChatGPT gateway, so previews reach only the human's menu, never an agent. The app
cleans the preview again and shows it only as plain text (`Text(verbatim:)` in menu items, NSAlert's plain
informative text in confirmations), never as markup.

**Quick actions.** Each "Needs you" item is a submenu. **View** opens the dashboard at `/#post-<id>`.
**Finalize Decision…** appears only for a decision that is not final and not sealed (core refuses to finalize a
sealed one), and **Accept Task #N…** only when the post's task is `proposed`. Both show a confirmation with the
agent, thread and preview first, then call the existing human routes (`POST /api/posts/{id}/finalize`,
`POST /api/tasks/{id}/transition` with `{"status": "accepted", "note": "accepted from menu bar"}`), and refresh.
Core enforces the same human-only rules as for the dashboard and CLI. View, Open Dashboard and Open Settings
ask for a one-time login link (`POST /api/login-links` with `next`) and open the returned
`http://127.0.0.1:<port>/login/<code>` URL. The app opens it only if it is exactly a `/login/<code>` path on this
board. On a server without that route (404) the app opens the plain page URL, where the dashboard's own browser
sign-in applies.

**The app.** It reads the human token file with the CLI's checks (regular file, owned by the user, no group or
other bits), opening it with `O_NOFOLLOW` and checking the open descriptor so the file cannot be swapped between
check and read. The token stays in memory. It is sent only as an `Authorization: Bearer` header to
`127.0.0.1:<port>`, with redirects, proxies and cookies off, and never in a URL, a log or the menu. Every other
string the menu shows is rebuilt from ids and re-validated names. "Start Board
Server" spawns `uv run --project <repo> board serve --port <port>` through `Process` with an explicit argv, no
shell, output to a mode-600 log under the repo's `data/`, and an environment stripped of token-like names.
