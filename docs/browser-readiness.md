# Browser audit readiness

Browser permission and browser capability are separate. A probe is a short-lived,
self-reported observation, not a permission grant. Agent Comms never starts a browser,
changes a host policy, or treats a tool name as proof that it works.

Before a browser audit:

1. Register the actual executing session and worktree. Bind the original request to
   its exact target URL with `board_bind_browser_request`.
2. Check `board_browser_status`. A policy denial or host-permission failure blocks
   the origin in that project across sessions, identities and browser surfaces.
   Stop; do not retry with curl, another browser, raw CDP or a different runtime.
3. Begin an attempt with `board_browser_begin_probe` in the authorized execution
   context, then observe a successful HTTP response, render
   the intended target, and perform one harmless interaction with an observed result.
   For example, open and close an About dialog and verify its visible version.
   Preserve the target service; never stop or restart it as a connection workaround.
4. Record the complete result with `board_browser_probe` and its `attempt_id`.
   Delayed, replayed and superseded attempts are rejected. Supply context
   `{kind: desktop|headless, transport, connection_id}` and evidence
   `{http_status, rendered_url, rendered_identity, interaction, interaction_result}`.
   Use the exact target URL, not a nearby page or another process's observation.
5. Only then route/acknowledge started. The recipient must be live, in the exact
   project/worktree/session, and retain its actual browser connection. Evidence
   expires after five minutes; session liveness expires after 90 seconds.
   A dispatch run cannot report an interactive desktop connection as its own.

When a browser call fails, report `board_browser_failure` immediately. Distinguish
`policy_denied`, `host_permission`, `disconnected`, `unreachable`, `browser_missing`,
`render_failed` and `interaction_failed`. None is audit completion. Failure
invalidates readiness and blocks affected work; it does not expand permissions.

Only disconnected or expired contexts are eligible for bounded recovery. Reserve an
attempt with `board_browser_reconnect`, then use the adapter's documented reconnect
operation in that **same** context. At most two attempts are allowed without a new
complete successful probe. A permission rejection during recovery must be reported
as denial, not disguised as disconnection. An unreachable origin needs a separate
reachability diagnosis within existing permission; browser reconnect is not a fix.

After an explicit denial, the human must first change the actual host permission
through its supported controls. For Codex Browser, use Settings > Browser > Agent
permissions and a site exception for the exact scheme, host and port. A broad
implementation approval or a board decision does not alter that permission. A human
may then record the observed change through authenticated
`POST /api/browser/permission-change` with `project`, `target_url`, `evidence`, and
current `expected_epoch`. The endpoint only records the change; it grants no access.
A new complete probe is still required, and normal browser tool gates still apply.
Agents cannot clear this gate. Historical denial/probe events remain preserved.

The intended execution environment must produce the proof. An interactive desktop
probe does not prove a CLI worker can attach to that browser. Headless workers must
use a supported, authorized browser adapter available inside their own runtime, or
leave the request visibly blocked for a live eligible owner. New browser MCP tools
may themselves need narrowly scoped tool approval; never add blanket approvals or
change filesystem/network sandbox policy to make a probe pass.

## Current NEXUS recovery boundary

The October 8, 2026 S2 attempt received an explicit user-declined browser action at
12:50:11 p.m. Eastern for `http://127.0.0.1:5185`. Earlier desktop success is not a
permission reversal. The user subsequently confirmed the site permission change. A fresh built-in
Browser probe in the active desktop chat returned Document HTTP 200, rendered
NEXUS and opened/closed About showing LOCAL 1.43.17. This proves that desktop
context only; the three audits remain unfinished and CLI access is not implied.

The default local dispatcher uses `codex exec`, which does not inherit a desktop
chat's browser connection. Browser-bound requests must route to an already verified
live owner; do not launch the generic CLI runner and hope its tool inventory works.
The built-in Browser's supported surface and site-permission controls are documented
in [Codex Browser](https://learn.chatgpt.com/docs/browser?surface=app).


For the built-in Browser, select the documented `iab` surface explicitly. Numeric
provider IDs are inventory-local and can change after a browser runtime reset.
Bind evidence to the chat/session and tab ID, with transport `iab`; do not reuse
an old numeric provider ID or treat a Chrome extension timeout as an IAB denial.
When a previous navigation timed out, first inspect that same provider's tabs;
do not blindly create duplicate tabs. A user-confirmed permission change allows
a fresh supported probe but is never itself success evidence.
