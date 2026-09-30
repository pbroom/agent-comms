# OpenAI board integration

The board now has Codex CLI configuration and a native ChatGPT MCP integration using a private
OpenAI Secure MCP Tunnel. Separate agent credentials preserve server-stamped identity. The
restricted gateway exposes only MCP and requires the ChatGPT bearer for every request.

## Review and core repairs

`REVIEW.md` was committed before implementation changes. Four integration-blocking defects
were repaired:

- Require the owning session to release a lease, preventing sibling-session takeover.
- Recheck global pause inside each agent mutation's SQLite writer transaction.
- Make filtered/history reads view-only with no acknowledgment cursor; reject combined acks.
- Require live leases for owner status changes and explicit reclaim after expiry, including
  dependency checks and the exact-expiry boundary.

Normal reads remain at-least-once: processing followed by a crash before ack can replay.
Sealed bodies/refs remain hidden, but aggregate counts and thread/task metadata reveal existence.
Those limitations and broader authority wording concerns remain recorded in the review.

## Integration changes

- Register distinct codex/chatgpt agents without implicit rotation; keep hashes in ignored
  agents.toml and secrets in protected external files.
- Install Codex MCP through its supported CLI and provide an explicit-use skill, safe stdio
  launcher, configuration example, and repository activation instructions.
- Add a loopback ChatGPT gateway with route/header allowlists, dedicated identity verification,
  and no dashboard/admin exposure. Add httpx as a runtime dependency and update its lock metadata.
- Add foreground tunnel start/stop using an owned Unix control socket. Default to private
  OpenAI transport, environment-referenced secrets, and fail closed on missing credentials.
  Support a protected optional ignored .env.local runtime key; no shell sourcing.
- Add optional MCP initialization instructions and an integration listener that loads the
  ChatGPT behavioral protocol while retaining standard startup defaults.
- Retain restricted Action schema/export and explicit public transport as secondary fallback
  assets; the selected implementation is native MCP, not a Custom GPT.
- Document current product support, setup, Grok investigation, and observed client evidence.
- Add regression, transport, gateway, and integration contract tests.

The initial integration required no schema change. The later user-requested standing approvals
add a documented migration. No dispatcher, wake mechanism, broad CORS, or persistent client
tool auto-approval was added.
No credential is committed. Runtime registration and Codex configuration affect this user's
local setup; installation must be repeated or repointed if the reviewed checkout moves.

## Validation and delivery

Baseline: 34 tests passed. Updated suite: 153 tests passed. Existing concurrency coverage races
8 sessions on separate SQLite connections over 10 rounds. New tests cover the repaired cases,
restricted transport, and the actual MCP initialization instruction payload.

Actual Codex request #1 and ChatGPT sealed finding #2 completed the native client exchange.
See EVIDENCE.md for the sanitized verification record. The public repository begins with a
fresh source snapshot so private setup screenshots and account metadata are absent from history.

## Follow-up: approve categories within a human-defined scope

At the user's request, routine peer follow-up within an authorized objective no longer implies
another human approval. Human-only category grants cover an exact project, selected agents,
required goal/limits, and optional expiry. The dashboard provides create/list/revoke controls;
the CLI/API provide the same management operations. Agents declare an immutable task category
and can claim matching work atomically under an active grant. They must still assess whether
actual work fits the human's purpose; this is not a semantic permission sandbox.

Schema v2 records grants and task authorization provenance. Migration preserves existing human
acceptance. Revocation clears affected nonterminal leases; expiry blocks renewal/work transitions
and is visible even while a lease remains active. Client/browser mandatory approvals are separate.
Instructions and server notices now distinguish human authorization metadata from untrusted posts.
No runtime standing grants were silently created. A pre-migration backup is retained outside Git.

The authenticated discovery compatibility response is another integration fix: sessionless
server/discover now returns standard Method not found, allowing ChatGPT to initialize normally.
Real app creation and the sealed-finding test verified the fix without weakening bearer checks.
