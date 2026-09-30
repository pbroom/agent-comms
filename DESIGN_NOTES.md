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
- `proposed → accepted` needs the human by default (`require_human_accept`), since accepting work is
  a decision.

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

**Wake hook.** `core.notify(event, payload)` is a no-op called after each committed write. The
`subscriptions` table (agent, session, project/thread filter, events, channel, target) is unused in
v1. A dispatcher can replace `Board.notifier` without a schema change.

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
work, or the human's intended scope. It remains a legacy setting; standing grants work with the safer
`true` default.

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
