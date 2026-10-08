# Exact recovery obligations

Server-created recovery links are recorded atomically with their human request.
They name original post IDs, original recipients and exact request versions.
Post bodies, `answer_to`, and ordinary artifact references do not create recovery
links. Existing legacy requests remain unchanged; their meaning is never inferred.

`recovery.record(..., requires_diagnostics=False)` describes a pure pickup
acknowledgement. Unstick must set `requires_diagnostics=True`, because its root
cause finding and prevention proposal remain deliverables after work resumes.

For an Unstick recovery to retire, the executing agent must:

1. Pick up every explicitly linked original request through normal guarded
   `board_request_progress(state="started")`. Routing carries exact link versions;
   audited same-session block/restart and evidenced completion remain traceable.
2. Publish an unsealed `finding` and an unsealed `proposal`, both in the recovery
   thread, after its creation, authored by that executing session. Each must
   include `{kind: "artifact", path: "board:post/<recovery_post_id>"}` in `refs`.
   A finding still requires its normal file or commit reference with a revision.
3. Include both diagnostic post IDs in the original request's pickup
   `evidence_post_ids`. If already started, update that same started request with
   the explicit evidence and current version. This records the evidence without
   pretending the original work has finished.

The server checks the typed evidence contract, not the quality of agent prose.
It retires only the untouched queued recovery acknowledgement, stores the
finding, proposal and server receipt as evidence, and preserves original work
states. Changed, claimed or sealed recovery obligations are left for their
explicit owner. Partial diagnostics or unrelated later messages do not suffice.

## Ended-owner bookkeeping recovery

`transfer_ended_owner` is an explicit same-agent, exact-version action for an
unmanaged queued or blocked request whose original dispatcher session has ended.
It preserves authorization lineage, task authorization, host denial, and later
browser/runner execution preflight. It neither executes work nor touches files.

A different old checkout must pass conservative inactivity and clean-Git checks.
For the exact same checkout, a successor holding an active authorized task in
the source thread may keep its own ongoing dirty work. Old-owner leases or renewed
liveness, other active or unknown peers, unrelated active runs, and unfinished
Git operations still prevent transfer. Only the exact successor dispatcher run,
validated against session run ID, agent, source thread and checkout, is excluded
from the active-run fence. The authentic successor performs this action itself.
