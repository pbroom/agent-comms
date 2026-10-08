# Repository tool preflight for dispatched Claude runs

Enable only for a human-approved repository in the existing `[dispatch]` table of
`board.local.toml`:

```toml
claude_tool_projects = ["/absolute/path/to/approved/repository"]
```

Restart the dispatcher to load the opt-in. The default is empty. Other projects,
non-Claude runners, existing authorization rules, and launch budgets are unchanged.
The existing configured worktree determines the working directory. This is a
launch-scoped permission profile, **not an operating-system sandbox**.

For an opted-in project the runner uses `dontAsk` and a fixed list in
`runner_preflight.ALLOWED` in place of broad runner-argv permissions. It permits
repository edits, Git status/diff/log/show/rev-parse/add/commit, pytest through uv or
directly, and GitHub CLI version/read-only PR/check/run inspection. It does not
pre-approve arbitrary Bash, git reset/clean/push, GitHub merge/API/delete, or new
machine privileges. The native Claude permission engine checks compound commands;
these patterns are not our own shell parser. Tests and Git hooks execute repository
code, so only opt in repositories whose implementation work the human approved.
Inherited user, project, local, and managed ask/deny policies stay in force. The
profile rejects conflicting settings/session/directory overrides instead of
silently replacing them. Existing broader permissions outside the runner argv
are not revoked by this feature.

Before the work prompt, the **actual configured Claude CLI** executes separate
`git status --short`, `uv run pytest --version`, and `gh --version` calls. The board
keeps the request queued. It verifies the CLI's command-specific tool-use/result
receipts, successful final result, and absence of permission denials. Prose or a
self-reported capability is insufficient. The process is bounded to 120 seconds
(and normal process-group termination/kill grace), with six turns and a 4 MiB
accepted transcript limit. This is one budgeted launch, not a retry loop.

Only then does the dispatcher resume the **same Claude conversation ID**, in the
same directory with the same configured permissions and environment. Before that
resume it rechecks dispatcher ownership, pause, original approval revocation and
expiry, thread openness, and exact request assignment/version. The request's
`started` transition also requires verified server-owned proof, matching captured
Claude conversation ID and working directory. Clients without trusted conversation
capture must report the concrete blocker; an HTTP caller cannot supply that ID.
CLI exit/spawn errors, denied tools, changed authority or incomplete receipts leave
an explicit blocked request. A dispatcher restart during preflight fails closed
through ordinary orphan reconciliation rather than repeating work.

This verifies tools at that moment, not future permissions, GitHub authentication,
remote push access, or a successful test suite. A permission change or tool failure
mid-run must still produce an explicit `blocked` update. Completed work still
requires the normal request-specific evidence; human clicks are not agent pickup.

Implementation follows installed `claude --help` and the native permission rules:
https://code.claude.com/docs/en/permissions (deny/ask rules precede allow rules).
