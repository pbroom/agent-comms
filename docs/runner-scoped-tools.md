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

The run goes straight to its work prompt; there is no separate tool-preflight run. The prompt ends with the
command forms it can use (`runner_preflight.tools_notice`, built on the node from the run's own scoped command line):
the git commands, new branches as `codex/<name>`, `uv run pytest ...` or `pytest ...`, and the read-only `gh`
commands, opening with "other commands may be refused without asking". That plain text is used only for a runner
whose options are all known not to restrict tools; any other accepted option (a deny rule, `--tools`,
`--restricted`, a plugin, or one this code does not know) gets the same list with a warning that any of these forms
may also be refused; its rule text is never copied in. A runner's own `--settings` is refused, as above. The
prompt also tells it that a denied tool is a blocker: it marks the request `blocked`, naming the tool, and stops,
and the request then reaches the human. A hosted session in such a project gets the same text through
`--append-system-prompt` (a resumed session keeps the text it was started with until it is compacted: start a new
session after an upgrade or a rule change); when a command is refused it blocks the board request it is working
(a root woken by a board post holds that request) and otherwise says in its reply which command was refused (a
human's turn or a child's task has no request post). A session asked for at `read-only` and a runner that
declares itself read-only are not scoped. A run that ends without settling its request is retried a bounded number
of times (README "Dispatcher") and then blocked. Completed work still requires the normal request-specific
evidence; human clicks are not agent pickup.

Implementation follows installed `claude --help` and the native permission rules:
https://code.claude.com/docs/en/permissions (deny/ask rules precede allow rules).
