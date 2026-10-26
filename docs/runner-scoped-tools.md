# Scoped tools for dispatched Claude runs

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
repository edits, Git status/diff/log/show/rev-parse/add/commit, fetch from origin
(`git fetch origin`, `git fetch origin main`; the remote must be explicit),
new `codex/*` branches and worktrees, pytest through uv or
directly, and GitHub CLI version/read-only PR/check/run inspection (`gh pr view`,
`gh pr list`, `gh pr diff`, `gh pr checks`, `gh run view`), so a run can confirm a
PR merged before it settles the request. It does not
pre-approve arbitrary Bash, git reset/clean/push, GitHub merge/API/delete, or new
machine privileges. Force/discard flags are explicitly denied. The native Claude permission engine checks compound commands;
these patterns are not our own shell parser. Tests and Git hooks execute repository
code, so only opt in repositories whose implementation work the human approved.
Inherited user, project, local, and managed ask/deny policies stay in force. The
profile rejects conflicting settings/session/directory overrides instead of
silently replacing them. Existing broader permissions outside the runner argv
are not revoked by this feature.

The run goes straight to its work prompt; there is no separate tool-preflight run. The prompt tells the agent
that a denied tool is a blocker: it marks the request `blocked`, naming the tool, and stops, and the request then
reaches the human. A run that ends without settling its request is retried a bounded number of times (README
"Dispatcher") and then blocked. Completed work still requires the normal request-specific evidence; human clicks are
not agent pickup.

Implementation follows installed `claude --help` and the native permission rules:
https://code.claude.com/docs/en/permissions (deny/ask rules precede allow rules).
