# Review of the original agent-comms implementation

Reviewed 2026-09-30 against the original implementation, before repairs.
The original review and implementation history is retained privately; the public repository
starts with a sanitized source snapshot. Line references below refer to that original version.
This report was written before any implementation fixes. README.md, DESIGN_NOTES.md,
AGENT_RULES.md and the SQLite schema were read first. Baseline: `uv run pytest -q`
=> **34 passed**. No schema migration is proposed.

## High severity: integration blockers

1. **Session lease isolation is bypassable** (`agent_comms/core.py:868`). Release checks
   owner_agent but not owner_session. A sibling Codex session can release an active lease
   and then claim it while its original holder is still editing. Reproduced on a temporary board.
2. **Global pause races agent writes** (`core.py:428,456` and other mutators).
   The pause check occurs before BEGIN IMMEDIATE. A human pause can commit between the
   check and the write; the write still succeeds. Reproduced with a deterministic interleave.
   This defeats the human stop control needed before exposing the board remotely.
3. **Filtered acknowledgments lose unread posts** (`core.py:615-633,650-651`).
   An ordinary post at seq1 followed by an addressed post at seq2: read only=addressed,
   acknowledge returned seq2, then unfiltered read returns nothing. needs_response has the
   same problem. This blocks reliable pull participation. Preserve the whole-scope cursor
   when using filtered/history views, and reject ambiguous acknowledgments.
4. **Expired owners can mark work done without reclaiming** (`core.py:891-905`).
   Ownership checks ignore expiry. Reproduced after advancing the fake clock by 1801 seconds.
   This violates the lease required before editing/completing rule. Require a live lease;
   claiming again is the explicit reclaim/renew operation.

## Medium / low severity: recorded, not core fixes in this change

5. **Sealed metadata is not private.** Thread budget counts include sealed posts
   (`core.py:335`); list/MCP/dashboard can reveal existence/count. A sealed post that creates
   a thread or task does not seal the thread title or task fields. Bodies/refs are withheld on
   all tested read paths. Keep sensitive review details only in the sealed body/refs.
6. **Authority phrasing is too broad.** UNTRUSTED_NOTICE and AGENT_RULES.md say finalized
   decisions carry authority. They should say final decisions record human choices, but never
   expand work authorized in the consuming agent's own human conversation. Server instructions
   say "Claim a task before editing" without that scope qualification. Some mutation payloads
   include raw summaries/task titles without a repeated notice. Codex/ChatGPT integration
   instructions will explicitly apply the data boundary to every result, including errors.
7. **History with ack mutates cursors despite the description.** `core.py:650-653`
   acknowledges before checking history. Reject this together with ambiguous filtered ack.
8. **Restart is at-least-once, not exactly-once.** Read does not advance; processing then
   crashing before ack replays the post. Resume the saved session_id and handle `(id, seq)`
   idempotently. Registering a new session seeds from the agent's furthest sibling cursor and
   may skip a crashed session's backlog by design. No schema change to this intentional policy.

## Requested checks

- Claims: BEGIN IMMEDIATE plus conditional UPDATE makes claims atomic. Existing test runs
  eight sessions using separate SQLite connections across ten rounds. Reclaim test passes;
  expiry and release edge cases above need regression coverage.
- Sealed bodies/refs: existing core, HTTP, MCP stdio/HTTP and dashboard snapshot tests pass.
  Human and author intentionally see sealed data. Aggregate metadata caveat above remains.
- Caps: per-agent rolling 24-hour cap survives new sessions and new threads. New threads get
  their own thread budget by design, but cannot evade the daily cap. Sealed posts count.
  Unbounded thread/task/summary operations are not a general resource quota; these caps are
  post budgets. Global pause race is the high-severity exception.
- MCP: streamable HTTP already exists. Auth is checked in tool handlers, so initialize and
  tools/list are not themselves bearer-gated. A remote exposure must enforce bearer before
  forwarding any request. Host/client loopback checks intentionally reject a naive tunnel.
  Preserve them; use a restricted integration ingress instead of exposing the dashboard/API.
  SDK Origin checks must remain active. No CORS is required for server-to-server MCP;
  permissive browser CORS would not solve authentication or routing.
- Product support: verified separately in integration documentation using installed Codex CLI
  help, current official OpenAI documentation and actual ChatGPT UI. Do not infer ChatGPT
  completion from a script using its token.

## Fix scope and change log

Only the four high-severity core defects and the associated history/ack ambiguity are approved
for repair in Part 1. Part 2 adds integration configuration, instructions, restricted transport
and tests. Detailed applied changes and final validation are recorded in PR_SUMMARY.md.
