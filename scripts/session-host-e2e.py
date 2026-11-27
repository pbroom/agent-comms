#!/usr/bin/env python3
"""End-to-end check of hosted sessions: a real hub, a real `board node`, the real `board session` CLI.

Everything lives in one temporary directory: the hub's board.toml, agents.toml and database, the node's own
board.toml and logs, the project checkout, and the identities (a human, an agent, a node) made for this run. The hub
and the node are subprocesses on free localhost ports and are stopped at the end. Nothing here reads or writes the
real board database, board.local.toml, agents.toml, the real token files or ~/Library/LaunchAgents.

    python scripts/session-host-e2e.py                         # stand-ins for Claude and Codex (tests/stand_in_*.py)
    python scripts/session-host-e2e.py --claude-bin "$(command -v claude)"    # the real CLI, with --model haiku
    python scripts/session-host-e2e.py --codex-bin "$(command -v codex)" --only codex     # the real Codex CLI
    python scripts/session-host-e2e.py --gateway               # the node reaches the hub through `board gateway`
    python scripts/session-host-e2e.py --only tree             # parent and child sessions only (below)

A hosted Codex session comes first (`--only claude` skips it, `--only codex` runs nothing else):
C1. `board session start <project> <message> --agent <the Codex agent> --wait`: the node starts `codex app-server`,
    finishes the handshake (nothing of the message is written before it) and the turn's reply is printed.
C2. `send --wait` to the idle session: written within --max-latency.
C3. A long turn and a second `send --wait` right behind it: queued, the long turn is not interrupted.
C4. The node is stopped (the session's process goes with it) and started again; the next message resumes the SAME
    Codex thread (`thread/resume` with the id the node recorded) and its reply shows the earlier turns are there.
C5. `close` ends the session and its process.
The default is tests/stand_in_codex.py installed as `codex`. With --codex-bin the real CLI runs in the temp checkout,
made a fresh git repository Codex has never seen, with the workspace-write sandbox (the shipped runner's),
`--codex-model` (default gpt-6-luna) at low effort, plugins and apps off, and every MCP server of the user's Codex
configuration disabled by a `-c` override for these processes only (the script asks `codex mcp list --json` for
their names and checks that the overrides took): five small turns. The user's ~/.codex/config.toml is never
written, by the script or by Codex: its mtime and SHA-256 are taken before anything starts and compared at the end
(a `thread/start` that named the checkout would have Codex record it there as trusted; the session names none, and
the script checks that too). Codex keeps its threads in the user's Codex home (~/.codex/sessions), as any Codex run
does.

Then the Claude session. What it does and checks:
1. `board session start <project> <message> --wait`: the turn runs, the reply is printed.
2. `board session send <id> <message> --wait` to the idle session: written to the process within --max-latency.
3. A long turn, and right behind it a second `send --wait`: the second message is queued while the first turn is in
   flight, the first turn is not interrupted, and the second is written within --max-latency of the first ending.
4. A second agent identity posts a request addressed to the hosted agent, in a thread of the project, over the
   hub's HTTP API: the idle session takes a wake turn within --max-wake. The line written to the process's stdin
   names the thread and the counts and holds no part of the post's body.
5. Two more posts inside the hub's wake interval, one over HTTP and one written straight to the database by another
   process (what `board mcp` does): no wake until the interval has passed and the turn before is over, then ONE wake
   for both.
6. One process took every turn; `close` ends the session and its process; a later post wakes nothing.
7. The project takes a new session; stopping the node stops that session's idle process too.

The session's process is started through a small wrapper named `claude` that copies every stdin line to
logs/session-stdin.log before passing it on, so the script can check what was written. With the real CLI the wrapper
runs `claude -p {prompt} --permission-mode dontAsk --setting-sources= --strict-mcp-config` (no user settings, hooks or
MCP servers) in the empty temp checkout: seven small haiku turns.

    python scripts/session-host-e2e.py --claude-bin "$(command -v claude)" --with-board

runs another, shorter scenario (two haiku turns) in which the session has ONE MCP server: this checkout's `board mcp`
on the temp hub's database, signed in as the temp agent from a token file in the temp directory, with only that
server's tools allowed. The second agent posts a request addressed to the hosted agent and the session is told
nothing but the wake. The script reports what the session did (its tool calls, its reply, the board session it
registered, each board call's arguments and whether its result held the post) and checks that the hub noted the
session was shown the post (hosted_wake.handed; a delivery receipt too when the read was unfiltered) and that
`hosted.holds` is true for it
once the wake's grace has passed.

Then the send modes on a running turn (`--only steer` runs nothing else; DESIGN_NOTES "Restarting a running turn"),
for a Claude session (an agent of its own, whose runner may also run `sleep`) and a Codex one:
S1/S4. A long turn (the stand-in holds it until the script releases it; a real model runs `sleep 12`), then
    `--mode steer` (DESIGN_NOTES "Mid-turn steering", step 2). Claude (S1): refused with the reason (its adapter does
    not steer: this node runs with Claude steering switched off, `claude_steer = false`, so with --claude-bin this
    proves the off configuration on the real CLI), nothing is sent, and the turn runs to its end. Codex (S4): taken
    as a steer into that turn (no message of its own); its `turn/steer` is written once; `--mode steer --wait` (a retry with the same request id) prints
    the turn's reply and exits 0 with the steer `in_turn`.
S2/S5. A long turn (`sleep 12` again), then `--mode restart --wait`: the long turn ends `interrupted by a restart:
    message N replaced it` (the provider said the interrupt ended it), the new message is written only after that
    end, as an ordinary turn, and answered (kiwi), and the same process took it (the in-band interrupt).
S3/S6. `--mode auto` on the idle session: a turn of its own, written within --max-latency.
With --claude-bin and --codex-bin those sessions are real (three small turns each).
Then Claude turn correlation (`--only correlation` runs nothing else; DESIGN_NOTES "Claude turn correlation"), for a
Claude session of its own agent (its runner may run `sleep`). VERIFIED ships {2.1.330}, which the stand-in does not
report; these steps run on a hub and a node of their own, after the scenarios above have stopped theirs and before any
below start (X0), the node with the CLI's own version (`claude --version`; the stand-in's "2.1.330-stand-in") put
in VERIFIED in that node process alone (NODE_VERIFIED), so its lines carry a fresh uuid and its processes are
attributed by evidence; X5 stops them and checks nothing is left. The release gates are measured here on the real
CLI:
X1. `start --wait`: the line written (the wrapper's stdin log) carries a uuid, and the node's transcript shows the
    turn's `init` echoing it in `user_message_uuids` and its `result` in `user_message_uuid(s)`.
X2. M3: the node is restarted, so the session's next process is started with `--resume` (exactly one new resume):
    its first turn, and a second one in the SAME process, echo their lines' uuids the same way, and that process's
    `init` names the expected version and `msg_lifecycle_v1`.
X3. M7 (in part): a long turn (`sleep 12`) restarted with `--mode restart --wait`: its aborted `result`
    (`aborted_tools`) still echoes the interrupted line's uuid; the restart's message is answered.
X4. (stand-ins only) The fix: a turn the CLI starts itself (`bg`) right after one turn is NOT taken for the next
    message's; that message gets its own reply. (With VERIFIED empty it would be, exactly as on main:
    tests/test_claude_correlation.py.)
With --claude-bin: five small haiku turns.
Then mid-turn steering, step 2 (`--only steering` runs nothing else; DESIGN_NOTES "Mid-turn steering, loose
contract" and "Mid-turn steering, step 2: Codex steering, as built"), on a hub and a node of their own with child
sessions on, after every scenario below has stopped its own (Q0). With --codex-bin the Codex sessions are real
(gate M5 is measured on that CLI); the Claude ones are the stand-in unless --claude-bin.
Q1. M5 on a fresh start: a Codex root with a long turn; its turn/start carries `clientUserMessageId` N; the answer
    to that request names T and a userMessage item with N's clientId comes in T (the node's transcript; printed raw).
    Then `send --mode steer`: written once as `turn/steer` with `expectedTurnId` T; `--wait` (a retry) exits 0 with
    the steer `in_turn`, Codex having answered that request naming T before T's `turn/completed`.
Q2. M5 on a join: with the real CLI, a probe of its own app-server (the node's flags, a fresh directory of the run):
    a long turn A, then a turn/start J while A runs: the answer names A's turn and J's clientId item comes in it
    (printed raw). With the stand-in, through the node: a turn/start the thread joins (`join-own`), proved and
    steered (`in_turn`).
Q3. The refusals: a Claude turn (its adapter does not steer; with a real CLI of a version that steers, step 3, it is
    refused for having no turn in flight); the cap (12,288 bytes); a second steer while one is
    unsettled; a restart while it is unsettled; `--output-schema` with a steer (refused before sending);
    `board session list` marks it; after N's end no turn is in flight.
Q4. A parent (the spawning agent's stand-in session, with a spawn grant) steers its Codex child through its own
    board server: `board_session_send(mode=steer, wait=true)` takes the child's result for that turn with the steer
    `in_turn`, and the result's text carries the hub's line outside the child's markers.
Q5. (stand-in only) Codex refuses the steer (`not_delivered`, exit 5), answers an internal error and never answers
    (`unconfirmed`, exit 6).
Q6. (stand-in only) The node restarts after the steer was written: N ends with its process, the steer is
    `unconfirmed`, the session goes on and the steer is never written again.
Q7. (stand-in only, and only with `--only steering`: it takes 82 s) A partition before the steer could be written
    (the turn not proved yet): the hub is stopped (SIGSTOP) past the node's 75 s contact clock; once it is back and
    the turn proved, the steer is still never written; N completes and the steer is `not_delivered`.
Q8. Stop; nothing is left; the user's Codex config.toml is unchanged (with --codex-bin).
(Claude steering is on by default, `[dispatch.sessions] claude_steer` absent. The first node of this script (the
codex, claude, steer, structured-off, check-off and board scenarios, and correlation's, which reuses its settings) sets
it off explicitly, `claude_steer = false`, so S1 proves the off configuration; every later node (the tree scenario's,
and the steering section's own, Q1-Q8 and Z0-Z4, `--only steering`) has NO key, so the steering section proves the
default: Q3 expects a version that passed gate M8 to steer, and Z1-Z3 steer.)
Then Claude steering, step 3 (with `--only steering` too; DESIGN_NOTES "Mid-turn steering, step 3: Claude steering,
as built"), on a hub and a node of their own whose VERIFIED and STEER_MODES name the Claude CLI's version (that node
process only): the stand-in's, or with --claude-bin the real one's (gate M8 as an e2e check, as M5 is for Codex):
Z1. A human steer into a Claude tool turn: written once, with its own uuid, into N's owned bracket; N's result lists
    N's uuid and S's: `in_turn`, exit 0.
Z2. A human steer into a text-only turn: N's result lists only N; N stays in flight on the hub with its checkpoint
    (`held`) while an `init` echoing exactly [S] opens S's own turn; then N settles once: N's reply, `next_turn`,
    S's own reply after it, exit 0 (the raw frames printed: M8's evidence through the node).
Z3. (stand-in only) A close while S's own turn runs: N's native end first, `next_turn` with S failed ("its own turn
    was cut"), then the session ends.
Z10. (stand-in, and the real CLI: gate M8 in a RESUMED process, ruling E) A session answers a first turn; the node
    restarts, so its next process is started with `--resume`, as the node resumes any hosted session; a human steer
    into that process's text-only turn: N's result lists exactly [N] and S's own turn exactly [S] (`next_turn`), or
    N's lists exactly [N, S] (`in_turn`); the raw frames printed. With the real CLI three runs; with E2E_KEEP_FRAMES
    naming a directory, each run's frames and lines are kept there (for the fixtures).
Then, stand-in only (review round 1, ruling D), before Z4:
Z5. A parent steers its Claude child with `board_session_send(mode=steer, wait=true)`, twice (two children): the
    wait takes N's result with `next_turn`, S's own reply in a block of its own after the hub's line; the second S
    fails as its own turn, and its failure detail is kept (in the facts and in the text).
Z6. The node restarts while N is held and S's own turn runs: N's native end, `next_turn` with S cut; the session
    goes on in a new process and S is never written again.
Z8. The bound: with a turn timeout of 12 s (that node run only), S's own turn outlasts it: N's native end comes
    with `next_turn`, S cut ("timed out"), within the bound (two turn timeouts plus 2 x kill_grace of N's end).
Z9. A turn the CLI starts itself (a background notification) takes S in: `unconfirmed` (`consumed`), exit 6, N's
    reply as it was.
Z7. (only with `--only steering`: it takes 82 s) A partition while S's own turn runs: the hub is stopped past the
    attempt's 75 s contact clock; the node cuts S's turn; once the hub is back, N's native end with `next_turn`, S
    cut (its contact ran out).
Z12. (review round 2) The node CRASHES (SIGKILL, no shutdown) while N is held with its checkpoint committed: the
    node started again settles N by the checkpoint (N's own reply), S `unconfirmed`; the session goes on and S is
    never written again.
Z11. (only with `--only steering`: it takes 82 s) A partition with NO committed checkpoint: the hub is stopped right
    after S is written, before N ends, past the attempt's contact clock; the checkpoint is never taken; once the hub
    is back N settles by the node's own end, S cut.
Z4. Stop; nothing is left; no steer text in the node's log.
Last, parent and child sessions (`--only tree` runs nothing else; `--only claude`, `--only codex` and --with-board
skip it). Everything above ran with child sessions OFF, as shipped. The hub and the node are then started again over
the same temp directory with `[dispatch.sessions.spawn] enabled = true` on the hub, and on the node a worktree
directory inside the temp directory, `allow_full_access = true`, a Claude runner that may edit files in its run
directory (`dontAsk` with Write and Edit allowed there, no Bash) and a second Claude agent whose runner bypasses
permission checks. The temp checkout becomes a Git repository with one commit.
T1. A Claude parent: `board session start <project> <message> --wait`; its node reports its access (`workspace`).
T2. `start --parent <id> --access read-only`: the child is asked to write a file with the Write tool and cannot.
T3. `start --parent <id> --isolation worktree`: the child writes a file; it is in the child's own worktree (made
    from the checkout's HEAD) and not in the parent's checkout.
T4. `start --parent <id> --agent <the Codex agent> --effort low`: a Codex child under the Claude parent, in the
    parent's checkout, with the sandbox its runner has. Its `thread/start` names no `cwd` (Codex would record a
    named directory as trusted); Codex reports the checkout as the thread's working directory all the same.
T4b. The same with `--isolation worktree`: no `cwd` either, and the thread works in the child's own worktree,
    because that is where its process was started.
T4c. A settings file does not reach a read-only child. The temp checkout has a `.claude/settings.json` that
    allows `Bash(*)`, `Write`, `Edit` and `Read`, and has two COMMAND HOOKS: a SessionStart hook and a PreToolUse
    hook on Read, each appending its input to a log in the temp directory. A third Claude agent's runner loads that
    file with `--settings` (in `dontAsk`, with nothing on its command line allowing anything). A `workspace` child
    of that agent reads a file, writes one and runs a command, and both hooks ran for it: grants and hooks are in
    force. A `read-only` child of the same agent, asked the same, reads the file and can do neither of the other
    two; NO hook ran for it (it is started with `--setting-sources=` and without the runner's `--settings`), and
    its process reports `dontAsk` and no tool but Read, Grep and Glob. The same runner also loads a PLUGIN directory
    (`--plugin-dir`, in the temp directory) whose SessionStart command hook writes a log: it ran for the workspace
    child and not for the read-only one, which is started without the runner's plugin options.
    Bypass is switched off in both (`permissions.disableBypassPermissionsMode`): the workspace child is started
    from a settings file its node wrote (the runner's own settings and that key, mode 600). Its process was asked
    what is in force FIRST and alone (`get_settings`), said the key is, and only then was the turn written to it;
    the transcript keeps the node's one-line record of the answer and not the answer, which is every setting in
    force. (What such a process answers is shown by asking one this script starts itself.)
T5. `board session list` shows the six under their parent; `show` of the parent lists them.
T6. `close` of the parent ends all six; no process is left; the worktree that holds a file stays, and `show` says
    where it is; the Codex child's worktree, which holds nothing of its own, is removed.
T6b. A mode from a settings file never takes effect. A second temp checkout has a `.claude/settings.json` whose
    `defaultMode` is `bypassPermissions`, and a fourth Claude agent's runner loads it with `--settings` and names no
    mode itself. The node does not allow full access. Every hosted Claude session is started with an explicit
    permission mode (a runner that names none gets `manual`), and an explicit mode beats the file: the session's
    process reports `default`, the command it was asked to run is refused, and the session is an ordinary
    `workspace` one. (Were the process to report `bypassPermissions` after all, the host would end it: that is a
    deterministic test, tests/test_hosted_tree.py.) And it cannot be PUT into that mode: the session is started
    from a settings file its node wrote, the fixture plus `permissions.disableBypassPermissionsMode = "disable"`;
    its process was asked what is in force before the turn was written and said the key is; and a process this
    script starts itself with the very same command line (as the wrapper recorded it) names both the fixture's
    `defaultMode` and that key when asked, and answers a `set_permission_mode` request for `bypassPermissions`
    with the error `bypass_disabled`.
T6c. The node is restarted with `allow_full_access = true`.
T7. Full access: a root of the bypass runner is `full`, and once its process has said so (`bypassPermissions`, in
    its `init`) a child may be `full` under it.
T7b. The node is restarted. The root has no process now, so nothing has confirmed its full access in this node
    process: `show` still says `full` (what it was started as), a child asked for at `full` is refused, and after
    the root's next turn (its new process reports `bypassPermissions`) the same request goes through. Then three
    children: one of the ordinary Claude agent at `--access workspace`, asked to run `touch <file>` with the Bash
    tool (its `dontAsk` runner: refused); one at the root's own access (`full`: the file is made); and a Codex
    child at `--access full`, whose `thread/start` carries `danger-full-access` and whose answer names that sandbox.
    Closing the root ends all three.
T7c. A runner Claude would refuse to start is refused when the session is opened: `--restricted` beside
    `--permission-mode bypassPermissions`. The session ends with the reason and no process is started.
(With child sessions on, every child's turn above is also a result for its parent, which takes it as a turn: the
steps above do not depend on those turns. The next step looks at them.)
T9. Result delivery. A Claude parent; a read-only Claude child replies one word: that reply is the parent's next
    turn within --max-latency of the child's turn ending, a `child` message whose line (in the wrapper's stdin log)
    starts with the server's header and names the result's id, the child, its id, agent, message and outcome, with
    the reply between the markers; the result counts as delivered once the parent's process said that turn started;
    `show` of the parent lists the result and the message that delivered it.
T9b. A second Claude child and a Codex child: each result is written to the parent once and never again. With the
    stand-ins the parent is first given an 8-second turn, so both results wait and go together as ONE turn after it
    (the Claude child's first); and a child whose turn fails is a result too, with the outcome and the node's reason.
    With real agents (three haiku turns for the parent, two for the Claude children, one Codex turn) the timing
    decides whether the two go together; the script says which it was.
T10. Spawn tools (hosted_spawn.py). A sixth Claude agent's runner has the temp board's MCP server (this checkout's
    `board mcp` on the hub's database) and only its tools allowed. The script, acting as that session's own board
    server (a real `board mcp` process with the session's binding in its environment), finds that without a grant it
    may not spawn and is told why; the human gives a grant (`board session grant ... --access workspace`); a child
    above it (`full`) and a forged key are refused. Then two children, a Claude one and a Codex one: with the real
    CLI the HUMAN tells the session to delegate and the session calls board_session_spawn itself (wait=true) and
    answers from the replies; with the stand-ins the script makes the calls. Each child's turn starts with the
    server's frame, each result says the parent started that work, the tree's audit thread has one automatic post
    per child, and closing the parent ends both (one more audit post each).
T10b. (stand-ins) An interactive caller: a `board mcp` with no binding is the agent's one interactive root for the
    project, counted against the same budget; its child runs on the node, and another board server process (a restart)
    sees it and takes its result, which is never a turn; closing that tree ends the child.
T11. A deeper tree (max_depth 2, shipped). A root of the sixth agent starts a child of the same agent (workspace),
    which starts a read-only Claude grandchild: with the real CLI the human asks the root to delegate two levels down
    and each level calls board_session_spawn itself (wait=true); with the stand-ins the script makes each call from
    that session's own board server (its binding). The child's frame says it may start helpers and that theirs may
    not; the grandchild's says it cannot. The grandchild's result goes to the child only, the child's to the root.
    Each run proves one path, and requires it: with the real CLI each level waits in its own turn, so both results
    are `claimed`, and the root's reply is exactly the grandchild's word; with the stand-ins nobody waits, so both are
    turns (`delivered`: the child takes its helper's result as a turn, and its reply to it is the root's next
    turn), and then the waiting path is run deterministically too (a second grandchild started with wait=true from
    the child's board server, a message to the child sent with wait=true from the root's: both `claimed`). One audit
    post per start naming the direct parent and the root; closing the root ends everything under it, with an audit
    post for each end below it.
A1-A3 (stand-ins only: a real CLI in a fresh account folder has no sign-in). Two accounts on one machine (README
"Two accounts on one machine"): a seventh Claude agent, claude-work, whose runner on the node has an account folder
in the temp directory ([dispatch.runner_homes]), and which the hub opts in to the temp checkout only
([dispatch.agent_projects]).
A1. A session of claude-work: its process is started with CLAUDE_CONFIG_DIR set to that folder (a wrapper records
    it), and `show` and `list` say which folder it is.
A2. A session of claude-work in the second temp checkout is refused with the reason; nothing is made.
A3. The spawning agent's session in the temp checkout sees claude-work among its targets and starts a child of it,
    whose process gets the folder too; an interactive caller of the spawning agent in the second checkout is not
    offered claude-work there, and its spawn of it is refused with the reason.
P1-P4 (stand-ins only). An account pool (README "Balancing between accounts"): the hub's settings get
`pools = {"e2e-pool" = ["claude-work", "claude"]}` while it runs.
P1. The spawning agent's session spawns `child_agent = "e2e-pool"`: the child is claude-work's (the first member),
    and its audit post says so ("first choice").
P2. The work child is sent `usage 95`: the stand-in writes a `rate_limit_event` saying its five-hour window is at 95%,
    the node's next hello carries it to the hub (which asked for it), and the next spawn of the pool goes to claude,
    with `show` saying why (overflow, the first member at 95%).
P3. A rule `only = ["claude-work"]` for the temp checkout: the next spawn is claude-work's again, though it is near
    its limit (the other member is refused there; headroom never refuses work). The same rule for the second
    checkout, where claude-work is not opted in, leaves nothing: refused with the reason.
P4. A rule `never = ["claude-work"]` (and the work account back at 10%): the spawn goes to claude, and so does the
    human's `board session start --parent ... --agent e2e-pool`, whose output says how it was chosen.
D7-D8 (`--only structured`, or alone with `--only check`): the in-turn result check (DESIGN_NOTES "In-turn result
check"), through real `board mcp` processes on the temp hub's database.
D7. With the stand-ins, deterministically (the correction gate): a human root with child sessions OFF has a held
    turn checked (accepted) and settles accepted; with them on, a root of the spawning agent has a held turn checked
    twice (a block that does not fit: missing, shape, the settlement's exact reasons; the fixed one: accepted) and
    settles accepted, and a second one whose accepted check is followed by a reply with no block settles missing
    (no_block): a check is not a result. (That the board writes no durable row is tests/test_result_check.py's, which
    compares the whole database around the checks.) With real CLIs: a root of the spawning agent (board tools)
    reviews with a broken draft, first with the frame's recommendation alone (the task does not name the tool:
    RECORDED), then told in the task to check too: a check answered by the validator (each tool call matched to its
    result by id, the answer parsed) and an accepted settlement REQUIRED; a Codex child whose runner reaches the temp
    board (`e2e-board`, never the user's own `agent-comms` entry) and approves the tool, told in the task: REQUIRED
    the same. Whether a first draft needed correction is recorded.
D8. A Codex runner that lists the tool but does not approve it: with the stand-in, deterministically, its refused call
    is recorded as a denial of exactly that tool and the session shows `waiting`, while the verdict is still the
    reply's; with the real Codex, told to check: recorded, and no check may reach the validator. The frames: every
    asked frame written to a Claude process recommends the check; asked frames were written to Codex processes, and
    none of them does.
The settings fixtures live in the temp directory only. They are loaded with `--settings` because Claude Code
ignores an untrusted directory's own `.claude/settings.json` in print mode (it says so on stderr), and trusting the
temp checkout would mean writing the user's `~/.claude.json`. With the real CLI those two runners also pass
`--setting-sources=project` (not the empty list): the user's own settings and hooks are not run by this script.
With --claude-bin the Claude sessions are real (`--model haiku`, ten small turns), with --codex-bin the Codex
children are (three small turns: two under the workspace-write sandbox, one with the sandbox off, asked for one
word), and the user's `~/.codex/config.toml` is checked
to be unchanged afterwards (mtime and SHA-256), as after the Codex scenario; so are the user's Claude settings files
(`~/.claude/settings.json`, `settings.local.json`) when the Claude sessions are real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_comms.config import create_agent  # noqa: E402

NODE = "e2enode"
AGENT = "claude"
POSTER = "codex"                # the second agent identity: it posts the requests that wake the hosted session
CODEX_AGENT = "codex-host"      # the agent whose hosted session is a Codex one (its own runner on the node)
FULL_AGENT = "claude-full"      # the tree scenario's second Claude agent: its runner bypasses permission checks
GRANT_AGENT = "claude-granted"  # ... a third: its runner loads a settings file that allows Bash(*), Write and Edit
MODE_AGENT = "claude-defmode"   # ... a fourth: its runner loads a settings file whose defaultMode is bypassPermissions
RESTRICTED_AGENT = "claude-restricted"      # ... a fifth: --restricted beside the bypass mode, which Claude refuses
SPAWN_AGENT = "claude-spawner"  # ... a sixth: its runner has the temp board's MCP tools, so it can start children itself
CHECK_CODEX = "codex-checker"   # D7: a Codex agent whose runner has the temp board's server AND approves the check
DENIED_CODEX = "codex-denied"   # D8: a Codex agent whose runner lists the temp board's server but approves nothing
CHECK_SERVER = "e2e-board"      # the temp board's server as those two runners name it (never the user's agent-comms)
WORK_AGENT = "claude-work"      # ... a seventh: another account's runner (an account folder of its own), opted in to
                                # the temp checkout only (A1-A3)
POOL = "e2e-pool"               # P1-P4: an account pool of the work agent first, then the first agent
STEER_AGENT = "claude-steer"    # the send-mode scenario's Claude agent: its runner may also run `sleep` (a long call)
CORR_AGENT = "claude-corr"      # the turn-correlation scenario's Claude agent (X1-X4): the same runner as STEER_AGENT
NO_BYPASS = "disableBypassPermissionsMode"  # the settings key (under permissions) a node adds to every launch but full
GRANTS = {"permissions": {"allow": ["Bash(*)", "Write", "Edit", "Read"]}}     # ... and two hooks (enable_tree)
BYPASS_BY_SETTINGS = {"permissions": {"defaultMode": "bypassPermissions"}}
WAKE_INTERVAL = 6               # the hub's [dispatch.sessions] wake_min_interval_seconds for this run
MARKER = "E2E-BODY-MARKER-4c1e9a"
BODY_WORDS = ("zebra", "quartz", "lantern")

# The turn-correlation steps' node (X1-X4): the node, with VERIFIED set to argv[1] in this process only.
from agent_comms.providers.claude import STEER_MODES   # noqa: E402  (Q3: which Claude versions steer, as shipped)

NODE_VERIFIED = """
import os, sys
from agent_comms.providers import claude
claude.VERIFIED = frozenset(sys.argv[1].split(","))
if os.environ.get("E2E_STEER_MODES"):       # Claude steering's e2e: gate M8's record for the CLI under test
    claude.STEER_MODES = dict(x.split("=", 1) for x in os.environ["E2E_STEER_MODES"].split(","))
from agent_comms.cli import main
main(sys.argv[2:])
"""

# The session's process, wrapped: every stdin line is appended to a log before the real program gets it.
WRAPPER = """#!{python}
import json, subprocess, sys
log = open({log!r}, "ab", buffering=0)
with open({log!r} + ".argv", "a") as started:     # ... and the arguments of every start, one JSON list per line
    started.write(json.dumps(sys.argv[1:]) + "\\n")
child = subprocess.Popen([{real!r}, *sys.argv[1:]], stdin=subprocess.PIPE)
try:
    for line in sys.stdin.buffer:
        log.write(line)
        child.stdin.write(line)
        child.stdin.flush()
    child.stdin.close()
except OSError:
    pass
sys.exit(child.wait())
"""

# A1: the work account's runner, a wrapper named `claude` that records the CLAUDE_CONFIG_DIR its process was started
# with (and its arguments), then runs the ordinary wrapper.
ENV_WRAPPER = """#!{python}
import json, os, sys
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR")}}) + "\\n")
os.execv({inner!r}, [{inner!r}, *sys.argv[1:]])
"""

# A post written straight to the hub's database by another process, as `board mcp` (stdio) does.
DIRECT_POST = """
import json, os, sys
from agent_comms.config import Settings
from agent_comms.core import Board
board = Board(Settings.load())
p = board.authenticate(os.environ["E2E_POSTER_TOKEN"])
sid = board.register_session(p, sys.argv[1])["session_id"]
post = board.create_post(p, sid, body=sys.argv[3], type="request", thread_id=int(sys.argv[2]), to=[sys.argv[4]])
print(json.dumps({"id": post["id"]}))
"""

# Structured results (D1-D6): the review schema of the design, an unsatisfiable one, and the review task.
REVIEW_SCHEMA = {"type": "object", "required": ["verdict", "findings"], "additionalProperties": False,
                 "properties": {"verdict": {"enum": ["ok", "changes"]},
                                "findings": {"type": "array", "items": {
                                    "type": "object", "required": ["file", "line", "note"],
                                    "additionalProperties": False,
                                    "properties": {"file": {"type": "string"},
                                                   "line": {"type": "integer", "minimum": 1},
                                                   "note": {"type": "string", "maxLength": 500}}}}}}
REVIEW_TEXT = json.dumps(REVIEW_SCHEMA)
UNSAT_SCHEMA = {"type": "object", "required": ["x"], "additionalProperties": False,
                "properties": {"x": {"type": "string", "minLength": 2, "maxLength": 1}}}
REVIEW_TASK = """Review this diff for bugs. Page numbers are 1-based in the new code. Do not use any tools: the diff \
is all you need.

--- a/src/pager.py
+++ b/src/pager.py
@@ -1,6 +1,6 @@
 def page(items, n, size):
-    start = n * size
-    return items[start:start + size]
+    start = (n - 1) * size
+    return items[start:start + size + 1]
"""

# The in-turn check (D7-D8): a schema whose length bounds a first draft is likely to miss, and a review task that hands
# over such a draft. The stand-ins take the review schema and their `data-*` turns instead.
CHECK_SCHEMA = {"type": "object", "required": ["verdict", "summary", "findings"], "additionalProperties": False,
                "properties": {"verdict": {"enum": ["ok", "changes"]},
                               "summary": {"type": "string", "maxLength": 20},
                               "findings": {"type": "array", "items": {
                                   "type": "object", "required": ["file", "line", "note"],
                                   "additionalProperties": False,
                                   "properties": {"file": {"type": "string"},
                                                  "line": {"type": "integer", "minimum": 1},
                                                  "note": {"type": "string", "maxLength": 30}}}}}}
CHECK_TASK = REVIEW_TASK.replace("Do not use any tools: the diff is all you need.",       # (the check is a tool)
                                 "You need no file or shell tool: the diff is all you need.") + """
A colleague already drafted the result object for this review; start from it, and check it before you use it:
{"verdict": "changes", "summary": "The end index of the page slice includes one item too many", "findings": [{"file": \
"src/pager.py", "line": 3, "note": "start + size + 1 returns size + 1 items on every page"}]}
"""
CHECK_TELL = ("\nBefore you answer, check your result-data block with the board tool board_session_check_result, and "
              "fix what it names.")
# The board's answers in a transcript: a check's JSON answer names `would_be` (checked) or `checked: false`.
D7_RECORD = []

BOARD_GRACE = 5                 # --with-board: the hub's wake_grace_seconds, short so `holds` is asked after it
BOARD_BOUND = 90                # --with-board: most seconds from the post to the session's delivery receipt

# --with-board: what the hub's database says about one request, read by another process on that database.
PROBE = """
import json, sys, time
from agent_comms import delivery, hosted, hosted_wake
from agent_comms.config import Settings
from agent_comms.core import Board
board = Board(Settings.load())
agent, project, post_id, sid = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
c, now = board.conn, time.time()
rec = hosted_wake._load(c, sid) or {}
rows = [dict(r) for r in c.execute("SELECT id, project, worktree, started_at, last_seen FROM sessions "
                                   "WHERE agent = ? ORDER BY id", (agent,))]
print(json.dumps({"now": now, "holds": hosted.holds(board, agent, project, post_id, now),
                  "woken_at": (rec.get("woken") or {}).get(str(post_id)), "grace": rec.get("grace"),
                  "shown_at": (rec.get("read") or {}).get(str(post_id)),
                  "handed_at": (rec.get("handed") or {}).get(str(post_id)),
                  "receipt": delivery.receipt(c, post_id, agent), "readers": delivery.readers(c, post_id, agent),
                  "sessions": rows}))
"""


# T10: the key that proves a process is hosted session argv[1]'s (what its node put into that process's environment),
# read by another process on the hub's database: so the script can act as that session's own board server.
BIND_KEY = """
import sys
from agent_comms import hosted_spawn
from agent_comms.config import Settings
from agent_comms.core import Board
board = Board(Settings.load())
hosted_spawn.ensure_secret(board)
print(hosted_spawn.bind_key(board.conn, sys.argv[1]))
"""

# T10: board tool calls through a real `board mcp` stdio server, started with the environment this process has (so
# with a hosted session's binding, or none: an interactive session's board server). argv[1]: the agent; argv[2]: a
# JSON list of [tool, arguments]. Prints [{"error": bool, "text": str}] for the calls, in order.
MCP_CALLS = """
import asyncio, json, os, sys
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_comms.cli", "mcp", "--agent", sys.argv[1]],
                                   env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = []
            for name, args in json.loads(sys.argv[2]):
                res = await s.call_tool(name, args)
                out.append({"error": bool(res.is_error), "text": res.content[0].text if res.content else ""})
            print(json.dumps(out))

asyncio.run(main())
"""


class LineReader:
    """Lines from a pipe, never blocking past a deadline (R2-12): `next(deadline)` is the next complete line, or None
    once `deadline` (time.monotonic) has passed; EOFError when the pipe closed."""

    def __init__(self, fd: int):
        self.fd, self.buf = fd, b""

    def next(self, deadline: float) -> bytes | None:
        import selectors
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            # (Review round 5:) a selector, not select(): select() refuses a descriptor above FD_SETSIZE (1024), which
            # a long test run in one process reaches.
            with selectors.DefaultSelector() as sel:
                sel.register(self.fd, selectors.EVENT_READ)
                ready = sel.select(left)
            if not ready:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise EOFError("the pipe closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line + b"\n"


class M5Join:
    """Gate M5 on a join, from the app-server's lines (R2-12): after a turn/start J (request `rid`, `clientId` j)
    while turn T of thread `tid` runs, BOTH the answer to J's request and a userMessage item of that thread with J's
    clientId, in either order, BEFORE T's own turn/completed (that thread and that turn; another turn's completion is
    not T's). `feed(m)`: "proved" once both came in time, "late" when T completed first, else None."""

    def __init__(self, tid: str, t: str, rid, j: str):
        self.tid, self.t, self.rid, self.j = tid, t, rid, j
        self.answer: dict | None = None
        self.item: dict | None = None

    def feed(self, m) -> str | None:
        if not isinstance(m, dict):
            return None
        p = m.get("params") if isinstance(m.get("params"), dict) else {}
        it = p.get("item") if isinstance(p.get("item"), dict) else {}
        if (type(m.get("id")) is int and m["id"] == self.rid and "method" not in m
                and ("result" in m) != ("error" in m) and self.answer is None):
            # (an integer id only: 4.0 is not request 4, R3-12) The answer must name T: one naming another turn
            # fails the probe at once.
            named = ((m.get("result") or {}).get("turn") or {}).get("id") if isinstance(m.get("result"), dict) else None
            if named != self.t:
                return "wrong"
            self.answer = m
        elif (m.get("method") in ("item/started", "item/completed") and p.get("threadId") == self.tid
              and p.get("turnId") == self.t and it.get("type") == "userMessage" and it.get("clientId") == self.j
              and self.item is None):
            self.item = m       # (only an item of T itself: one in another turn never takes its place, R3-12)
        elif (m.get("method") == "turn/completed" and p.get("threadId") == self.tid
              and isinstance(p.get("turn"), dict) and p["turn"].get("id") == self.t):
            return None if self.answer is not None and self.item is not None else "late"
        return "proved" if self.answer is not None and self.item is not None else None

    def holds(self) -> bool:
        """Both observations name T: the answer's turn and the item's turn."""
        a = ((self.answer or {}).get("result") or {}).get("turn") or {}
        ip = (self.item or {}).get("params") or {}
        return (a.get("id") == self.t and ip.get("turnId") == self.t and ip.get("threadId") == self.tid
                and (ip.get("item") or {}).get("type") == "userMessage")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not alive(pid)


def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(x) for x in items) + "]"


class Run:
    def __init__(self, args):
        self.args = args
        self.real = args.claude_bin is not None
        self.real_codex = args.codex_bin is not None
        self.tmp = Path(tempfile.mkdtemp(prefix="session-host-e2e-")).resolve()
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.pids: set[int] = set()
        self.port = free_port()
        self.hub_home, self.node_home = self.tmp / "hub", self.tmp / "node"
        self.project = self.tmp / "checkout"
        for d in (self.hub_home, self.node_home, self.project, self.tmp / "tokens", self.tmp / "logs"):
            d.mkdir()
        # The environment every subprocess starts from: no board variable of the caller's survives, and the token
        # locations point into the temp directory (they are never read: tokens are passed in the environment).
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_COMMS_", "BOARD_"))}
        self.env |= {"PYTHONPATH": str(ROOT), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens"),
                     "AGENT_COMMS_TOKEN_FILE": str(self.tmp / "tokens" / "human.token"), "PYTHONUNBUFFERED": "1"}
        if not self.real and not self.real_codex:
            (self.tmp / "home").mkdir()
            self.env["HOME"] = str(self.tmp / "home")     # the stand-ins need nothing of the user's

    # ------------------------------------------------------------ set-up

    def write_settings(self) -> None:
        agents = self.hub_home / "agents.toml"
        self.human_token = create_agent(agents, "human", "human", is_human=True)
        agent_token = self.agent_token = create_agent(agents, AGENT, "claude-code")
        self.poster_token = create_agent(agents, POSTER, "codex-cli")
        create_agent(agents, CODEX_AGENT, "codex-cli")
        create_agent(agents, STEER_AGENT, "claude-code")
        create_agent(agents, CORR_AGENT, "claude-code")
        self.node_token = create_agent(agents, NODE, "node")
        (self.hub_home / "board.toml").write_text(f"""[server]
host = "127.0.0.1"
port = {self.port}
db_path = "data/board.db"
agents_path = "agents.toml"

[conversations]
enabled = false

[dispatch.nodes.{NODE}]
projects = {toml_list([str(self.project)])}
agents = {toml_list([AGENT, CODEX_AGENT, STEER_AGENT, CORR_AGENT])}

[dispatch.sessions]
wake_min_interval_seconds = {WAKE_INTERVAL}
""" + (f"wake_grace_seconds = {BOARD_GRACE}\n" if self.args.with_board else ""))
        real = self.args.claude_bin
        if not self.real:
            real = self.tmp / "stand-in" / "claude"
            real.parent.mkdir()
            real.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "stand_in_claude.py").read_text())
            real.chmod(0o700)
        self.claude_real = str(real)
        self.stdin_log = self.tmp / "logs" / "session-stdin.log"
        exe = self.tmp / "bin" / "claude"           # the adapter goes by the executable's name
        exe.parent.mkdir()
        exe.write_text(WRAPPER.format(python=sys.executable, log=str(self.stdin_log), real=str(real)))
        exe.chmod(0o700)
        runner = [str(exe), "-p", "{prompt}", "--permission-mode", "dontAsk"]
        if self.real:
            runner += ["--setting-sources=", "--strict-mcp-config"]
        # The send-mode scenario's runner: the same, and `sleep` may run (a turn that waits in a tool call, so a
        # restart arrives while it runs). Nothing else is allowed.
        steer_runner = runner + ["--allowedTools", "Bash(sleep:*)"]
        if self.args.with_board:
            # The session's only MCP server: this checkout's `board mcp` (stdio), on the TEMP hub's database, signed
            # in as the temp agent from a token file in the temp directory. Nothing else is configured or allowed:
            # no user settings (--setting-sources=), no other MCP server (--strict-mcp-config), and only the board's
            # tools are allowed (dontAsk refuses what would prompt; Bash and the file tools are disallowed outright).
            token_file = self.tmp / "tokens" / f"{AGENT}.token"
            token_file.touch(mode=0o600)
            token_file.write_text(agent_token + "\n")
            (self.tmp / "tokens").chmod(0o700)
            mcp = self.tmp / "claude-mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {"agent-comms": {
                "type": "stdio", "command": sys.executable, "args": ["-m", "agent_comms.cli", "mcp", "--agent", AGENT],
                "env": {"AGENT_COMMS_HOME": str(self.hub_home), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens"),
                        "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}}}}, indent=2))
            runner += ["--mcp-config", str(mcp), "--allowedTools=mcp__agent-comms",
                       "--disallowedTools=Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"]
        (self.node_home / "board.toml").write_text(f"""[conversations]
enabled = false

[dispatch]
kill_grace_seconds = 5

[dispatch.runners]
{AGENT} = {toml_list(runner)}
{CODEX_AGENT} = {toml_list(self.codex_runner())}
{STEER_AGENT} = {toml_list(steer_runner)}
{CORR_AGENT} = {toml_list(steer_runner)}

[dispatch.worktrees]
{json.dumps(str(self.project))} = {json.dumps(str(self.project))}

[dispatch.sessions]
enabled = true
codex = true
claude_steer = false
""")

    def codex_runner(self) -> list[str]:
        """The Codex agent's runner on the node: a `codex exec` template, as a dispatched run would use. The session
        host turns it into `codex app-server`. The executable is a wrapper named `codex` that logs every stdin line."""
        real = self.args.codex_bin
        if not self.real_codex:
            real = self.tmp / "stand-in" / "codex"      # it keeps its threads beside itself, in the temp directory
            real.parent.mkdir(exist_ok=True)
            real.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "stand_in_codex.py").read_text())
            real.chmod(0o700)
        if getattr(self, "codex_template", None):
            return self.codex_template          # the tree scenario's node settings name the same runner again
        self.codex_stdin_log = self.tmp / "logs" / "codex-stdin.log"
        exe = self.tmp / "bin" / "codex"               # the adapter goes by the executable's name
        exe.parent.mkdir(exist_ok=True)
        exe.write_text(WRAPPER.format(python=sys.executable, log=str(self.codex_stdin_log), real=str(real)))
        exe.chmod(0o700)
        # The real run uses the shipped runner's sandbox: it is the one under which a `cwd` on thread/start would make
        # Codex record the checkout as trusted in the user's config.toml, which this run proves does not happen.
        self.codex_sandbox = "workspace-write" if self.real_codex else "read-only"
        runner = [str(exe), "exec", "--cd", "{project}", "--sandbox", self.codex_sandbox]
        if self.real_codex:
            runner += ["--disable", "plugins", "--disable", "apps", *self.codex_quiet(),
                       "-c", 'model_reasoning_effort="low"']
        else:
            runner += ["-c", 'mcp_servers.agent-comms.tools.board_post.approval_mode="approve"']
        self.codex_template = runner + ["{prompt}"]
        return self.codex_template

    def board_codex_runner(self, agent: str, approve: bool) -> list[str]:
        """D7-D8: the Codex runner with the temp board's own `board mcp` as one more MCP server (named CHECK_SERVER,
        so the user's own `agent-comms` entry stays disabled), signed in as `agent` from a token file in the temp
        directory; `approve`: the runner approves the check tool (and only it)."""
        base = self.codex_runner()[:-1]          # without "{prompt}"
        env = {"AGENT_COMMS_HOME": str(self.hub_home), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens"),
               "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
        out = base + ["-c", f"mcp_servers.{CHECK_SERVER}.command={json.dumps(sys.executable)}",
                      "-c", f"mcp_servers.{CHECK_SERVER}.args=" + json.dumps(["-m", "agent_comms.cli", "mcp",
                                                                              "--agent", agent]),
                      *[x for k, v in env.items()        # one dotted key each: a runner's argv holds no braces
                        for x in ("-c", f"mcp_servers.{CHECK_SERVER}.env.{k}={json.dumps(v)}")]]
        if approve:
            out += ["-c", f'mcp_servers.{CHECK_SERVER}.tools.board_session_check_result.approval_mode="approve"']
        return out + ["{prompt}"]

    def codex_quiet(self) -> list[str]:
        """`-c` overrides that disable every MCP server the user's Codex configuration names, for the processes this
        run starts only. One of them may be the user's real board: a session here must never start it. The names
        come from Codex itself, and the overrides are checked before anything is started."""
        base = [self.args.codex_bin, "mcp", "list", "--json", "--disable", "plugins", "--disable", "apps"]

        def listed(extra: list[str]) -> list[dict]:
            r = subprocess.run(base + extra, cwd=str(self.tmp), capture_output=True, text=True, timeout=60,
                               env=self.codex_env())
            if r.returncode != 0:
                raise SystemExit(f"`codex mcp list` failed:\n{r.stderr[-800:]}")
            return [x for x in json.loads(r.stdout) if isinstance(x, dict)]

        names = [x.get("name") for x in listed([]) if x.get("enabled") is not False]
        odd = [n for n in names if not isinstance(n, str) or not n.replace("-", "").replace("_", "").isalnum()]
        if odd:
            raise SystemExit(f"cannot disable these MCP servers with a -c override: {odd}")
        quiet = [x for n in names for x in ("-c", f"mcp_servers.{n}.enabled=false")]
        left = [x.get("name") for x in listed(quiet) if x.get("enabled") is not False]
        if left:
            raise SystemExit(f"these MCP servers of the Codex configuration could not be disabled: {left}")
        print(f"codex: {len(names)} configured MCP server(s) disabled for this run's sessions by -c override")
        return quiet

    def spawn(self, name: str, home: Path, *argv: str, **extra_env: str) -> subprocess.Popen:
        log = open(self.tmp / "logs" / f"{name}.log", "a")     # a restarted node goes on in the same file
        verified = getattr(self, "node_verified", None) if name == "node" else None
        steers = getattr(self, "node_steer_modes", None) if name == "node" else None
        if steers:
            extra_env = extra_env | {"E2E_STEER_MODES": ",".join(f"{v}={m}" for v, m in sorted(steers.items()))}
        start = ["-c", NODE_VERIFIED, ",".join(sorted(verified))] if verified else ["-m", "agent_comms.cli"]
        proc = subprocess.Popen([sys.executable, *start, *argv], cwd=str(self.tmp), stdout=log,
                                stderr=subprocess.STDOUT, env=self.env | {"AGENT_COMMS_HOME": str(home)} | extra_env)
        self.procs.append((name, proc))
        return proc

    def log_text(self, name: str) -> str:
        try:
            return (self.tmp / "logs" / f"{name}.log").read_text()
        except OSError:
            return ""

    def wait_for(self, what: str, check, seconds: float = 40.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for name, proc in self.procs:
                if proc.poll() is not None:
                    raise SystemExit(f"{name} exited early (code {proc.returncode}):\n{self.log_text(name)}")
            if check():
                return
            time.sleep(0.1)
        raise SystemExit(f"timed out waiting for {what}")

    def hub_up(self) -> bool:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/whoami",
                                     headers={"Authorization": "Bearer " + self.human_token})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as r:
                return json.loads(r.read())["is_human"] is True
        except (OSError, ValueError, KeyError):
            return False

    def start_services(self) -> None:
        self.spawn("hub", self.hub_home, "serve", "--port", str(self.port))
        self.wait_for("the hub to answer", self.hub_up)
        target = self.port
        if self.args.gateway:
            target = free_port()
            self.spawn("gateway", self.hub_home, "gateway", "--port", str(target))
            self.wait_for("the gateway to listen", lambda: listening(target))
        self.node_port, self.node_runs = target, self.log_text("node").count("connected to") + 1
        self.spawn("node", self.node_home, "node", "--hub", f"http://127.0.0.1:{target}", "--name", NODE,
                   AGENT_COMMS_TOKEN=self.node_token)
        self.wait_for("the node to connect", lambda: self.log_text("node").count("connected to") >= self.node_runs)
        print(f"hub on 127.0.0.1:{self.port}" + (f", gateway on 127.0.0.1:{target}" if self.args.gateway else "")
              + f"; {[x for x in self.log_text('node').splitlines() if 'connected to' in x][-1].strip()}")

    # ------------------------------------------------------------ the CLI

    def board(self, *argv: str, ok: bool = True) -> tuple[str, str, float]:
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-m", "agent_comms.cli", *argv], cwd=str(self.tmp), capture_output=True,
                           text=True, timeout=300,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home), "BOARD_TOKEN": self.human_token})
        took = time.monotonic() - t0
        if ok and r.returncode != 0:
            raise SystemExit(f"`board {' '.join(argv)}` exited {r.returncode}:\n{r.stdout}{r.stderr}")
        return r.stdout, r.stderr, took

    def show(self, sid: str) -> dict:
        view = json.loads(self.board("session", "show", sid, "--json")[0])
        if view.get("pid"):
            self.pids.add(view["pid"])
        return view

    def api(self, token: str, method: str, path: str, body: dict | None = None) -> dict:
        """One call to the hub's HTTP API as the identity with this token."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
            return json.loads(r.read())

    def message(self, sid: str, n: int, until, seconds: float) -> dict | None:
        """The session's message `n` once `until(message)` holds, or as it stands when the time is up."""
        deadline = time.monotonic() + seconds
        while True:
            m = next((m for m in self.show(sid)["messages"] if m["n"] == n), None)
            if (m is not None and until(m)) or time.monotonic() >= deadline:
                return m
            time.sleep(0.1)

    def codex_env(self) -> dict[str, str]:
        """The environment as far as Codex's home goes for a session's process: the node passes HOME on and not
        CODEX_HOME (dispatch.child_env), so that home is ~/.codex."""
        return {k: v for k, v in self.env.items() if k != "CODEX_HOME"}

    def codex_config(self) -> tuple[int, str] | None:
        """(mtime in ns, SHA-256) of the Codex config.toml the session processes use, or None when there is none.
        Read to be compared, never written and never shown."""
        path = Path(self.env["HOME"]) / ".codex" / "config.toml"
        try:
            return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def check_codex_config(self, before: tuple[int, str] | None) -> None:
        """The user's Codex config.toml is what it was when `before` was taken."""
        after = self.codex_config()
        print("  ~/.codex/config.toml: " + ("none" if after is None else
                                          f"mtime {after[0]} ns, sha256 {after[1][:16]}...")
              + (" (as before the run)" if after == before else
                 f"; BEFORE the run: {before and (before[0], before[1][:16])}"))
        self.check(after == before,
                   "the user's Codex config.toml is byte for byte and to the nanosecond what it was before the "
                   "run: Codex recorded no trust for the fresh checkout")

    def fresh_git_checkout(self) -> None:
        """Make the temp checkout a git repository: one no Codex has seen, so nothing has marked it trusted."""
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True, timeout=60,
                       env=self.env)

    def codex_thread_cwd(self, sid: str, thread: str) -> str | None:
        """The working directory Codex reported for the thread (its answer to thread/start or thread/resume, the
        last one), from the session's transcript on the node."""
        cwd = None
        for line in (self.node_home / "data" / "node-logs" / "sessions" / f"{sid}.jsonl").read_text().splitlines():
            try:
                result = json.loads(line).get("result")
            except ValueError:
                continue
            if isinstance(result, dict) and (result.get("thread") or {}).get("id") == thread:
                cwd = result.get("cwd")
        return cwd

    def codex_stdin(self) -> list[dict]:
        """Every message written to the Codex session processes' stdin so far, in order."""
        try:
            return [json.loads(line) for line in self.codex_stdin_log.read_text().splitlines()]
        except OSError:
            return []

    def restart_node(self) -> float:
        """SIGTERM the node, wait for it to go, start another over the same home and logs. Returns how many seconds
        the stop took."""
        proc = next(p for n, p in self.procs if n == "node")
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(40)
        took = time.monotonic() - t0
        self.procs = [(n, p) for n, p in self.procs if p is not proc]
        self.node_runs += 1
        self.spawn("node", self.node_home, "node", "--hub", f"http://127.0.0.1:{self.node_port}", "--name", NODE,
                   AGENT_COMMS_TOKEN=self.node_token)      # the same log file, appended to
        self.wait_for("the node to connect again",
                      lambda: self.log_text("node").count("connected to") >= self.node_runs)
        return took

    def stdin_lines(self) -> list[str]:
        """What was written to the session processes' stdin so far: the text of each turn, in order."""
        lines = [json.loads(line) for line in self.stdin_log.read_text().splitlines()]
        return [x["message"]["content"] for x in lines if x.get("type") == "user"]

    def check(self, condition: bool, what: str) -> None:
        if not condition:
            raise SystemExit(f"FAIL: {what}")
        print(f"  ok: {what}")

    # ------------------------------------------------------------ the scenario

    def scenario(self) -> None:
        limit = self.args.max_latency
        if self.real:
            first, second = "Reply with exactly the word: ready", "Reply with exactly the word: pong"
            long, after = ("Write a story of about 300 words about a lighthouse keeper.",
                           "Reply with exactly the word: after")
            again = "Reply with exactly the word: again"
        else:
            first, second, long, after, again = "ready", "pong", "sleep 4 then tell a long story", "after", "again"

        print("\n[1] board session start <project> <message> --wait")
        out, err, took = self.board("session", "start", str(self.project), first, "--agent", AGENT, "--model", "haiku",
                                    "--wait", "--json")
        data = json.loads(out)
        sid, m1 = data["session"]["session_id"], data["message"]
        print(f"  session {sid} on node {data['session']['node']}; wall {took:.2f}s; queued -> written "
              f"{m1['delivered_at'] - m1['created_at']:.2f}s; turn {m1['ended_at'] - m1['delivered_at']:.2f}s")
        print(f"  reply: {data['reply']!r}")
        self.check(m1["state"] == "done" and bool(data["reply"].strip()), "the first turn ended with a reply")
        self.check(m1["delivered_at"] - m1["created_at"] <= limit, f"written within {limit:g}s of the start")
        if not self.real:
            self.check(data["reply"] == "turn 1: READY", "the stand-in's reply came back unchanged")
        pid = self.show(sid)["pid"]

        print("\n[2] board session send <id> <message> --wait   (the session is idle)")
        out, err, took = self.board("session", "send", sid, second, "--wait")
        m2 = next(m for m in self.show(sid)["messages"] if m["n"] == 2)
        print(f"  wall {took:.2f}s; queued -> written {m2['delivered_at'] - m2['created_at']:.2f}s; "
              f"turn {m2['ended_at'] - m2['delivered_at']:.2f}s")
        print(f"  reply: {out.strip()!r}")
        self.check(m2["state"] == "done" and bool(out.strip()), "the turn ended and stdout is its reply")
        self.check(m2["delivered_at"] - m2["created_at"] <= limit,
                   f"a message to the idle session was written within {limit:g}s")
        if not self.real:
            self.check(out == "turn 2: PONG\n", "stdout is exactly the reply")

        print("\n[3] a long turn, then at once: board session send <id> <message> --wait   (the session is busy)")
        self.board("session", "send", sid, long)
        out, err, took = self.board("session", "send", sid, after, "--wait")
        view = self.show(sid)
        m3, m4 = (next(m for m in view["messages"] if m["n"] == n) for n in (3, 4))
        t0 = m3["created_at"]
        print(f"  long turn:   sent +0.00s, written +{m3['delivered_at'] - t0:.2f}s, ended +{m3['ended_at'] - t0:.2f}s")
        print(f"  queued send: sent +{m4['created_at'] - t0:.2f}s, written +{m4['delivered_at'] - t0:.2f}s "
              f"({m4['delivered_at'] - m3['ended_at']:.2f}s after the long turn ended), ended "
              f"+{m4['ended_at'] - t0:.2f}s; wall {took:.2f}s")
        print(f"  reply: {out.strip()!r}")
        self.check(m3["delivered_at"] - m3["created_at"] <= limit, f"the long turn started within {limit:g}s")
        self.check(m3["created_at"] < m4["created_at"] < m3["ended_at"],
                   "the second message was sent before the long turn ended, so it had to queue")
        self.check(m3["state"] == "done", "the long turn ran to its end: nothing was interrupted")
        self.check(m4["delivered_at"] >= m3["ended_at"], "the queued message was written only after the long turn ended")
        self.check(m4["delivered_at"] - m3["ended_at"] <= limit, f"... and within {limit:g}s of it")
        self.check(m4["state"] == "done" and bool(out.strip()), "the queued turn ended with a reply")
        if not self.real:
            self.check(out == "turn 4: AFTER\n", "its reply is the fourth turn's")

        tid = self.wake_scenario(sid)

        print("\n[6] one process, then close")
        view = self.show(sid)
        self.check(view["turns"] == 6 and view["pid"] == pid, f"one process (pid {pid}) took all six turns")
        self.check(len(self.stdin_lines()) == 6, "six lines were written to its stdin, one per turn")
        listed, _, _ = self.board("session", "list")
        self.check(sid in listed, "board session list shows it")
        out, _, _ = self.board("session", "close", sid)
        print(f"  {out.strip()}")
        deadline = time.monotonic() + 20
        while self.show(sid)["state"] != "ended" and time.monotonic() < deadline:
            time.sleep(0.2)
        self.check(self.show(sid)["state"] == "ended", "the node confirmed the close")
        out, err, _ = self.board("session", "send", sid, "too late", ok=False)
        self.check("has ended" in err, "a closed session takes no message")
        # The hub hears `ended` when the node has signalled the process; the node reaps it on a pass soon after.
        self.check(gone(pid), f"its process (pid {pid}) is gone")

        self.api(self.poster_token, "POST", "/api/posts",
                 {"body": f"after the close {MARKER}", "type": "request", "thread_id": tid, "to": [AGENT],
                  "session_id": self.poster_sid})
        time.sleep(1.5)
        self.check(len(self.stdin_lines()) == 6 and not any(m["n"] > 6 for m in self.show(sid)["messages"]),
                   "a post addressed to the agent after the close wakes nothing")

        print("\n[7] a new session in the same project, left open while the node is stopped")
        out, err, took = self.board("session", "start", str(self.project), again, "--agent", AGENT, "--model", "haiku",
                                    "--wait", "--json")
        data = json.loads(out)
        self.check(data["session"]["session_id"] != sid and data["message"]["state"] == "done",
                   "the project takes a new session once the old one has ended")
        print(f"  reply: {data['reply']!r}")
        self.open_pid = self.show(data["session"]["session_id"])["pid"]
        self.check(bool(self.open_pid) and alive(self.open_pid), f"its process (pid {self.open_pid}) stays up, idle")

    # ------------------------------------------------------------ a hosted Codex session

    def codex_scenario(self) -> None:
        limit, model = self.args.max_latency, self.args.codex_model
        if self.real_codex:
            first = "Remember this word for later: lantern. Now reply with exactly the word: ready"
            second, after = "Reply with exactly the word: pong", "Reply with exactly the word: after"
            long = "Write the integers from 1 to 300, one per line, and nothing else."
            recall = "Reply with exactly the word I asked you to remember at the start of this conversation."
        else:
            first, second, long, after, recall = "remember lantern", "pong", "sleep 4 then count", "after", "recall"

        print(f"\n[C1] board session start <project> <message> --agent {CODEX_AGENT} --wait   (a hosted Codex session)")
        out, err, took = self.board("session", "start", str(self.project), first, "--agent", CODEX_AGENT,
                                    "--model", model, "--wait", "--json")
        data = json.loads(out)
        sid, m1 = data["session"]["session_id"], data["message"]
        print(f"  session {sid} on node {data['session']['node']}; wall {took:.2f}s; queued -> written "
              f"{m1['delivered_at'] - m1['created_at']:.2f}s (the process start and Codex's handshake included); "
              f"turn {m1['ended_at'] - m1['delivered_at']:.2f}s")
        print(f"  reply: {data['reply']!r}")
        self.check(m1["state"] == "done" and bool(data["reply"].strip()), "the first turn ended with a reply")
        view = self.show(sid)
        pid, thread = view["pid"], view["provider_id"]
        print(f"  Codex thread {thread}, process {pid}")
        self.check(bool(thread) and thread != sid, "the hub's record holds Codex's own thread id (not the session id)")
        wrote = self.codex_stdin()
        self.check([m.get("method") for m in wrote] == ["initialize", "initialized", "thread/start", "turn/start"],
                   "stdin got the handshake first and then the one turn: initialize, initialized, thread/start, "
                   "turn/start")
        start = wrote[2]["params"]
        print(f"  thread/start: {json.dumps(start)}")
        self.check(start == {"sandbox": self.codex_sandbox, "approvalPolicy": "never", "model": model},
                   "the thread was started with approvals off, the runner's sandbox and the hub's model, and "
                   "nothing else: no cwd, no instructions")
        where = self.codex_thread_cwd(sid, thread)
        print(f"  Codex reports the thread's working directory as {where}")
        self.check(where is not None and os.path.realpath(where) == str(self.project),
                   "the thread works in the node's run directory, which is where its process was started")
        self.check(wrote[3]["params"]["threadId"] == thread
                   and wrote[3]["params"]["input"] == [{"type": "text", "text": first}],
                   "the turn went to that thread with the message as its only input")
        if self.real_codex:
            self.check("ready" in data["reply"].lower(), "the reply is the model's answer")
        else:
            self.check(data["reply"] == "turn 1: REMEMBER LANTERN",
                       "the reply is the turn's final answer (not its progress note, not a sub-agent's answer)")

        print("\n[C2] board session send <id> <message> --wait   (the session is idle)")
        out, err, took = self.board("session", "send", sid, second, "--wait")
        m2 = next(m for m in self.show(sid)["messages"] if m["n"] == 2)
        print(f"  wall {took:.2f}s; queued -> written {m2['delivered_at'] - m2['created_at']:.2f}s; "
              f"turn {m2['ended_at'] - m2['delivered_at']:.2f}s")
        print(f"  reply: {out.strip()!r}")
        self.check(m2["state"] == "done" and bool(out.strip()), "the turn ended and stdout is its reply")
        self.check(m2["delivered_at"] - m2["created_at"] <= limit,
                   f"a message to the idle session was written within {limit:g}s")
        if not self.real_codex:
            self.check(out == "turn 2: PONG\n", "stdout is exactly the reply")

        print("\n[C3] a long turn, then at once: board session send <id> <message> --wait   (the session is busy)")
        self.board("session", "send", sid, long)
        out, err, took = self.board("session", "send", sid, after, "--wait")
        view = self.show(sid)
        m3, m4 = (next(m for m in view["messages"] if m["n"] == n) for n in (3, 4))
        t0 = m3["created_at"]
        print(f"  long turn:   sent +0.00s, written +{m3['delivered_at'] - t0:.2f}s, ended +{m3['ended_at'] - t0:.2f}s")
        print(f"  queued send: sent +{m4['created_at'] - t0:.2f}s, written +{m4['delivered_at'] - t0:.2f}s "
              f"({m4['delivered_at'] - m3['ended_at']:.2f}s after the long turn ended), ended "
              f"+{m4['ended_at'] - t0:.2f}s; wall {took:.2f}s")
        print(f"  reply: {out.strip()!r}")
        self.check(m3["delivered_at"] - m3["created_at"] <= limit, f"the long turn started within {limit:g}s")
        self.check(m3["created_at"] < m4["created_at"] < m3["ended_at"],
                   "the second message was sent before the long turn ended, so it had to queue")
        self.check(m3["state"] == "done", "the long turn ran to its end: nothing was interrupted")
        self.check(m4["delivered_at"] >= m3["ended_at"], "the queued message was written only after the long turn ended")
        self.check(m4["delivered_at"] - m3["ended_at"] <= limit, f"... and within {limit:g}s of it")
        self.check(m4["state"] == "done" and bool(out.strip()), "the queued turn ended with a reply")
        self.check(view["turns"] == 4 and view["pid"] == pid and view["provider_id"] == thread,
                   f"one process (pid {pid}) and one Codex thread took all four turns")
        methods = [m.get("method") for m in self.codex_stdin()]
        self.check(methods.count("turn/start") == 4 and methods.count("thread/start") == 1
                   and "turn/interrupt" not in methods, "four turn/start lines, one thread, no interrupt")

        print("\n[C4] the node is stopped and started again; the next message goes to the same Codex thread")
        took = self.restart_node()
        self.check(gone(pid), f"the session's process (pid {pid}) went with the node ({took:.1f}s after SIGTERM)")
        view = self.show(sid)
        self.check(view["state"] == "idle" and view["pid"] is None and view["provider_id"] == thread,
                   "the session is still there, idle, with no process, and its thread id on record")
        before = len(self.codex_stdin())
        out, err, took = self.board("session", "send", sid, recall, "--wait")
        m5 = next(m for m in self.show(sid)["messages"] if m["n"] == 5)
        view = self.show(sid)
        print(f"  wall {took:.2f}s; queued -> written {m5['delivered_at'] - m5['created_at']:.2f}s (a new process and "
              f"its handshake included); turn {m5['ended_at'] - m5['delivered_at']:.2f}s; process {view['pid']}")
        print(f"  reply: {out.strip()!r}")
        wrote = self.codex_stdin()[before:]
        self.check([m.get("method") for m in wrote] == ["initialize", "initialized", "thread/resume", "turn/start"],
                   "the new process was told to resume: initialize, initialized, thread/resume, turn/start")
        self.check(wrote[2]["params"]["threadId"] == thread and wrote[3]["params"]["threadId"] == thread,
                   "... the same thread, by the id the node kept on disk")
        where = self.codex_thread_cwd(sid, thread)
        self.check(wrote[2]["params"].get("cwd") == str(self.project) and where is not None
                   and os.path.realpath(where) == str(self.project),
                   "the resume names the node's run directory, and Codex goes on there")
        self.check(view["pid"] not in (None, pid) and view["provider_id"] == thread and m5["state"] == "done",
                   "a new process took the turn in the old thread")
        if self.real_codex:
            self.check("lantern" in out.lower(), "the reply holds the word from the first turn: the conversation "
                                                 "was kept across the restart")
        else:
            self.check(out == "turn 5: FIRST WAS REMEMBER LANTERN\n",
                       "the reply counts on from turn 4 and names the first message: the conversation was kept")
        record = json.loads((self.node_home / "data" / "node-logs" / "node-session-providers.json").read_text())
        self.check(record == {sid: thread}, "node-session-providers.json on the node holds the thread id")

        print("\n[C5] close")
        pid = view["pid"]
        out, _, _ = self.board("session", "close", sid)
        print(f"  {out.strip()}")
        deadline = time.monotonic() + 20
        while self.show(sid)["state"] != "ended" and time.monotonic() < deadline:
            time.sleep(0.2)
        self.check(self.show(sid)["state"] == "ended", "the node confirmed the close")
        out, err, _ = self.board("session", "send", sid, "too late", ok=False)
        self.check("has ended" in err, "a closed session takes no message")
        self.check(gone(pid), f"its process (pid {pid}) is gone")

    # ------------------------------------------------------------ the send modes on a running turn (--only steer)

    def steer_scenario(self) -> None:
        """S1-S6 (the module text): a Claude session and a Codex one: a steer is refused with the reason; restart;
        auto."""
        self._steer_one("Claude", STEER_AGENT, self.real, ["--model", "haiku"], "S")
        self._steer_one("Codex", CODEX_AGENT, self.real_codex, ["--model", self.args.codex_model], "S")

    def _steer_one(self, label: str, agent: str, real: bool, model: list[str], tag: str) -> None:
        steps = ("1", "2", "3") if label == "Claude" else ("4", "5", "6")
        hold1, hold2 = self.tmp / f"steer-release-{agent}-1", self.tmp / f"steer-release-{agent}-2"
        if real:
            long1 = ("Run this exact shell command once, with your shell or Bash tool, and nothing else first: "
                     "`sleep 12`. When it has finished, reply with one short sentence.")
            long2 = ("Run this exact shell command once, with your shell or Bash tool, and nothing else first: "
                     "`sleep 12`. When it has finished, reply with exactly the word: done")
        else:
            long1, long2 = f"hold {hold1} for the steer", f"hold {hold2} for the restart"
        steer = "IMPORTANT UPDATE: in your final reply also include the exact word PINEAPPLE."
        steer_no = "this session's agent CLI cannot steer into a running turn (its node says so)"
        restart = "Reply with exactly the word: kiwi" if real else "kiwi"
        idle = "Reply with exactly the word: apple" if real else "apple"

        def running(sid: str, n: int) -> None:
            m = self.message(sid, n, lambda m: m["state"] == "delivered", 60)
            self.check(m is not None and m["state"] == "delivered", f"message {n}'s turn is in flight")
            deadline = time.monotonic() + 60
            while self.show(sid)["state"] != "running" and time.monotonic() < deadline:
                time.sleep(0.2)
            if real:
                time.sleep(4)       # into the command the model runs
                m = next(m for m in self.show(sid)["messages"] if m["n"] == n)
                if m["state"] != "delivered":
                    raise SystemExit(f"FAIL: the model ended the long turn within 4 s ({m['state']}: {m['detail']!r}, "
                                     f"transcript {self.show(sid)['transcript']}): nothing to send into")

        print(f"\n[{tag}{steps[0]}] {label}: a long turn, then board session send <id> <message> --mode steer")
        out, _, _ = self.board("session", "start", str(self.project), long1, "--agent", agent, *model, "--json")
        sid = json.loads(out)["session"]["session_id"]
        running(sid, 1)
        pid = self.show(sid)["pid"]
        if label == "Claude":
            # Step 2, and the off configuration (this node runs with `claude_steer = false`): Claude's adapter does
            # not steer, real CLI or stand-in; the send is refused with the reason, as on main, and nothing is made
            # for it.
            out, err, _ = self.board("session", "send", sid, steer, "--mode", "steer", ok=False)
            self.check(steer_no in err, "a steer is refused with the reason, and nothing is sent")
            self.check(len(self.show(sid)["messages"]) == 1, "no message was made for it")
            if not real:
                hold1.touch()
            m1 = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 90)
            self.check(m1 is not None and m1["state"] == "done", "the long turn ran to its end untouched")
        else:
            # Step 2: Codex takes it into that turn (`turn/steer` with expectedTurnId); no message of its own.
            out, _, _ = self.board("session", "send", sid, steer, "--mode", "steer", "--request-id", f"{tag}4",
                                   "--json")
            sent = json.loads(out)
            self.check(sent["message"] is None and sent["steer"]["into"] == 1, "taken as a steer into message 1")
            self.check(len(self.show(sid)["messages"]) == 1, "no message was made for it")
            deadline = time.monotonic() + 20
            while not any(x.get("method") == "turn/steer" for x in self.codex_stdin()) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not real:
                hold1.touch()
            rc, out, _ = self.board_rc("session", "send", sid, steer, "--mode", "steer", "--request-id", f"{tag}4",
                                       "--wait", "--json")
            data = json.loads(out) if out.strip().startswith("{") else {}
            print(f"  exit {rc}; steer {(data.get('steer') or {}).get('outcome')}; reply {data.get('reply')!r}")
            self.check(rc == 0 and (data.get("steer") or {}).get("outcome") == "in_turn"
                       and sum(x.get("method") == "turn/steer" for x in self.codex_stdin()) == 1,
                       "written once into that turn, taken in (in_turn); the turn ran to its end")
        self._restart_and_auto(label, sid, pid, real, long2, restart, idle, hold2, running, tag, steps, 2)

    def _restart_and_auto(self, label, sid, pid, real, long2, restart, idle, hold2, running, tag, steps, k) -> None:
        """A long turn (message k) restarted by message k+1; then `auto` on the idle session (message k+2)."""
        limit = self.args.max_latency
        print(f"\n[{tag}{steps[1]}] {label}: a long turn, then --mode restart")
        self.board("session", "send", sid, long2)
        running(sid, k)
        out, err, took = self.board("session", "send", sid, restart, "--mode", "restart", "--wait", "--json")
        data = json.loads(out)
        print(f"  reply: {data['reply']!r}; wall {took:.2f}s")
        view = self.show(sid)
        old, new = (next(m for m in view["messages"] if m["n"] == n) for n in (k, k + 1))
        self.check(old["state"] == "failed" and old["detail"].startswith(f"interrupted by a restart: message {k + 1}")
                   and old.get("restarted_by") == k + 1, "the long turn was interrupted by the restart, and says so")
        self.check(new.get("replaced") == k and new["delivered_at"] >= old["ended_at"],
                   "the restart's message was written only after the interrupted turn had ended")
        self.check(new["state"] == "done" and "kiwi" in (data["reply"] or "").lower(),
                   "the restart's message was answered")
        self.check(view["pid"] == pid, f"in-band: the same process (pid {pid}) took the new turn")
        if not real:
            hold2.touch()

        print(f"\n[{tag}{steps[2]}] {label}: board session send <id> <message> --mode auto (the session is idle)")
        out, err, took = self.board("session", "send", sid, idle, "--mode", "auto", "--wait", "--json")
        data = json.loads(out)
        last = next(m for m in self.show(sid)["messages"] if m["n"] == k + 2)
        print(f"  reply: {data['reply']!r}; queued -> written {last['delivered_at'] - last['created_at']:.2f}s")
        self.check("mode" not in last and last["state"] == "done" and "apple" in (data["reply"] or "").lower(),
                   "auto on an idle session started a turn of its own")
        self.check(data["message"].get("mode") is None, "auto never restarts")
        self.check(last["delivered_at"] - last["created_at"] <= limit, f"written within {limit:g}s")
        self.board("session", "close", sid)
        deadline = time.monotonic() + 20
        while self.show(sid)["state"] != "ended" and time.monotonic() < deadline:
            time.sleep(0.2)
        self.check(self.show(sid)["state"] == "ended" and gone(pid), "closed, and its process is gone")

    # ------------------------------------------------------------ mid-turn steering, step 2 (--only steering)

    STEER_TEXT = "IMPORTANT UPDATE (Q): in your final reply also include the exact word PINEAPPLE."
    LONG = ("Run this exact shell command once, with your shell tool, and nothing else first: `sleep 12`. When it has "
            "finished, reply with one short sentence.")

    def _running(self, sid: str, n: int, real: bool) -> None:
        m = self.message(sid, n, lambda m: m["state"] == "delivered", 90)
        self.check(m is not None and m["state"] == "delivered", f"message {n}'s turn is in flight")
        deadline = time.monotonic() + 60
        while self.show(sid)["state"] != "running" and time.monotonic() < deadline:
            time.sleep(0.2)
        if real:
            time.sleep(5)       # into the command the model runs
            m = next(m for m in self.show(sid)["messages"] if m["n"] == n)
            if m["state"] != "delivered":
                raise SystemExit(f"FAIL: the model ended the long turn within 5 s ({m['state']}: {m['detail']!r}): "
                                 "nothing to steer into")

    def _frames(self, sid: str) -> list[dict]:
        try:
            return self.transcript(sid)
        except OSError:
            return []

    def _m5_proof(self, sid: str, rid: int, client_id: str | None, seconds: float = 60) -> tuple[str | None, dict, dict]:
        """M5 from the session's transcript: (T, the answer to the turn/start request `rid`, the userMessage item
        whose clientId is `client_id`), waiting for both."""
        deadline = time.monotonic() + seconds
        answer = item = {}
        while time.monotonic() < deadline:
            frames = self._frames(sid)
            answer = next((f for f in frames if f.get("id") == rid and isinstance(f.get("result"), dict)), {})
            item = next((f for f in frames if f.get("method") in ("item/started", "item/completed")
                         and ((f.get("params") or {}).get("item") or {}).get("type") == "userMessage"
                         and client_id is not None
                         and ((f.get("params") or {}).get("item") or {}).get("clientId") == client_id), {})
            if answer and item:
                break
            time.sleep(0.2)
        t = ((answer.get("result") or {}).get("turn") or {}).get("id")
        return t, answer, item

    def _steer_lines(self, text: str) -> list[dict]:
        return [x for x in self.codex_stdin() if x.get("method") == "turn/steer"
                and text in json.dumps((x.get("params") or {}).get("input"))]

    def _wait_steer_line(self, text: str, seconds: float = 20) -> dict | None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            got = self._steer_lines(text)
            if got:
                return got[-1]
            time.sleep(0.1)
        return None

    def _close(self, sid: str) -> None:
        self.board("session", "close", sid)
        self.check(self.wait_ended(sid, 40)["state"] == "ended", "closed")

    def _codex_root(self, text: str, model: list[str]) -> tuple[str, int, str | None]:
        """A Codex root started with `text` (not waited for): (its id, its turn/start request id, the clientId)."""
        before = len(self.codex_stdin())
        out, _, _ = self.board("session", "start", str(self.project), text, "--agent", CODEX_AGENT, *model, "--json")
        sid = json.loads(out)["session"]["session_id"]
        deadline = time.monotonic() + 60
        start = None
        while start is None and time.monotonic() < deadline:
            start = next((x for x in self.codex_stdin()[before:] if x.get("method") == "turn/start"), None)
            time.sleep(0.1)
        if start is None:
            raise SystemExit("FAIL: no turn/start was written for the Codex session")
        return sid, start["id"], (start.get("params") or {}).get("clientUserMessageId")

    # ------------------------------------------------------------ Claude steering, step 3 (--only steering)

    Z_STEER = "Change of plan from me: include the codeword PINEAPPLE in your reply."

    def _claude_root(self, text: str, model: list[str]) -> str:
        out, _, _ = self.board("session", "start", str(self.project), text, "--agent", STEER_AGENT, *model, "--json")
        return json.loads(out)["session"]["session_id"]

    def _z_line(self, text: str) -> dict | None:
        return next((x for x in reversed(self.written_lines()) if x["message"]["content"] == text), None)

    def _z_steer(self, sid: str, rid: str, text: str) -> tuple[int, dict, str, dict | None]:
        out, _, _ = self.board("session", "send", sid, text, "--mode", "steer", "--request-id", rid, "--json")
        sent = json.loads(out)
        self.check(sent["message"] is None and sent["steer"]["into"] == 1 and sent["steer"]["state"] == "accepted",
                   "the steer is accepted as a steer into message 1")
        deadline, line = time.monotonic() + 20, None
        while line is None and time.monotonic() < deadline:
            line = self._z_line(text)
            time.sleep(0.05)
        return 0, sent, out, line

    def _z_wait(self, sid: str, rid: str, text: str) -> tuple[int, dict, str]:
        rc, out, err = self.board_rc("session", "send", sid, text, "--mode", "steer", "--request-id", rid, "--wait",
                                     "--json")
        data = json.loads(out) if out.strip().startswith("{") else {}
        return rc, data, err

    def claude_steering_scenario(self) -> None:
        """Z1-Z3 (the module text)."""
        real = self.real
        model = ["--model", "haiku"]
        [version] = sorted(self.node_verified)
        print(f"\n[Z0] a hub and a node of their own; the node runs with {version} in VERIFIED and STEER_MODES "
              f"({self.node_steer_modes[version]}), that node process only; the Claude agent is "
              + (f"the real CLI ({self.args.claude_bin})" if real else "the stand-in"))

        print("\n[Z1] a human steer into a Claude tool turn: board session send <id> <text> --mode steer --wait")
        hold = self.tmp / "z1-release"
        n_text = ("Run this exact shell command once, with your Bash tool, and nothing else first: `sleep 10`. When it "
                  "has finished, reply with one line: DONE followed by any codeword you were given.") if real \
            else f"hold {hold} z1"
        sid = self._claude_root(n_text, model)
        self._running(sid, 1, real)
        _, _, _, line = self._z_steer(sid, "z1", self.Z_STEER)
        n_line = self._z_line(n_text)
        print(f"  written: {json.dumps(line)[:240]}")
        self.check(line is not None and isinstance(line.get("uuid"), str) and line["uuid"] != n_line.get("uuid"),
                   "S was written once, as a user line with its own uuid")
        if not real:
            hold.touch()
        rc, data, err = self._z_wait(sid, "z1", self.Z_STEER)
        steer = data.get("steer") or {}
        frames = self.transcript(sid)
        n_res = next((f for f in frames if f.get("type") == "result"
                      and n_line.get("uuid") in (f.get("user_message_uuids") or [])), {})
        print(f"  exit {rc}; steer {steer.get('outcome')} ({steer.get('why')}); N's result echoes "
              f"{n_res.get('user_message_uuids')}")
        if real and steer.get("outcome") != "in_turn":
            print("  (Claude put S elsewhere: the real CLI decides alone; Z2 measures the other path)")
        else:
            self.check(rc == 0 and steer.get("outcome") == "in_turn"
                       and line["uuid"] in (n_res.get("user_message_uuids") or []),
                       "in_turn: N's result lists N's uuid and S's; exit 0")
        self.check(sum(1 for x in self.written_lines() if x["message"]["content"] == self.Z_STEER) == 1,
                   "the retry with --wait sent nothing again")
        self._close(sid)

        print("\n[Z2] a human steer into a text-only turn: N's end is held; S runs as its own turn (next_turn)")
        z2 = self.Z_STEER + " z2"
        n_text = ("Write a story of about 500 words about a lighthouse keeper, then a last line: DONE followed by any "
                  "codeword you were given. Use no tools.") if real else "think 4 z2"
        s_text = z2 if real else "sleep 3 " + z2
        sid = self._claude_root(n_text, model)
        m = self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
        self.check(m is not None, "message 1's turn is in flight")
        time.sleep(1.0 if real else 0.3)
        _, _, _, line = self._z_steer(sid, "z2", s_text)
        n_line = self._z_line(n_text)
        if not real:
            # while S's own turn runs (3 s), N is still in flight on the hub, with its checkpoint
            deadline, held = time.monotonic() + 20, None
            while held is None and time.monotonic() < deadline:
                held = self.held_of(sid, 1)
                time.sleep(0.2)
            self.check(held is not None and self.show(sid)["messages"][0]["state"] == "delivered",
                       "N's native end is checkpointed (`held`) while N stays in flight")
        rc, data, err = self._z_wait(sid, "z2", s_text)
        steer = data.get("steer") or {}
        frames = self.transcript(sid)
        n_res = next((f for f in frames if f.get("type") == "result"
                      and n_line.get("uuid") in (f.get("user_message_uuids") or [])), {})
        s_init = next((f for f in frames if f.get("subtype") == "init"
                       and line and (f.get("user_message_uuids") or []) == [line["uuid"]]), {})
        s_res = next((f for f in frames if f.get("type") == "result"
                      and line and (f.get("user_message_uuids") or []) == [line["uuid"]]), {})
        print("  M8 raw evidence through the node:")
        print(f"    N's result: uuids {n_res.get('user_message_uuids')} origin {n_res.get('origin')} "
              f"num_turns {n_res.get('num_turns')}")
        print(f"    S's init:   uuids {s_init.get('user_message_uuids')} origin {s_init.get('origin')}")
        print(f"    S's result: uuids {s_res.get('user_message_uuids')} origin {s_res.get('origin')} "
              f"num_turns {s_res.get('num_turns')}")
        own = steer.get("result") or {}
        print(f"  exit {rc}; steer {steer.get('outcome')} ({steer.get('why')}); N's reply {(data.get('reply') or '')[-60:]!r}; "
              f"S's reply {(own.get('reply') or '')[-60:]!r}")
        self.check(rc == 0 and steer.get("outcome") == "next_turn" and own.get("ok") is True and s_init and s_res
                   and (n_res.get("user_message_uuids") or []) == [n_line.get("uuid")],
                   "next_turn: N's result lists only N; an init echoing exactly [S] opened S's own turn; N settled "
                   "once with S's own reply; exit 0")
        self.check(self.show(sid)["reply"] == data.get("reply"), "the session's reply stays N's")
        self.check(self.held_of(sid, 1) is None, "the checkpoint is cleared with N's settlement")
        self._close(sid)

        for i in range(1, 4 if real else 2):
            self._z10_resumed(model, i)

        if not real:
            print("\n[Z3] a close while S's own turn runs: N's native end, next_turn with S cut, then the end")
            sid = self._claude_root("think 2 z3", model)
            self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
            time.sleep(0.3)
            self._z_steer(sid, "z3", "sleep 30 " + self.Z_STEER + " z3")
            deadline = time.monotonic() + 20
            while self.held_of(sid, 1) is None and time.monotonic() < deadline:
                time.sleep(0.2)
            time.sleep(1.0)
            self._close(sid)
            st = self.steer_of(sid, 1)
            m = self.show(sid)["messages"][0]
            print(f"  N {m['state']} ({m.get('detail')!r}); steer {st.get('outcome')} ({st.get('why')}); "
                  f"S's result {(st.get('result') or {}).get('detail')!r}")
            self.check(m["state"] == "done" and st.get("outcome") == "next_turn"
                       and str((st.get("result") or {}).get("detail")).startswith("its own turn was cut"),
                       "N's native end, then next_turn with S failed: its own turn was cut")
            self._z5_parent(model)
            self._z6_restart(model)
            self._z8_bound(model)
            self._z9_cli_turn(model)
            self._z12_crash(model)
            if self.args.only == "steering":
                self._z7_partition(model)
                self._z11_partition_without_checkpoint(model)

    def crash_node(self) -> None:
        """SIGKILL the node (no shutdown, nothing reported), then start another over the same home and logs."""
        proc = next(p for n, p in self.procs if n == "node")
        proc.send_signal(signal.SIGKILL)
        proc.wait(40)
        self.procs = [(n, p) for n, p in self.procs if p is not proc]
        self.node_runs += 1
        self.spawn("node", self.node_home, "node", "--hub", f"http://127.0.0.1:{self.node_port}", "--name", NODE,
                   AGENT_COMMS_TOKEN=self.node_token)
        self.wait_for("the node to connect again",
                      lambda: self.log_text("node").count("connected to") >= self.node_runs)

    def _z12_crash(self, model: list[str]) -> None:
        print("\n[Z12] the node crashes (SIGKILL) while N is held with its checkpoint committed")
        sid = self._claude_root("think 2 z12", model)
        self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
        time.sleep(0.3)
        text = "sleep 60 steer before the crash z12"
        self._z_steer(sid, "z12", text)
        held = self._z_held(sid)
        self.check(held is not None and held.get("text") == "turn 1: THINK 2 Z12", "N's checkpoint is committed")
        self.crash_node()
        m = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 120)
        st = self.steer_of(sid, 1)
        print(f"  N {m and m['state']} {m and m.get('detail')!r}; steer {st.get('outcome')} ({st.get('why')})")
        self.check(m is not None and m["state"] == "done" and self.show(sid)["reply"] == "turn 1: THINK 2 Z12"
                   and st.get("outcome") == "unconfirmed",
                   "the restarted node's LOST settles N by its checkpoint (N's own reply); S unconfirmed")
        out, _, _ = self.board("session", "send", sid, "after the crash", "--wait")
        self.check("AFTER THE CRASH" in out and len(self._z_framed(text)) == 1,
                   "the session goes on in a new process, and S was never written again")
        self._close(sid)

    def _z11_partition_without_checkpoint(self, model: list[str]) -> None:
        print("\n[Z11] (--only steering) a partition with no committed checkpoint: the hub stopped for 82 s from "
              "before N's end")
        sid = self._claude_root("think 4 z11", model)
        self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
        time.sleep(0.3)
        self._z_steer(sid, "z11", "sleep 150 steer across a partition z11")
        hub = next(p for n, p in self.procs if n == "hub")
        hub.send_signal(signal.SIGSTOP)
        try:
            time.sleep(82)
        finally:
            hub.send_signal(signal.SIGCONT)
        self._z_cut(sid, "contact", "N's native end from the node's own report; next_turn with S cut: its contact")
        self.check(self.held_of(sid, 1) is None, "no checkpoint was ever committed")
        self._close(sid)

    def _z10_resumed(self, model: list[str], i: int) -> None:
        real = self.real
        print(f"\n[Z10.{i}] M8 in a resumed process: the node restarts; a steer into the resumed process's turn")
        first = "Reply with exactly the word: ready" if real else "ready"
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", STEER_AGENT, *model,
                               "--wait", "--json")
        sid = json.loads(out)["session"]["session_id"]
        resumes = self.resumes(sid)
        self.restart_node()
        n_text = ("Write a story of about 500 words about a lighthouse keeper, then a last line: DONE followed by any "
                  f"codeword you were given. Use no tools. (z10 {i})") if real else f"think 4 z10 {i}"
        s_text = f"{self.Z_STEER} z10 {i}" if real else f"sleep 2 steer into a resumed process z10 {i}"
        self.board("session", "send", sid, n_text)
        m = self.message(sid, 2, lambda m: m["state"] == "delivered", 90)
        self.check(m is not None and self.resumes(sid) == resumes + 1,
                   "message 2's turn is in flight in ONE new process, started with --resume")
        time.sleep(1.5 if real else 0.3)
        out, _, _ = self.board("session", "send", sid, s_text, "--mode", "steer", "--request-id", f"z10-{i}", "--json")
        sent = json.loads(out)
        self.check(sent["message"] is None and sent["steer"]["into"] == 2, "the steer is accepted into message 2")
        rc, data, err = self._z_wait(sid, f"z10-{i}", s_text)
        steer = data.get("steer") or {}
        n_line, s_line = self._z_line(n_text) or {}, self._z_line(s_text) or {}
        n_u, s_u = n_line.get("uuid"), s_line.get("uuid")
        frames = self.transcript(sid)
        n_res = next((f for f in frames if f.get("type") == "result" and n_u in (f.get("user_message_uuids") or [])),
                     {})
        s_init = next((f for f in frames if f.get("subtype") == "init" and (f.get("user_message_uuids") or []) == [s_u]),
                      {})
        s_res = next((f for f in frames if f.get("type") == "result" and (f.get("user_message_uuids") or []) == [s_u]),
                     {})
        print("  M8 raw evidence through the node (resumed process):")
        print(f"    N's result: uuids {self._z_names(n_res.get('user_message_uuids'), n_u, s_u)} origin "
              f"{n_res.get('origin')} num_turns {n_res.get('num_turns')}")
        print(f"    S's init:   uuids {self._z_names(s_init.get('user_message_uuids'), n_u, s_u)} origin "
              f"{s_init.get('origin')}")
        print(f"    S's result: uuids {self._z_names(s_res.get('user_message_uuids'), n_u, s_u)} origin "
              f"{s_res.get('origin')} num_turns {s_res.get('num_turns')}")
        own = steer.get("result") or {}
        print(f"  exit {rc}; steer {steer.get('outcome')} ({steer.get('why')}); S's reply "
              f"{(own.get('reply') or '')[-60:]!r}")
        if steer.get("outcome") == "in_turn":
            self.check(rc == 0 and (n_res.get("user_message_uuids") or []) == [n_u, s_u],
                       "in_turn: N's result lists exactly [N, S]; exit 0")
        else:
            self.check(rc == 0 and steer.get("outcome") == "next_turn" and own.get("ok") is True and s_init and s_res
                       and (n_res.get("user_message_uuids") or []) == [n_u] and s_init.get("origin") is None,
                       "next_turn: N's result lists exactly [N]; an init listing exactly [S] (no origin) opened S's "
                       "own turn and a result listing exactly [S] ended it; exit 0")
        self.check(self.held_of(sid, 2) is None, "no checkpoint left")
        keep = os.environ.get("E2E_KEEP_FRAMES")
        if keep:
            start = next((k for k, f in enumerate(frames) if f.get("type") == "command_lifecycle"
                          and f.get("command_uuid") == n_u), 0)
            Path(keep).mkdir(parents=True, exist_ok=True)
            (Path(keep) / f"resumed-{i}.json").write_text(json.dumps(
                {"N": n_u, "S": s_u, "frames": frames[start:], "outcome": steer}, indent=1))
        self._close(sid)

    @staticmethod
    def _z_names(uuids, n_u, s_u):
        return None if uuids is None else ["N" if u == n_u else "S" if u == s_u else "other" for u in uuids]

    def _z_framed(self, text: str) -> list[dict]:
        """Every user line written to a Claude process whose text contains `text` (a parent's steer is framed)."""
        return [x for x in self.written_lines() if text in str(x["message"]["content"])]

    def _z_held(self, sid: str, seconds: float = 20) -> dict | None:
        deadline, held = time.monotonic() + seconds, None
        while held is None and time.monotonic() < deadline:
            held = self.held_of(sid, 1)
            time.sleep(0.2)
        return held

    def _z_cut(self, sid: str, words: str, what: str) -> None:
        """N's native end with `next_turn`, S failed as cut (`words` in its detail): N done, its reply N's."""
        m = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 120)
        st = self.steer_of(sid, 1)
        own = st.get("result") or {}
        print(f"  N {m and m['state']}; steer {st.get('outcome')} ({st.get('why')}); S's result ok {own.get('ok')} "
              f"{own.get('detail')!r}")
        self.check(m is not None and m["state"] == "done" and st.get("outcome") == "next_turn"
                   and own.get("ok") is False and str(own.get("detail")).startswith("its own turn was cut")
                   and words in str(own.get("detail")), what)

    def _z5_parent(self, model: list[str]) -> None:
        print("\n[Z5] a parent steers its Claude child with board_session_send(mode=steer, wait=true): next_turn")
        out, _, _ = self.board("session", "start", str(self.project), "ready", "--agent", SPAWN_AGENT, "--model",
                               "haiku", "--wait", "--json")
        rid = json.loads(out)["session"]["session_id"]
        self.board("session", "grant", str(self.project), "--agent", SPAWN_AGENT, "--access", "workspace",
                   "--hours", "1")
        busy = self.tmp / "z5-parent"
        # The parent is in a turn of its own meanwhile (as a parent that waits is): the child's result waits for it.
        self.board("session", "send", rid, f"hold {busy} z5 parent")
        self._running(rid, 2, False)
        kids = []
        for name, steer, ok in (("z5", "plum please z5", True), ("z5b", "fail-own z5b", False)):
            [made] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
                "task": f"think 6 {name}", "child_agent": STEER_AGENT, "access": "read-only"}]], rid)
            if made["error"]:
                raise SystemExit(f"FAIL: the Claude child was refused: {made['text'][:600]}")
            kid = json.loads(made["text"])["child"]["id"]
            kids.append(kid)
            self.message(kid, 1, lambda m: m["state"] == "delivered", 90)
            time.sleep(0.5)
            got: list = []
            th = threading.Thread(target=lambda: got.append(self.mcp_calls(SPAWN_AGENT, [["board_session_send", {
                "child": kid, "message": steer, "mode": "steer", "wait": True}]], rid)))
            th.start()
            th.join(120)
            [sent] = got[0] if got else [{"error": True, "text": "no answer"}]
            answer = json.loads(sent["text"]) if not sent["error"] else {}
            facts = (answer.get("result") or {}).get("result") or {}
            text = (answer.get("result") or {}).get("text") or ""
            st = facts.get("steer") or {}
            print(f"  {name}: status {answer.get('steer_status')}; result steer {st}"
                  + (f"; ERROR {sent['text'][:600]}" if sent["error"] else ""))
            lines = self._z_framed(steer)
            self.check(len(lines) == 1 and "A message sent while your turn was running" in json.dumps(lines[0]),
                       f"{name}: the parent's text went once, in the follow-up frame that says what it is")
            self.check((answer.get("steer_status") or {}).get("outcome") == "next_turn"
                       and st.get("outcome") == "next_turn" and st.get("ok") is ok,
                       f"{name}: wait=true took N's result with the steer's outcome next_turn, S's own turn "
                       + ("succeeded" if ok else "failed"))
            head, _, tail = text.partition("\nYour steer (sent while this turn ran) ran as a turn of its own after "
                                           "it; its reply follows.\n<<<child-output ")
            self.check(f"THINK 6 {name.upper()}" in head and "<<<end child-output " in head and tail
                       and ("PLUM PLEASE Z5" in tail if ok else "FAIL-OWN Z5B" in tail),
                       f"{name}: N's reply in its block; the hub's line; S's own reply in a block of its own after it")
            if not ok:
                self.check("detail" in st and "The steer's own turn failed: " in head,
                           f"{name}: S's failure detail is kept, in the facts and in the text")
        busy.touch()
        self.board("session", "close", rid)
        self.check(all(self.wait_ended(x, 40)["state"] == "ended" for x in [rid, *kids]), "closed, with its children")

    def _z6_restart(self, model: list[str]) -> None:
        print("\n[Z6] the node restarts while N is held and S's own turn runs")
        sid = self._claude_root("think 2 z6", model)
        self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
        time.sleep(0.3)
        text = "sleep 60 steer before the restart z6"
        self._z_steer(sid, "z6", text)
        self.check(self._z_held(sid) is not None, "N is held (its checkpoint on the hub)")
        time.sleep(1.0)
        self.restart_node()
        self._z_cut(sid, "", "N's native end; next_turn with S cut at the node's stop")
        out, _, _ = self.board("session", "send", sid, "after the restart", "--wait")
        self.check("AFTER THE RESTART" in out and len(self._z_framed(text)) == 1,
                   "the session goes on in a new process, and S was never written again")
        self._close(sid)

    def _z8_bound(self, model: list[str]) -> None:
        print("\n[Z8] the bound: S's own turn outlasts a 12 s turn timeout (this node run only)")
        conf = self.node_home / "board.toml"
        before = conf.read_text()
        conf.write_text(before.replace("[dispatch]\n", "[dispatch]\ntimeout_minutes = 0.2\n", 1))
        try:
            self.restart_node()
            sid = self._claude_root("think 2 z8", model)
            self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
            time.sleep(0.3)
            self._z_steer(sid, "z8", "sleep 90 steer past the bound z8")
            self.check(self._z_held(sid) is not None, "N is held")
            t0 = time.monotonic()
            self._z_cut(sid, "timed out", "N's native end; next_turn with S cut by the turn timeout")
            took = time.monotonic() - t0
            print(f"  N settled {took:.1f} s after its checkpoint was seen")
            self.check(took <= 2 * 12 + 2 * 5 + 5, "within the bound: two turn timeouts plus 2 x kill_grace")
            self._close(sid)
        finally:
            conf.write_text(before)
            self.restart_node()

    def _z9_cli_turn(self, model: list[str]) -> None:
        print("\n[Z9] a turn the CLI starts itself takes S in: unconfirmed (consumed)")
        sid = self._claude_root("think 2 bg 0.3 1 take z9", model)
        self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
        time.sleep(0.3)
        text = "steer taken by the cli z9"
        self._z_steer(sid, "z9", text)
        rc, data, err = self._z_wait(sid, "z9", text)
        steer = data.get("steer") or {}
        frames = self.transcript(sid)
        took = [f for f in frames if f.get("type") == "result" and (f.get("origin") or {}).get("kind")
                == "task-notification" and "STEER TAKEN BY THE CLI Z9" in str(f.get("result"))]
        print(f"  exit {rc}; steer {steer.get('outcome')} ({steer.get('why')}); N's reply {data.get('reply')!r}")
        self.check(took and rc == 6 and (steer.get("outcome"), steer.get("why")) == ("unconfirmed", "consumed")
                   and "THINK 2 BG" in str(data.get("reply")),
                   "the CLI's own turn took S: unconfirmed (consumed), exit 6; N's reply as it was")
        self._close(sid)

    def _z7_partition(self, model: list[str]) -> None:
        print("\n[Z7] (--only steering) a partition while S's own turn runs: the hub is stopped for 82 s")
        sid = self._claude_root("think 2 z7", model)
        self.message(sid, 1, lambda m: m["state"] == "delivered", 90)
        time.sleep(0.3)
        self._z_steer(sid, "z7", "sleep 150 steer across a partition z7")
        self.check(self._z_held(sid) is not None, "N is held")
        time.sleep(1.0)
        hub = next(p for n, p in self.procs if n == "hub")
        hub.send_signal(signal.SIGSTOP)
        try:
            time.sleep(82)
        finally:
            hub.send_signal(signal.SIGCONT)
        self._z_cut(sid, "contact", "N's native end; next_turn with S cut: the attempt's contact ran out")
        self._close(sid)

    def hub_rows(self, key: str) -> list:
        """One board_state value of the hub's database, read by another process (read only)."""
        code = ("import json, sys\nfrom agent_comms.config import Settings\nfrom agent_comms.core import Board\n"
                "b = Board(Settings.load())\n"
                "rows = [json.loads(v) for (v,) in b.conn.execute('SELECT value FROM board_state WHERE key = ?', "
                "(sys.argv[1],))]\nprint(json.dumps(rows))")
        r = subprocess.run([sys.executable, "-c", code, key], cwd=str(self.tmp), capture_output=True, text=True,
                           timeout=60, env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return json.loads(r.stdout)

    def held_of(self, sid: str, n: int) -> dict | None:
        """N's checkpoint as the hub keeps it (its database, read only)."""
        rows = self.hub_rows(f"host.msg.{sid}.{n}")
        m = rows[0] if rows else {}
        return m.get("held") if isinstance(m, dict) else None

    def steer_of(self, sid: str, n: int) -> dict:
        rows = self.hub_rows(f"host.steer.{sid}.{n}")
        return rows[0] if rows else {}

    def steering_scenario(self) -> None:
        """Q1-Q7 (the module text)."""
        real = self.real_codex
        model = ["--model", self.args.codex_model]
        print("\n[Q0] a hub and a node of their own, child sessions on; the Codex agent is "
              + (f"the real CLI ({self.args.codex_bin})" if real else "the stand-in"))
        self._q1_fresh(real, model)
        self._q2_join(real, model)
        self._q3_refusals(real, model)
        self._q4_parent(real, model)
        if not real:
            self._q5_answers(model)
            self._q6_node_restart(model)
            if self.args.only == "steering":
                self._q7_partition(model)

    def _q1_fresh(self, real: bool, model: list[str]) -> None:
        print("\n[Q1] M5 on a fresh start, then a human steer: board session send <id> <text> --mode steer --wait")
        hold = self.tmp / "q1-release"
        sid, rid, client_id = self._codex_root(self.LONG if real else f"hold {hold} q1", model)
        self._running(sid, 1, real)
        t, answer, item = self._m5_proof(sid, rid, client_id)
        print(f"  M5 raw evidence (fresh start): turn/start request {rid} clientUserMessageId {client_id}")
        print(f"    answer: {json.dumps(answer)[:400]}")
        print(f"    item:   {json.dumps(item)[:500]}")
        self.check(t is not None and item and (item.get("params") or {}).get("turnId") == t
                   and (item.get("params") or {}).get("threadId") == self.show(sid).get("provider_id"),
                   "M5 (fresh start): the answer to N's turn/start names T, and a userMessage item with N's clientId "
                   "came in T")
        out, _, _ = self.board("session", "send", sid, self.STEER_TEXT, "--mode", "steer", "--request-id", "q1",
                               "--json")
        sent = json.loads(out)
        self.check(sent["message"] is None and sent["steer"]["into"] == 1 and sent["steer"]["state"] == "accepted",
                   "the steer is accepted as a steer into message 1, with no message of its own")
        line = self._wait_steer_line(self.STEER_TEXT)
        print(f"  written: {json.dumps(line)[:300]}")
        self.check(line is not None and line["params"].get("expectedTurnId") == t,
                   "turn/steer was written once, with expectedTurnId T (the proved turn)")
        if not real:
            hold.touch()
        rc, out, err = self.board_rc("session", "send", sid, self.STEER_TEXT, "--mode", "steer", "--request-id", "q1",
                                     "--wait", "--json")
        data = json.loads(out) if out.strip().startswith("{") else {}
        steer = data.get("steer") or {}
        print(f"  exit {rc}; steer {steer.get('outcome')} ({steer.get('why')}); reply {data.get('reply')!r}")
        frames = self._frames(sid)
        ans = next((f for f in frames if line and f.get("id") == line["id"]), None)
        end = next((i for i, f in enumerate(frames) if f.get("method") == "turn/completed"
                    and ((f.get("params") or {}).get("turn") or {}).get("id") == t), None)
        print(f"    its answer: {json.dumps(ans)[:300]}")
        self.check(rc == 0 and steer.get("outcome") == "in_turn" and ans is not None
                   and (ans.get("result") or {}).get("turnId") == t and end is not None and frames.index(ans) < end,
                   "in_turn: Codex answered that very turn/steer naming T before T's turn/completed; exit 0")
        self.check(len(self._steer_lines(self.STEER_TEXT)) == 1, "the retry with --wait sent nothing again")
        if real:
            print(f"  the reply {'says' if 'pineapple' in (data.get('reply') or '').lower() else 'does not say'} "
                  "PINEAPPLE (in_turn is evidence that Codex admitted it, not of what the model did)")
        else:
            self.check("STEERED: " + self.STEER_TEXT.upper() in (data.get("reply") or ""),
                       "the stand-in read the steer in that turn")
        self._close(sid)

    def _q2_join(self, real: bool, model: list[str]) -> None:
        print("\n[Q2] M5 on a join: a turn/start while the thread runs a turn is answered with THAT turn")
        if real:
            self._m5_join_probe()
            return
        hold = self.tmp / "q2-release"
        sid, rid, client_id = self._codex_root(f"hold {hold} join-own q2", model)
        self._running(sid, 1, real)
        t, answer, item = self._m5_proof(sid, rid, client_id)
        frames = self._frames(sid)
        began = next((i for i, f in enumerate(frames) if f.get("method") == "turn/started"
                      and ((f.get("params") or {}).get("turn") or {}).get("id") == t), None)
        self.check(t is not None and began is not None and began < frames.index(answer)
                   and (item.get("params") or {}).get("turnId") == t,
                   "the turn began before the answer, which named it; N's clientId item came in it: proved")
        text = "steer into the joined turn"
        self.board("session", "send", sid, text, "--mode", "steer", "--request-id", "q2")
        line = self._wait_steer_line(text)
        self.check(line is not None and line["params"]["expectedTurnId"] == t, "the steer went into the joined turn")
        hold.touch()
        rc, out, _ = self.board_rc("session", "send", sid, text, "--mode", "steer", "--request-id", "q2",
                                   "--wait", "--json")
        self.check(rc == 0 and json.loads(out)["steer"]["outcome"] == "in_turn", "in_turn; exit 0")
        self._close(sid)

    def _m5_join_probe(self) -> None:
        """The real CLI, its own app-server in a fresh directory of this run, as the node starts it (plugins and apps
        off, every configured MCP server off, read-only sandbox, approvals never): a long turn A, then a turn/start J
        while A runs (a join), then a turn/steer into A. Raw lines kept and printed."""
        work = self.tmp / "m5-join"
        work.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True, timeout=60, env=self.env)
        argv = [self.args.codex_bin, "app-server", "--disable", "plugins", "--disable", "apps", *self.codex_quiet(),
                "-c", 'model_reasoning_effort="low"', "--listen", "stdio://"]
        proc = subprocess.Popen(argv, cwd=str(work), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=open(self.tmp / "logs" / "m5-join.stderr", "w"), env=self.codex_env())
        self.pids.add(proc.pid)
        lines: list[dict] = []
        raw = open(self.tmp / "logs" / "m5-join.jsonl", "w")

        def send(msg: dict) -> None:
            raw.write(">> " + json.dumps(msg) + "\n")
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            proc.stdin.flush()

        reader = LineReader(proc.stdout.fileno())

        def until(pred, seconds=None) -> dict:
            # (`m5_seconds`: a test's shorter limit; the probe's own is 120 s)
            deadline = time.monotonic() + (seconds if seconds is not None else getattr(self, "m5_seconds", 120.0))
            while time.monotonic() < deadline:
                try:
                    line = reader.next(deadline)     # never a blocking read past the deadline (R2-12)
                except EOFError:
                    raise SystemExit("FAIL: the Codex app-server of the M5 probe exited") from None
                if line is None:
                    break
                raw.write(line.decode())
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                lines.append(m)
                if isinstance(m, dict) and "id" in m and "method" in m:
                    send({"id": m["id"], "error": {"code": -32601, "message": "no approval surface"}})
                if pred(m):
                    return m
            raise SystemExit("FAIL: the M5 probe timed out")
        try:
            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-comms", "version": "0.1.0"},
                                                               "capabilities": {}}})
            until(lambda m: m.get("id") == 1)
            send({"method": "initialized", "params": {}})
            send({"id": 2, "method": "thread/start", "params": {"sandbox": "read-only", "approvalPolicy": "never",
                                                                 "model": self.args.codex_model}})
            tid = until(lambda m: m.get("id") == 2)["result"]["thread"]["id"]
            a, j = str(uuid.uuid4()), str(uuid.uuid4())
            send({"id": 3, "method": "turn/start", "params": {"threadId": tid, "clientUserMessageId": a, "input": [
                {"type": "text", "text": "Run the shell command `sleep 8` and then reply with exactly the word DONE."}]}})
            t = until(lambda m: m.get("id") == 3)["result"]["turn"]["id"]
            until(lambda m: m.get("method") == "item/started"
                  and ((m.get("params") or {}).get("item") or {}).get("type") == "commandExecution")
            send({"id": 4, "method": "turn/start", "params": {"threadId": tid, "clientUserMessageId": j, "input": [
                {"type": "text", "text": "Also include the word KIWI."}]}})
            # Both observations, in either order (S2-14), before T's OWN completion (R2-12): the answer to J's
            # request, and a userMessage item of THIS thread with J's clientId.
            watch, said = M5Join(tid, t, 4, j), []

            def decided(m) -> bool:
                said.append(watch.feed(m))
                return said[-1] is not None
            until(decided)
            if said[-1] != "proved":
                raise SystemExit("FAIL: M5 (join): " + ("the answer to J named another turn" if said[-1] == "wrong" else
                                                         "T completed before both the answer to J and J's item came"))
            joined, item = watch.answer, watch.item
        finally:
            try:
                proc.stdin.close()
                proc.wait(30)
            except Exception:
                proc.kill()
            raw.close()
        print(f"  M5 raw evidence (join): turn A {t} (clientUserMessageId {a}); turn/start J clientUserMessageId {j}")
        print(f"    answer to J: {json.dumps(joined)[:300]}")
        print(f"    J's item:    {json.dumps(item)[:400]}")
        self.check(watch.holds(),
                   "M5 (join): the answer to J's turn/start names the active turn T, and J's userMessage item, with "
                   "J's clientId, came in T, both before T's own turn/completed")

    def _q3_refusals(self, real: bool, model: list[str]) -> None:
        print("\n[Q3] the refusals: a Claude turn; the cap; a second steer; a restart while one is unsettled")
        hold = self.tmp / "q3-release"
        # A Claude session (its adapter does not steer: its node never says `steer`): refused with that reason, which
        # comes before whether a turn is in flight at all.
        first = "Reply with exactly the word: ready" if self.real else "ready"
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", AGENT, "--model", "haiku",
                               "--wait", "--json")
        cl = json.loads(out)["session"]["session_id"]
        rc, _, err = self.board_rc("session", "send", cl, self.STEER_TEXT, "--mode", "steer")
        if self.real and self.cli_version() in STEER_MODES:
            # Step 3: a verified process of a version that passed gate M8 steers (Z1-Z3); with nothing in flight
            # the steer is refused as idle, and nothing is made.
            self.check(rc != 0 and "no turn is in flight" in err and len(self.show(cl)["messages"]) == 1,
                       "Claude (a version that steers): refused with nothing in flight, nothing is made")
        else:
            self.check(rc != 0 and "cannot steer into a running turn (its node says so)" in err
                       and len(self.show(cl)["messages"]) == 1,
                       "Claude: refused with the reason (its adapter does not steer), nothing is made")
        self._close(cl)
        sid, _, _ = self._codex_root(self.LONG if real else f"hold {hold} q3", model)
        self._running(sid, 1, real)
        rc, _, err = self.board_rc("session", "send", sid, "x" * 12_300, "--mode", "steer")
        self.check(rc != 0 and "over 12,288 bytes" in err, "the bound: a steer over 12,288 bytes is refused")
        self.board("session", "send", sid, self.STEER_TEXT, "--mode", "steer")
        for argv, words, what in (
                (["send", sid, "again", "--mode", "steer"], "is not settled yet", "a second steer while one is unsettled"),
                (["send", sid, "redo", "--mode", "restart"], "a restart waits for it", "a restart while it is unsettled"),
                (["send", sid, "x", "--mode", "steer", "--output-schema", '{"type": "object"}'], "does not go with",
                 "--output-schema with a steer, before sending")):
            rc, _, err = self.board_rc("session", *argv)
            self.check(rc != 0 and words in err, f"refused: {what}")
        out, _, _ = self.board("session", "list")
        self.check("steer unsettled (into message 1)" in out, "board session list marks the steer unsettled")
        if not real:
            hold.touch()
        self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 120)
        rc, _, err = self.board_rc("session", "send", sid, "late", "--mode", "steer")
        self.check(rc != 0 and "no turn is in flight" in err, "after N's end: no turn in flight, refused")
        self._close(sid)

    def _q4_parent(self, real: bool, model: list[str]) -> None:
        print("\n[Q4] a parent steers its Codex child with board_session_send(mode=steer, wait=true)")
        first = "Reply with exactly the word: ready" if self.real else "ready"
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", SPAWN_AGENT, "--model",
                               "haiku", "--wait", "--json")
        rid = json.loads(out)["session"]["session_id"]
        self.board("session", "grant", str(self.project), "--agent", SPAWN_AGENT, "--access", "workspace",
                   "--hours", "1")
        hold, busy = self.tmp / "q4-release", self.tmp / "q4-parent"
        if not self.real:
            # The parent is in a turn of its own meanwhile (as a parent that waits is), so the child's result waits
            # for it and the wait takes it; an idle parent would get it as its next turn (the answer then says so).
            self.board("session", "send", rid, f"hold {busy} q4 parent")
            self._running(rid, 2, False)
        task = self.LONG if real else f"hold {hold} q4"
        [made] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
            "task": task, "child_agent": CODEX_AGENT, "access": "read-only",
            **({"model": self.args.codex_model} if real else {})}]], rid)
        if made["error"]:
            raise SystemExit(f"FAIL: the Codex child was refused: {made['text'][:600]}")
        kid = json.loads(made["text"])["child"]["id"]
        self._running(kid, 1, real)
        got: list = []
        steer = "Also say PLUM in your reply." if real else "plum please"
        th = threading.Thread(target=lambda: got.append(self.mcp_calls(SPAWN_AGENT, [["board_session_send", {
            "child": kid, "message": steer, "mode": "steer", "wait": True}]], rid)))
        th.start()
        line = self._wait_steer_line(steer, 60)
        if not real:
            hold.touch()
        th.join(120)
        [sent] = got[0] if got else [{"error": True, "text": "no answer"}]
        out = json.loads(sent["text"]) if not sent["error"] else {}
        res = (out.get("result") or {}).get("result") or {}
        print(f"  answer: steer {out.get('steer')}; status {out.get('steer_status')}; result steer {res.get('steer')}"
              + (f"; ERROR {sent['text'][:600]}" if sent["error"] else ""))
        self.check(line is not None and "A message sent while your turn was running" in json.dumps(line),
                   "the parent's text went in the follow-up frame that says what it is")
        self.check(out.get("message", 1) is None and (out.get("steer_status") or {}).get("outcome") == "in_turn",
                   "the answer carries the steer's terminal status, read from its record: in_turn")
        if res or not self.real:
            self.check((res.get("steer") or {}).get("outcome") == "in_turn",
                       "wait=true took the child's result for that turn, with the steer's outcome in_turn")
            text = (out.get("result") or {}).get("text") or ""
            self.check("\nYour steer into this turn: taken into it.\n<<<child-output " in text,
                       "the result's text carries the hub's line, outside the child's markers")
            if not real:
                self.check("STEERED: [AGENT-COMMS] A MESSAGE SENT WHILE YOUR TURN WAS RUNNING" in text
                           and "PLUM PLEASE" in text, "the stand-in child read the steer, in its frame, in its turn")
        else:
            print(f"  (the idle real parent got the result as its next turn: {out.get('note')!r})")
            self.check("went to you as a turn" in (out.get("note") or ""), "the answer says where the result went")
        if not self.real:
            busy.touch()
        self.board("session", "close", rid)
        self.check(all(self.wait_ended(x, 40)["state"] == "ended" for x in (rid, kid)), "closed, with its child")

    def _q5_answers(self, model: list[str]) -> None:
        print("\n[Q5] stand-in only: Codex refuses the steer; answers an internal error; never answers")
        for knob, code, outcome in (("refuse-steer", 5, "not_delivered"), ("internal-error-steer", 6, "unconfirmed"),
                                    ("no-steer-answer", 6, "unconfirmed")):
            hold = self.tmp / f"q5-{knob}"
            sid, _, _ = self._codex_root(f"hold {hold} {knob}", model)
            self._running(sid, 1, False)
            text = f"steer for {knob}"
            got: list = []
            # The first call waits (in a thread) while the stand-in's turn is held; then a retry with its request id.
            th = threading.Thread(target=lambda: got.append(self.board_rc(
                "session", "send", sid, text, "--mode", "steer", "--request-id", knob, "--wait")))
            th.start()
            self._wait_steer_line(text)
            hold.touch()
            th.join(120)
            rc, out, err = got[0] if got else (None, "", "")
            words = {"not_delivered": "steer: not delivered", "unconfirmed": "steer: not confirmed"}[outcome]
            print(f"  {knob}: exit {rc}; {[x for x in err.splitlines() if x.startswith('steer:')]}")
            self.check(rc == code and words in err
                       and (text in err.split("steer:", 1)[-1]) == (outcome == "not_delivered"),
                       f"{knob}: {outcome}, exit {code}" + (", with the text this call sent, to send again"
                                                            if code == 5 else ""))
            rc, out, err = self.board_rc("session", "send", sid, text, "--mode", "steer", "--request-id", knob,
                                         "--wait")
            self.check(rc == code and words in err and text not in err.split("steer:", 1)[-1],
                       "a retry with --wait answers the same, and never prints the text")
            self.check(self.show(sid)["messages"][0]["steer"]["outcome"] == outcome, "show says it too")
            self._close(sid)

    def _q6_node_restart(self, model: list[str]) -> None:
        print("\n[Q6] stand-in only: the node restarts after the steer was written: unconfirmed, never sent again")
        hold = self.tmp / "q6-release"
        sid, _, _ = self._codex_root(f"hold {hold} q6", model)
        self._running(sid, 1, False)
        text = "steer before the restart"
        self.board("session", "send", sid, text, "--mode", "steer")
        self.check(self._wait_steer_line(text) is not None, "written")
        self.restart_node()
        m = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 60)
        view = self.show(sid)
        self.check(m is not None and m["state"] == "failed"
                   and view["messages"][0]["steer"]["outcome"] == "unconfirmed",
                   "N ended with its process; the steer is unconfirmed")
        hold.touch()
        out, _, _ = self.board("session", "send", sid, "after the restart", "--wait")
        self.check("AFTER THE RESTART" in out and len(self._steer_lines(text)) == 1,
                   "the session goes on in a new process, and the steer was never sent again")
        self._close(sid)

    def _q7_partition(self, model: list[str]) -> None:
        print("\n[Q7] stand-in only (--only steering): a partition before the steer could be written: its authority "
              "lapses and is lost for good")
        hold = self.tmp / "q7-release"
        sid, _, _ = self._codex_root(f"hold {hold} late-item q7", model)
        self._running(sid, 1, False)
        text = "steer across a partition"
        self.board("session", "send", sid, text, "--mode", "steer")
        time.sleep(2)           # offered: the node holds it, unwritten (the turn is not proved yet)
        self.check(self._steer_lines(text) == [], "offered and held, not written: N's turn is not proved yet")
        hub = next(p for n, p in self.procs if n == "hub")
        print("  the hub is stopped (SIGSTOP) for 82 s: no hello or report of the node is acknowledged")
        hub.send_signal(signal.SIGSTOP)
        try:
            time.sleep(82)
        finally:
            hub.send_signal(signal.SIGCONT)
        Path(str(hold) + ".item").touch()       # the proof comes now
        time.sleep(4)
        self.check(self._steer_lines(text) == [], "contact is back and the turn is proved: still never written")
        hold.touch()
        m = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 60)
        steer = self.show(sid)["messages"][0].get("steer") or {}
        self.check(m is not None and m["state"] == "done" and (steer.get("outcome"), steer.get("why"))
                   == ("not_delivered", "not_written"), "N completed; the steer: not delivered (never written)")
        self._close(sid)

    # ------------------------------------------------------------ Claude turn correlation (X1-X4)

    def transcript(self, sid: str) -> list[dict]:
        """The session's transcript on the node: every line its processes wrote (a settings answer withheld)."""
        out = []
        for line in (self.node_home / "data" / "node-logs" / "sessions" / f"{sid}.jsonl").read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
        return out

    def written_lines(self) -> list[dict]:
        """Every user line written to a Claude session process so far (the wrapper's stdin log), parsed."""
        lines = [json.loads(x) for x in self.stdin_log.read_text().splitlines() if x.strip()]
        return [x for x in lines if x.get("type") == "user"]

    def echoed(self, sid: str, text: str) -> tuple[str | None, list[dict], list[dict]]:
        """(the uuid of the LAST line written with this text, the `init` frames echoing it, the `result` frames
        echoing it), from the wrapper's stdin log and the session's transcript."""
        mine = [x for x in self.written_lines() if x["message"]["content"] == text]
        u = mine[-1].get("uuid") if mine else None
        frames = self.transcript(sid)
        inits = [f for f in frames if f.get("subtype") == "init" and u in (f.get("user_message_uuids") or [])]
        results = [f for f in frames if f.get("type") == "result" and (
            f.get("user_message_uuid") == u or u in (f.get("user_message_uuids") or []))]
        return u, inits, results

    def check_echo(self, sid: str, text: str, what: str) -> tuple[dict, dict]:
        u, inits, results = self.echoed(sid, text)
        version = next((f.get("claude_code_version") for f in inits), None)
        print(f"  line uuid {u}; init echoes: {len(inits)}; result echoes: {len(results)}; version {version}")
        self.check(isinstance(u, str) and len(u) == 36, f"{what}: the line written carries a uuid")
        self.check(len(inits) == 1 and len(results) == 1,
                   f"{what}: its init lists it in user_message_uuids and its result in user_message_uuid(s)")
        return inits[0], results[0]

    def resumes(self, sid: str) -> int:
        starts = [json.loads(x) for x in Path(str(self.stdin_log) + ".argv").read_text().splitlines()]
        return sum(1 for a in starts if sid in a and "--resume" in a)

    def cli_version(self) -> str:
        """The Claude CLI's own version (`claude --version`), or the stand-in's."""
        if not self.real:
            return "2.1.330-stand-in"
        return subprocess.run([self.args.claude_bin, "--version"], capture_output=True, text=True,
                              timeout=60).stdout.split()[0]

    def correlation_scenario(self) -> None:
        """X1-X4 (the module text)."""
        real = self.real
        model = ["--model", "haiku"]
        words = {k: (f"Reply with exactly the word: {k}" if real else k) for k in ("alpha", "beta", "gamma", "kiwi")}
        long = ("Run this exact shell command once, with your shell or Bash tool, and nothing else first: `sleep 12`. "
                "When it has finished, reply with exactly the word: done") if real else "sleep 12 for the restart"

        [version] = sorted(self.node_verified)
        print(f"\n[X0] a hub and a node of their own; the node runs with {version} in VERIFIED (that node process "
              "only)")

        print("\n[X1] a Claude session: the line carries a uuid; the turn's init and result echo it")
        out, _, _ = self.board("session", "start", str(self.project), words["alpha"], "--agent", CORR_AGENT, *model,
                               "--wait", "--json")
        data = json.loads(out)
        sid = data["session"]["session_id"]
        self.check(data["message"]["state"] == "done" and "alpha" in (data["reply"] or "").lower(),
                   "the first turn was answered")
        self.check_echo(sid, words["alpha"], "the first turn")

        print("\n[X2] M3: the node is restarted; the session's next process resumes it, and its turns echo")
        before, resumes = self.show(sid)["pid"], self.resumes(sid)
        self.restart_node()
        out, _, _ = self.board("session", "send", sid, words["beta"], "--wait")
        pid = self.show(sid)["pid"]
        self.check(self.resumes(sid) == resumes + 1 and pid != before and alive(pid),
                   "the turn ran in ONE new process, started with --resume")
        self.check("beta" in out.lower(), "the resumed process's first turn was answered")
        for word, what in (("beta", "first"), ("gamma", "second")):
            if word == "gamma":
                out, _, _ = self.board("session", "send", sid, words["gamma"], "--wait")
                self.check("gamma" in out.lower(), "the resumed process's second turn was answered")
            init, _ = self.check_echo(sid, words[word], f"M3, the resumed process's {what} turn")
            self.check(init.get("claude_code_version") == version
                       and "msg_lifecycle_v1" in (init.get("capabilities") or []),
                       f"... its init names {version} and msg_lifecycle_v1")
        self.check(self.show(sid)["pid"] == pid and self.resumes(sid) == resumes + 1,
                   f"both turns ran in the same resumed process (pid {pid}), and no other was started")

        print("\n[X3] M7: a long turn restarted in-band; its aborted result echoes the interrupted line")
        self.board("session", "send", sid, long)
        n = len(self.show(sid)["messages"])
        m = self.message(sid, n, lambda m: m["state"] == "delivered", 60)
        self.check(m is not None and m["state"] == "delivered", f"message {n}'s turn is in flight")
        deadline = time.monotonic() + 60
        while not self.echoed(sid, long)[1] and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(4 if real else 1)        # into the command
        out, _, _ = self.board("session", "send", sid, words["kiwi"], "--mode", "restart", "--wait", "--json")
        data = json.loads(out)
        old = next(x for x in self.show(sid)["messages"] if x["n"] == n)
        self.check(old["state"] == "failed" and old.get("restarted_by") == n + 1,
                   "the long turn was interrupted by the restart, as the provider said")
        _, aborted = self.check_echo(sid, long, "M7, an interrupted turn")
        self.check(aborted.get("terminal_reason") == "aborted_tools" and aborted.get("is_error") is True,
                   "... and that result is the abort (error_during_execution, aborted_tools)")
        self.check("kiwi" in (data["reply"] or "").lower(), "the restart's message was answered")
        self.check_echo(sid, words["kiwi"], "the restart's message")

        if not real:
            print("\n[X4] a turn the CLI starts itself right after a turn is no message's")
            self.board("session", "send", sid, "bg 2 1 one", "--wait")
            out, _, _ = self.board("session", "send", sid, "two", "--wait")
            self.check(out.strip().endswith(": TWO"),
                       f"the next message got its own reply ({out.strip()!r}), not the CLI's own turn's")
        self.board("session", "close", sid)
        deadline = time.monotonic() + 20
        while self.show(sid)["state"] != "ended" and time.monotonic() < deadline:
            time.sleep(0.2)
        self.check(self.show(sid)["state"] == "ended", "closed")

    # ------------------------------------------------------------ --with-board: a woken session reads the board

    def probe(self, post_id: int, sid: str) -> dict:
        r = subprocess.run([sys.executable, "-c", PROBE, AGENT, str(self.project), str(post_id), sid],
                           cwd=str(self.tmp), capture_output=True, text=True, timeout=60,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return json.loads(r.stdout)

    def tool_uses(self, sid: str) -> list[dict]:
        """The tool calls the session's process made, in order, from its transcript on the node."""
        uses = []
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                content = (ev.get("message") or {}).get("content") if ev.get("type") == "assistant" else None
                uses += [b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        return uses

    def tool_results(self, sid: str) -> dict[str, str]:
        """What each tool call returned to the session (tool_use id -> the result, as text), from its transcript."""
        out = {}
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                content = (ev.get("message") or {}).get("content") if ev.get("type") == "user" else None
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        out[b.get("tool_use_id")] = json.dumps(b.get("content"))
        return out

    def tool_calls(self, sid: str) -> list[str]:
        return [b.get("name", "?") for b in self.tool_uses(sid)]

    def board_scenario(self) -> None:
        """A real session with the temp board's tools: is a wake enough for it to read the board? Reports what the
        session did either way (its tool calls and its reply) before anything is asserted about the receipt."""
        print("\n[1] board session start <project> <message> --wait   (the session has the temp board's MCP tools)")
        out, _, took = self.board("session", "start", str(self.project), "Reply with exactly the word: ready",
                                  "--agent", AGENT, "--model", "haiku", "--wait", "--json")
        data = json.loads(out)
        sid = data["session"]["session_id"]
        print(f"  session {sid}; wall {took:.2f}s; reply: {data['reply']!r}")
        self.check(data["message"]["state"] == "done", "the first turn ended")
        before = self.tool_calls(sid)       # the board's own instructions say to register at session start
        print(f"  tools called in the first turn: {before or 'none'}; board sessions of {AGENT} so far: "
              f"{[row['id'] for row in self.probe(0, sid)['sessions']] or 'none'}")

        print(f"\n[2] {POSTER} posts a request addressed to {AGENT}; nothing else is said to the session")
        self.poster_sid = self.api(self.poster_token, "POST", "/api/sessions",
                                   {"project": str(self.project)})["session_id"]
        tid = self.api(self.poster_token, "POST", "/api/threads",
                       {"title": "delivery check", "project": str(self.project),
                        "session_id": self.poster_sid})["id"]
        t1 = time.time()        # the hub's clock is this machine's
        post = self.api(self.poster_token, "POST", "/api/posts",
                        {"body": f"Delivery check {MARKER}: no work is needed. Reading this post is enough.",
                         "type": "request", "thread_id": tid, "to": [AGENT], "needs_response": True,
                         "session_id": self.poster_sid})
        m2 = self.message(sid, 2, lambda m: m["delivered_at"] is not None, 15)
        self.check(m2 is not None and m2["kind"] == "wake" and m2["delivered_at"] is not None,
                   "the post queued a wake and it was written to the process")
        print(f"  post #{post['id']} in thread {tid} +0.00s; wake written +{m2['delivered_at'] - t1:.2f}s")
        print(f"  stdin line: {self.stdin_lines()[1]!r}")
        self.check(MARKER not in self.stdin_log.read_text(), "the stdin line holds no part of the post's body")

        deadline, state = time.monotonic() + BOARD_BOUND, self.probe(post["id"], sid)
        while state["shown_at"] is None and time.monotonic() < deadline:
            time.sleep(0.5)
            state = self.probe(post["id"], sid)
        m2 = self.message(sid, 2, lambda m: m["ended_at"] is not None, 180)
        view = self.show(sid)
        calls = self.tool_calls(sid)[len(before):]
        print(f"  wake turn: {m2['state']}" + (f", ended +{m2['ended_at'] - t1:.2f}s" if m2["ended_at"] else "")
              + (f"; tools denied: {view['denials']}" if view.get("denials") else ""))
        print(f"  tools the session called in the wake turn: {calls or 'none'}")
        results = self.tool_results(sid)
        for use in self.tool_uses(sid)[len(before):]:
            if use.get("name", "").startswith("mcp__agent-comms__"):   # its own arguments, on the temp board
                got = results.get(use.get("id"))
                print(f"    {use['name'].removeprefix('mcp__agent-comms__')}({json.dumps(use.get('input'))[:300]})"
                      f" -> the result held the post's body: "
                      + ("no result seen" if got is None else "yes" if MARKER in got else "no"))
        print(f"  reply: {view['reply']!r}")
        posts = self.api(self.poster_token, "GET", f"/api/threads/{tid}/posts")["posts"]
        for p in posts:
            print(f"  thread {tid}: #{p['id']} by {p['agent']} ({p['type']}): {p['body'][:160]!r}")
        self.check(m2["state"] == "done", "the wake turn ran to its end")
        self.check(state["shown_at"] is not None,
                   f"the hub noted that a session of {AGENT} was shown post #{post['id']} within {BOARD_BOUND}s")
        shown, receipt = state["shown_at"], state["receipt"]
        print(f"  shown: +{shown - t1:.2f}s after the post (+{shown - m2['delivered_at']:.2f}s after the wake was "
              "written); delivery receipt: "
              + (f"board session {receipt['session_id']}, +{receipt['at'] - t1:.2f}s" if receipt
                 else "none (a filtered view leaves none)"))
        self.check(shown >= m2["delivered_at"], "it was shown after the wake was written: the read happened in the "
                                                "wake turn (the session was idle before it)")
        self.check("mcp__agent-comms__board_read_updates" in calls, "the wake turn called board_read_updates")
        self.check(not {"Bash", "Edit", "Write", "NotebookEdit"} & set(calls), "... and no shell or file-writing tool")

        # The grace counts from the later of the wake being queued and the node being shown it (the hub's records).
        def grace_from(st: dict) -> float:
            return max(st["woken_at"], st["handed_at"] or st["woken_at"])
        wait = grace_from(state) + BOARD_GRACE + 0.5 - time.time()
        if wait > 0:
            time.sleep(wait)
        state = self.probe(post["id"], sid)
        print(f"  holds: {state['holds']} at +{state['now'] - t1:.2f}s; wake queued +{state['woken_at'] - t1:.2f}s, "
              f"handed to the node +{grace_from(state) - t1:.2f}s; wake_grace_seconds {state['grace']:g}; "
              f"readers {state['readers']}")
        self.check(state["now"] - grace_from(state) > state["grace"] and state["holds"] is True,
                   "hosted.holds is true for the post after the grace (counted from the hand-over): the session was "
                   "shown it after the wake")
        print("  board sessions of the hosted agent (what it registered with):")
        for row in state["sessions"]:
            print(f"    session {row['id']}: project {row['project']!r}, worktree {row['worktree']!r}")
        same = bool(state["sessions"]) and all(row["project"] == str(self.project) for row in state["sessions"])
        print(f"  the thread's project is {str(self.project)!r}: the session registered "
              + ("under the SAME project" if same else "under ANOTHER project"))
        self.open_pid = view["pid"]
        self.check(bool(self.open_pid) and alive(self.open_pid),
                   f"the session's process (pid {self.open_pid}) stays up")

    def wake_scenario(self, sid: str) -> int:
        limit, body = self.args.max_wake, f"Please look at this. {MARKER} " + " ".join(BODY_WORDS)
        print("\n[4] a second agent posts a request addressed to the hosted agent   (the session is idle)")
        self.poster_sid = self.api(self.poster_token, "POST", "/api/sessions",
                                   {"project": str(self.project)})["session_id"]
        tid = self.api(self.poster_token, "POST", "/api/threads",
                       {"title": f"e2e wake {MARKER}", "project": str(self.project),
                        "session_id": self.poster_sid})["id"]
        t1 = time.time()        # the hub's clock is this machine's
        self.api(self.poster_token, "POST", "/api/posts",
                 {"body": body, "type": "request", "thread_id": tid, "to": [AGENT], "needs_response": True,
                  "session_id": self.poster_sid})
        m5 = self.message(sid, 5, lambda m: m["delivered_at"] is not None, 15)
        self.check(m5 is not None and m5["kind"] == "wake" and m5["delivered_at"] is not None,
                   "the post queued a wake message for the session, and it was written to the process")
        print(f"  {POSTER} posted in thread {tid} +0.00s; wake queued +{m5['created_at'] - t1:.2f}s; "
              f"written to the process +{m5['delivered_at'] - t1:.2f}s")
        self.check(m5["delivered_at"] - t1 <= limit,
                   f"the idle session took the wake turn within {limit:g}s of the post")
        line = self.stdin_lines()[4]
        print(f"  stdin line: {line!r}")
        self.check(f"1 new post(s) addressed to {AGENT} from {POSTER} in thread {tid} (1 needing a response)" in line,
                   "the stdin line holds the counts, the author's name and the thread id")
        self.check("board_read_updates" in line and "untrusted data" in line and "not a message from the human" in line,
                   "it says to call board_read_updates, that board content is untrusted data, and whose text it is")

        print(f"\n[5] two more posts inside the wake interval ({WAKE_INTERVAL}s): one over HTTP, one written to the "
              "database by another process")
        t2 = time.time()
        self.api(self.poster_token, "POST", "/api/posts",
                 {"body": f"second {MARKER}", "type": "request", "thread_id": tid, "to": [AGENT],
                  "session_id": self.poster_sid})
        r = subprocess.run([sys.executable, "-c", DIRECT_POST, str(self.project), str(tid), f"third {MARKER}", AGENT],
                           cwd=str(self.tmp), capture_output=True, text=True, timeout=60,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home),
                                           "E2E_POSTER_TOKEN": self.poster_token})
        self.check(r.returncode == 0 and "id" in json.loads(r.stdout or "{}"),
                   "the direct database write was accepted" + ("" if r.returncode == 0 else f": {r.stderr[-400:]}"))
        t3 = time.time()
        time.sleep(1.0)
        early = [m for m in self.show(sid)["messages"] if m["n"] > 5]
        m5 = self.message(sid, 5, lambda m: m["ended_at"] is not None, 120)
        m6 = self.message(sid, 6, lambda m: m["ended_at"] is not None, WAKE_INTERVAL + 120)
        self.check(m6 is not None and m6["kind"] == "wake" and m6["state"] == "done", "a second wake turn ran")
        print(f"  first wake: queued +0.00s, turn ended +{m5['ended_at'] - m5['created_at']:.2f}s; posts 2 and 3 "
              f"+{t2 - m5['created_at']:.2f}s and +{t3 - m5['created_at']:.2f}s; second wake queued "
              f"+{m6['created_at'] - m5['created_at']:.2f}s, written +{m6['delivered_at'] - m5['created_at']:.2f}s")
        self.check(early == [] or m6["created_at"] - m5["created_at"] >= WAKE_INTERVAL,
                   "nothing was queued for them at once")
        self.check(m6["created_at"] - m5["created_at"] >= WAKE_INTERVAL - 0.01,
                   f"the second wake was queued no sooner than {WAKE_INTERVAL}s after the first")
        self.check(m6["created_at"] >= m5["ended_at"], "... and only after the first wake's turn had ended")
        lines = self.stdin_lines()
        print(f"  stdin line: {lines[5]!r}")
        self.check(len(lines) == 6 and f"2 new post(s) addressed to {AGENT} from {POSTER} in thread {tid}" in lines[5],
                   "both posts went as ONE wake")
        view = self.show(sid)
        self.check([m["kind"] for m in view["messages"]] == ["user"] * 4 + ["wake"] * 2 and view["queued"] == 0,
                   "four messages of the human's and two wakes; nothing else was queued")
        everything = self.stdin_log.read_text() + self.log_text("node")
        self.check(not any(w in everything for w in (MARKER, *BODY_WORDS, "e2e wake")),
                   "no part of a post body or of the thread title is in the process's stdin or the node's log")
        self.check(view["reply_for"] == 6 and bool((view["reply"] or "").strip()),
                   "the wake turn's reply is the session's last reply (the human's to read)")
        print(f"  reply: {view['reply']!r}")
        return tid

    # ------------------------------------------------------------ parent and child sessions

    def git(self, *argv: str) -> str:
        r = subprocess.run(["git", "-C", str(self.project), "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid",
                            "-c", "commit.gpgsign=false", *argv], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise SystemExit(f"`git {' '.join(argv)}` failed in the temp checkout:\n{r.stderr[-800:]}")
        return r.stdout.strip()

    def enable_tree(self) -> None:
        """The settings of the tree scenario, written while nothing runs: child sessions on (hub); a worktree
        directory in the temp directory and the runners described in the module text (node). Full access is NOT
        allowed on the node at first (`node_settings`). Once per run (the steering scenario uses them too)."""
        if getattr(self, "tree_enabled", False):
            return
        self.tree_enabled = True
        agents = self.hub_home / "agents.toml"
        for name in (FULL_AGENT, GRANT_AGENT, MODE_AGENT, RESTRICTED_AGENT, WORK_AGENT):
            create_agent(agents, name, "claude-code")
        # D7-D8: two Codex agents whose runners reach the temp board's own `board mcp` (by token file, below).
        check_tokens = self.tmp / "tokens"
        check_tokens.mkdir(exist_ok=True)
        for name in (CHECK_CODEX, DENIED_CODEX):
            f = check_tokens / f"{name}.token"
            f.touch(mode=0o600)
            f.write_text(create_agent(agents, name, "codex-cli") + "\n")
        # A1: the work account's folder (an empty one: the stand-in reads nothing there) and its recording wrapper.
        self.work_home = self.tmp / "claude-work-home"
        self.work_home.mkdir(exist_ok=True)
        self.work_env_log = self.tmp / "logs" / "work-env.log"
        work_exe = self.tmp / "bin-work" / "claude"
        work_exe.parent.mkdir(exist_ok=True)
        work_exe.write_text(ENV_WRAPPER.format(python=sys.executable, log=str(self.work_env_log),
                                               inner=str(self.tmp / "bin" / "claude")))
        work_exe.chmod(0o700)
        # T10: the spawning agent signs in to the temp board from a token file in the temp directory, through this
        # checkout's `board mcp` (stdio, on the hub's database), which inherits its session's binding.
        tokens = self.tmp / "tokens"
        tokens.chmod(0o700)
        token_file = tokens / f"{SPAWN_AGENT}.token"
        token_file.touch(mode=0o600)
        token_file.write_text(create_agent(agents, SPAWN_AGENT, "claude-code") + "\n")
        self.spawn_mcp = self.tmp / "spawner-mcp.json"
        self.spawn_mcp.write_text(json.dumps({"mcpServers": {"agent-comms": {
            "type": "stdio", "command": sys.executable,
            "args": ["-m", "agent_comms.cli", "mcp", "--agent", SPAWN_AGENT],
            "env": {"AGENT_COMMS_HOME": str(self.hub_home), "AGENT_COMMS_TOKEN_DIR": str(tokens),
                    "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}}}}, indent=2))
        self.project_mode = self.tmp / "checkout-mode"      # where a settings file selects bypassPermissions
        self.worktrees = self.tmp / "worktrees"
        # The grants fixture also carries two command hooks. Each appends the JSON it is given on stdin (it names
        # the session) to a log in the temp directory, so a hook that ran for a session is on record.
        self.hook_logs = {"SessionStart": self.tmp / "logs" / "hook-session-start.log",
                          "PreToolUse": self.tmp / "logs" / "hook-pre-read.log",
                          "plugin SessionStart": self.tmp / "logs" / "hook-plugin-session-start.log"}
        # ... and a plugin directory (loaded with --plugin-dir) whose SessionStart hook does the same.
        self.plugin = self.tmp / "plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (self.plugin / "hooks").mkdir(exist_ok=True)
        (self.plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": "e2e-plugin", "version": "0.0.1", "description": "a SessionStart hook that writes a log"}))
        (self.plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f"(cat; echo) >> {self.hook_logs['plugin SessionStart']}"}]}]}}))
        hooked = GRANTS | {"hooks": {
            "SessionStart": [{"hooks": [{"type": "command",
                                         "command": f"(cat; echo) >> {self.hook_logs['SessionStart']}"}]}],
            "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command",
                                                          "command": f"(cat; echo) >> {self.hook_logs['PreToolUse']}"}]}]}}
        for directory, fixture in ((self.project, hooked), (self.project_mode, BYPASS_BY_SETTINGS)):
            (directory / ".claude").mkdir(parents=True, exist_ok=True)
            (directory / ".claude" / "settings.json").write_text(json.dumps(fixture, indent=2) + "\n")
        hub = (self.hub_home / "board.toml").read_text()
        hub = hub.replace(f"agents = {toml_list([AGENT, CODEX_AGENT, STEER_AGENT, CORR_AGENT])}", "agents = " + toml_list(
            [AGENT, CODEX_AGENT, FULL_AGENT, GRANT_AGENT, MODE_AGENT, RESTRICTED_AGENT, SPAWN_AGENT, WORK_AGENT,
             CHECK_CODEX, DENIED_CODEX, STEER_AGENT]))
        hub = hub.replace(f"projects = {toml_list([str(self.project)])}",
                          f"projects = {toml_list([str(self.project), str(self.project_mode)])}")
        (self.hub_home / "board.toml").write_text(hub + "\n[dispatch.sessions.spawn]\nenabled = true\n"
                                                        "max_children = 8\n"
                                                        # A2: claude-work runs in the temp checkout only
                                                        f"\n[dispatch.agent_projects]\n{json.dumps(WORK_AGENT)} = "
                                                        f"{toml_list([str(self.project)])}\n")
        self.node_settings(allow_full=False)
        if not (self.project / ".git").exists():
            self.git("init", "-q")
        if subprocess.run(["git", "-C", str(self.project), "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True, timeout=60).returncode != 0:     # no commit yet: a worktree needs one
            (self.project / "README").write_text("the e2e checkout\n")
            self.git("add", "README")
            self.git("commit", "-q", "-m", "init")

    def node_settings(self, allow_full: bool) -> None:
        """The node's board.toml for the tree scenario and the steering section's (no `claude_steer` key: the
        default, on)."""
        exe = str(self.tmp / "bin" / "claude")
        quiet = ["--setting-sources=", "--strict-mcp-config"] if self.real else []
        # The two runners that load a settings fixture: the fixture itself through --settings, and of the settings
        # sources only the project's (never the user's own settings and hooks).
        fixture = ["--setting-sources=project", "--strict-mcp-config"] if self.real else []
        worker = [exe, "-p", "{prompt}", "--permission-mode", "dontAsk", "--allowedTools=Write(./**),Edit(./**)", *quiet]
        bypass = [exe, "-p", "{prompt}", "--permission-mode", "bypassPermissions", *quiet]
        granted = [exe, "-p", "{prompt}", "--permission-mode", "dontAsk",
                   "--settings", str(self.project / ".claude" / "settings.json"), "--plugin-dir", str(self.plugin),
                   *fixture]
        restricted = [exe, "-p", "{prompt}", "--restricted", "--permission-mode", "bypassPermissions", *quiet]
        defmode = [exe, "-p", "{prompt}", "--settings", str(self.project_mode / ".claude" / "settings.json"), *fixture]
        # T10: the board's tools and nothing else allowed (dontAsk refuses the rest), from the temp board alone.
        spawner = [exe, "-p", "{prompt}", "--permission-mode", "dontAsk", *quiet, "--mcp-config", str(self.spawn_mcp),
                   "--allowedTools=mcp__agent-comms"]
        # A1: the work account's runner: the ordinary one, through the wrapper that records its environment.
        work = [str(self.tmp / "bin-work" / "claude"), "-p", "{prompt}", "--permission-mode", "dontAsk", *quiet]
        # Z1-Z3 (Claude steering): the send-mode runner, `sleep` allowed (a turn that waits in a tool call)
        steering = [exe, "-p", "{prompt}", "--permission-mode", "dontAsk", *quiet, "--allowedTools", "Bash(sleep:*)"]
        (self.node_home / "board.toml").write_text(f"""[conversations]
enabled = false

[dispatch]
kill_grace_seconds = 5
worktree_dir = {json.dumps(str(self.worktrees))}

[dispatch.runners]
{AGENT} = {toml_list(worker)}
{FULL_AGENT} = {toml_list(bypass)}
{GRANT_AGENT} = {toml_list(granted)}
{MODE_AGENT} = {toml_list(defmode)}
{RESTRICTED_AGENT} = {toml_list(restricted)}
{SPAWN_AGENT} = {toml_list(spawner)}
{WORK_AGENT} = {toml_list(work)}
{STEER_AGENT} = {toml_list(steering)}
{CODEX_AGENT} = {toml_list(self.codex_runner())}
{CHECK_CODEX} = {toml_list(self.board_codex_runner(CHECK_CODEX, approve=True))}
{DENIED_CODEX} = {toml_list(self.board_codex_runner(DENIED_CODEX, approve=False))}

[dispatch.runner_homes]
{json.dumps(WORK_AGENT)} = {json.dumps(str(self.work_home))}

[dispatch.worktrees]
{json.dumps(str(self.project))} = {json.dumps(str(self.project))}
{json.dumps(str(self.project_mode))} = {json.dumps(str(self.project_mode))}

[dispatch.sessions]
enabled = true
codex = true
allow_full_access = {"true" if allow_full else "false"}
max_live = 8
""")

    def claude_settings(self) -> dict:
        """(mtime in ns, SHA-256) of the user's own Claude settings files, to be compared: never written, never
        shown. The fixtures of this script live in the temp directory."""
        out = {}
        for name in ("settings.json", "settings.local.json"):
            path = Path(self.env["HOME"]) / ".claude" / name
            try:
                out[name] = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            except OSError:
                out[name] = None
        return out

    def wait_ended(self, sid: str, seconds: float = 20.0) -> dict:
        deadline = time.monotonic() + seconds
        while self.show(sid)["state"] != "ended" and time.monotonic() < deadline:
            time.sleep(0.2)
        return self.show(sid)

    def spawn_child(self, parent: str, text: str, *options: str) -> tuple[dict, dict, float]:
        """`board session start --parent ... --wait --json`: the child, its first message, and the wall time."""
        out, _, took = self.board("session", "start", "--parent", parent, *options, text, "--wait", "--json")
        data = json.loads(out)
        view = self.show(data["session"]["session_id"])     # also notes its pid, for the leftover check
        print(f"  {view['name']} {view['session_id']}: {view['agent']}, access {view['access']}, {view['isolation']}; "
              f"wall {took:.2f}s; queued -> written "
              f"{data['message']['delivered_at'] - data['message']['created_at']:.2f}s; turn "
              f"{data['message']['ended_at'] - data['message']['delivered_at']:.2f}s; reply: {data['reply']!r}")
        return view, data, took

    def hooks_ran(self, sid: str) -> list[str]:
        """The hook events of the settings fixture that ran for this session (their logs name it)."""
        out = []
        for event, path in self.hook_logs.items():
            try:
                if sid in path.read_text():
                    out.append(event)
            except OSError:
                pass
        return out

    def init_facts(self, sid: str) -> tuple[str | None, list[str]]:
        """What the session's Claude process reported when its (last) turn started: the permission mode it really
        ran in and the built-in tools it really had (MCP tools left out), from its transcript on the node."""
        mode, tools = None, []
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
                    mode = ev.get("permissionMode")
                    tools = sorted(t for t in ev.get("tools") or [] if isinstance(t, str) and not t.startswith("mcp__"))
        return mode, tools

    def permission_mode(self, sid: str) -> str | None:
        return self.init_facts(sid)[0]

    def started_with(self, sid: str) -> list[str] | None:
        """The arguments of the (last) start of this session's Claude process, as the wrapper recorded them."""
        try:
            starts = [json.loads(x) for x in Path(str(self.stdin_log) + ".argv").read_text().splitlines()]
        except OSError:
            return None
        return next((argv for argv in reversed(starts) if sid in argv), None)

    def settings_check(self, sid: str) -> tuple[dict | None, list[str], bool]:
        """What the node kept of its question to this session's process (`get_settings`, asked when a process is
        started from a settings file the node wrote, and answered before any turn is written to it): (the node's
        own record of the answer in the session's transcript, the kinds of the transcript's lines up to the first
        turn's `init` with Claude's hook events left out (a SessionStart hook runs, and says so, as the process
        starts), whether the answer ITSELF is anywhere in the transcript). The answer is every setting in force,
        which can hold credentials: only the record may be there."""
        record, kinds, raw = None, [], False
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            text = path.read_text()
            raw = raw or '"flagSettings"' in text       # a source's name: only the answer has it
            for line in text.splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                raw = raw or ev.get("type") == "control_response"
                kinds.append(f"{ev.get('type')}/{ev.get('subtype')}" if ev.get("subtype") else str(ev.get("type")))
                if ev.get("type") == "host" and ev.get("subtype") == "control_response_withheld" and record is None:
                    record = ev
        before = kinds[:kinds.index("system/init") + 1] if "system/init" in kinds else kinds
        return record, [k for k in before if not k.startswith("system/hook_")], raw

    def first_writes(self, sid: str) -> list[str]:
        """The kinds of the first lines written to this session's (last) process, from the wrapper's stdin log: the
        lines after the last `get_settings` question whose answer the node's record for this session names."""
        record = self.settings_check(sid)[0] or {}
        lines = [json.loads(x) for x in self.stdin_log.read_text().splitlines()]
        kinds = [x.get("request", {}).get("subtype") if x.get("type") == "control_request" else x.get("type")
                 for x in lines]
        asked = [i for i, x in enumerate(lines) if x.get("type") == "control_request"
                 and x.get("request_id") == record.get("request_id")]
        return kinds[asked[-1]:asked[-1] + 2] if asked else []

    def in_force(self, sid: str, cwd: Path) -> dict:
        """The `permissions` in force in a process started with this session's command line, asked by this script
        (`ask`): what the node's own question was answered with, which the node does not keep."""
        answer = self.ask(sid, cwd, {"subtype": "get_settings"})
        inner = answer.get("response") if isinstance(answer.get("response"), dict) else {}
        return (inner.get("effective") or {}).get("permissions") or {}

    def try_bypass(self, sid: str, cwd: Path) -> dict:
        """An explicit attempt to put a session into bypassPermissions (`ask`): a `set_permission_mode` request for
        that mode. Returns Claude's answer."""
        return self.ask(sid, cwd, {"subtype": "set_permission_mode", "mode": "bypassPermissions"})

    def ask(self, sid: str, cwd: Path, request: dict) -> dict:
        """A process started by this script with the very command line the node started this session's process
        with (only the session id is another), sent ONE line, a control request, and nothing else: no turn, no
        model. Returns Claude's answer (the `response` of its `control_response`)."""
        import uuid

        argv = [a for a in self.started_with(sid) or []]
        if not argv:
            raise SystemExit(f"FAIL: no recorded start of session {sid}")
        at = argv.index("--resume") if "--resume" in argv else argv.index("--session-id")
        argv[at:at + 2] = ["--session-id", str(uuid.uuid4())]
        import select

        proc = subprocess.Popen([self.claude_real, *argv], cwd=str(cwd), env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        found, buf, deadline = None, b"", time.monotonic() + 90
        try:
            proc.stdin.write(json.dumps({"type": "control_request", "request_id": "e2e-bypass",
                                         "request": request}).encode() + b"\n")
            proc.stdin.flush()      # ... and stdin stays open until the answer is there
            while found is None and time.monotonic() < deadline:
                if not select.select([proc.stdout], [], [], 0.5)[0]:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf and found is None:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    answer = ev.get("response") if isinstance(ev, dict) and ev.get("type") == "control_response" else None
                    if isinstance(answer, dict) and answer.get("request_id") == "e2e-bypass":
                        found = answer
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
        if found is None:
            err = proc.stderr.read().decode("utf-8", "replace")
            raise SystemExit(f"FAIL: no answer to the {request.get('subtype')} request (exit {proc.returncode}): "
                             f"{err[-400:]}")
        return found

    def tree_scenario(self) -> None:
        model = ["--model", "haiku"]
        if self.real:
            first = "Reply with exactly the word: ready"
            refuse = "If the tool is refused or not available, do not try any other way."
            write_ro = (f"Use the Write tool to create a file named ro-proof.txt in the current directory, containing "
                        f"the word ok. {refuse} Then reply with exactly one word: written or refused.")
            write_wt = ("Use the Write tool to create a file named wt-proof.txt in the current working directory, "
                        "containing the word ok. Then reply with exactly one word: written or refused.")

            def touch(name: str) -> str:
                return (f"Run exactly this shell command with the Bash tool, once: touch {name}\n{refuse} Then reply "
                        "with exactly one word: done or refused.")

            def both(written: str, touched: str) -> str:
                return (f"First use the Read tool to read the file README in the current directory. Then use the "
                        f"Write tool to create a file named {written} in the current directory, "
                        f"containing the word ok. Then run exactly this shell command with the Bash tool, once: "
                        f"touch {touched}\nIf a tool is refused or not available, do not try any other way. Then "
                        "reply with exactly one word: done.")
        else:
            first, write_ro, write_wt = "ready", "write ro-proof.txt", "write wt-proof.txt"

            def touch(name: str) -> str:
                return f"touch {name}"

            def both(written: str, touched: str) -> str:
                return f"probe README {written} {touched}"
        pong = "Reply with exactly the word: pong" if self.real_codex else "pong"

        print("\n[T0] the hub and the node run again, now with [dispatch.sessions.spawn] enabled = true")
        left = [s for s in json.loads(self.board("session", "list", "--json")[0])["sessions"]]
        for s in left:
            self.board("session", "close", s["session_id"])
            self.check(self.wait_ended(s["session_id"])["state"] == "ended",
                       f"the session an earlier scenario left open ({s['session_id']}) is closed first")
        out, err, _ = self.board("session", "start", "--parent", "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4", "x", ok=False)
        self.check("no such hosted session" in err, "a child needs a parent that exists")

        print("\n[T1] a Claude parent")
        out, _, took = self.board("session", "start", str(self.project), first, "--agent", AGENT, *model, "--wait", "--json")
        data = json.loads(out)
        sid = data["session"]["session_id"]
        parent = self.show(sid)
        print(f"  session {sid}; wall {took:.2f}s; reply: {data['reply']!r}; access {parent.get('access')}")
        self.check(data["message"]["state"] == "done" and parent.get("access") == "workspace",
                   "the parent's turn ended, and its node reported its access: workspace (the runner as configured)")
        out, err, _ = self.board("session", "start", "--parent", sid, "--access", "full", "x", ok=False)
        self.check("above the parent's (workspace)" in err, "a child above its parent's access is refused")
        argv = self.started_with(sid) or []
        given = argv[argv.index("--settings") + 1] if "--settings" in argv else None
        print(f"  its process was started with --settings {given}")
        self.check(given is not None and json.loads(given) == {"permissions": {NO_BYPASS: "disable"}}
                   and argv.count("--settings") == 1,
                   "bypass is switched off on its command line: the runner names no settings of its own, so the key "
                   "is given as JSON")
        answer = self.try_bypass(sid, self.project)
        print(f"  the same command line, sent a set_permission_mode request for bypassPermissions, answers: "
              f"{json.dumps(answer)}")
        self.check(answer.get("subtype") == "error" and answer.get("error_code") == "bypass_disabled",
                   "an explicit attempt to select bypass is refused by Claude itself: bypass_disabled")

        print("\n[T2] board session start --parent <id> --access read-only   (asked to write a file)")
        ro, data, _ = self.spawn_child(sid, write_ro, "--access", "read-only", "--role", "review", "--title",
                                       "Try to write", *model)
        self.check(data["message"]["state"] == "done" and ro["parent"] == sid and ro["name"] == "review-1",
                   "the read-only child took its turn under the parent, as review-1")
        self.check(not list(self.tmp.rglob("ro-proof.txt")), "it could not write: no ro-proof.txt anywhere in the temp "
                                                              "directory")
        if not self.real:
            self.check(data["reply"] == "turn 1: NO SUCH TOOL: WRITE", "Write is not among its tools")
        else:
            print(f"  its process reported permission mode {self.permission_mode(ro['session_id'])!r}")
            self.check(self.permission_mode(ro["session_id"]) == "dontAsk", "... in dontAsk mode")

        print("\n[T3] board session start --parent <id> --isolation worktree   (asked to write a file)")
        (self.project / "uncommitted.txt").write_text("the parent's work in progress\n")
        wt, data, _ = self.spawn_child(sid, write_wt, "--isolation", "worktree", "--role", "implementation", *model)
        path = Path(wt["worktree"] or "/nonexistent")
        print(f"  worktree: {path}")
        self.check(data["message"]["state"] == "done" and wt["isolation"] == "worktree" and path.is_dir()
                   and str(path).startswith(str(self.worktrees) + os.sep),
                   "the child has its own worktree, under the node's worktree directory")
        self.check((path / "wt-proof.txt").is_file() and not (self.project / "wt-proof.txt").exists(),
                   "the file it wrote is in its worktree and not in the parent's checkout")
        self.check((path / "README").is_file() and not (path / "uncommitted.txt").exists()
                   and self.git("rev-parse", "HEAD") in subprocess.run(
                       ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout,
                   "the worktree was made from the checkout's HEAD: the parent's uncommitted file is not in it")

        print(f"\n[T4] board session start --parent <id> --agent {CODEX_AGENT} --effort low   (a Codex child)")
        before = len(self.codex_stdin())
        cdx, data, _ = self.spawn_child(sid, pong, "--agent", CODEX_AGENT, "--model", self.args.codex_model,
                                        "--effort", "low", "--role", "research")
        wrote = self.codex_stdin()[before:]
        start = next(m["params"] for m in wrote if m.get("method") == "thread/start")
        turn = next(m["params"] for m in wrote if m.get("method") == "turn/start")
        print(f"  thread/start: {json.dumps(start)}; turn/start effort: {turn.get('effort')!r}")
        self.check(data["message"]["state"] == "done" and bool(data["reply"].strip()) and cdx["agent"] == CODEX_AGENT
                   and cdx["parent"] == sid, "the Codex child took its turn under the Claude parent")
        self.check(start == {"sandbox": self.codex_sandbox, "approvalPolicy": "never", "model": self.args.codex_model}
                   and turn.get("effort") == "low",
                   "its thread was started with the runner's sandbox, approvals off and the model, and nothing else "
                   "(no cwd); the turn carries effort low")
        where = self.codex_thread_cwd(cdx["session_id"], cdx["provider_id"])
        print(f"  Codex reports the thread's working directory as {where}")
        self.check(where is not None and os.path.realpath(where) == str(self.project),
                   "the thread works in the parent's checkout, which is where its process was started")

        print(f"\n[T4b] board session start --parent <id> --agent {CODEX_AGENT} --isolation worktree   (a Codex "
              "child in its own worktree)")
        before = len(self.codex_stdin())
        cwt, data, _ = self.spawn_child(sid, pong, "--agent", CODEX_AGENT, "--model", self.args.codex_model,
                                        "--isolation", "worktree", "--role", "test")
        wrote = self.codex_stdin()[before:]
        start = next(m["params"] for m in wrote if m.get("method") == "thread/start")
        codex_path = Path(cwt["worktree"] or "/nonexistent")
        where = self.codex_thread_cwd(cwt["session_id"], cwt["provider_id"])
        print(f"  thread/start: {json.dumps(start)}")
        print(f"  worktree: {codex_path}; Codex reports the thread's working directory as {where}")
        self.check(data["message"]["state"] == "done" and "cwd" not in start
                   and start == {"sandbox": self.codex_sandbox, "approvalPolicy": "never", "model": self.args.codex_model},
                   "its thread/start names no cwd either")
        self.check(codex_path.is_dir() and str(codex_path).startswith(str(self.worktrees) + os.sep)
                   and codex_path != path and where is not None and os.path.realpath(where) == str(codex_path),
                   "the thread works in the child's own worktree: its process was started there")

        print("\n[T4c] a settings file with grants and command hooks: in force for a workspace child, not for a "
              "read-only one")
        fixture = self.project / ".claude" / "settings.json"
        loaded = json.loads(fixture.read_text())
        print(f"  {fixture} (loaded by {GRANT_AGENT}'s runner with --settings; that runner is in dontAsk and allows "
              f"nothing on its command line): permissions {json.dumps(loaded['permissions'])}; hooks "
              + "; ".join(f"{event}{'(' + g['matcher'] + ')' if g.get('matcher') else ''}: {g['hooks'][0]['command']}"
                          for event, groups in loaded["hooks"].items() for g in groups))
        print(f"  {self.plugin} (loaded by the same runner with --plugin-dir): a plugin whose SessionStart hook is "
              f"{json.loads((self.plugin / 'hooks' / 'hooks.json').read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']}")
        ctl, data, _ = self.spawn_child(sid, both("granted-write.txt", "granted-touch.txt"), "--agent", GRANT_AGENT,
                                        "--access", "workspace", "--role", "test", *model)
        mode, tools = self.init_facts(ctl["session_id"])
        print(f"  its process reported permission mode {mode!r}; denied: {data['message']['denials']}; hooks that "
              f"ran for it: {self.hooks_ran(ctl['session_id'])}")
        self.check(data["message"]["state"] == "done" and (self.project / "granted-write.txt").is_file()
                   and (self.project / "granted-touch.txt").is_file() and mode == "dontAsk",
                   "the grants are in force under dontAsk: the workspace child wrote a file with Write and ran "
                   "`touch` with Bash")
        self.check(self.hooks_ran(ctl["session_id"]) == ["SessionStart", "PreToolUse", "plugin SessionStart"],
                   "the hooks are in force too: the settings file's SessionStart hook and PreToolUse hook on Read, "
                   "and the plugin's SessionStart hook, each ran a command for the workspace child")
        argv = self.started_with(ctl["session_id"]) or []
        merged = Path(argv[argv.index("--settings") + 1]) if "--settings" in argv else Path("/nonexistent")
        record, kinds, raw = self.settings_check(ctl["session_id"])
        writes = self.first_writes(ctl["session_id"])
        in_force = self.in_force(ctl["session_id"], self.project)
        print(f"  it was started with --settings {merged} (mode {oct(merged.stat().st_mode & 0o777)}): the fixture "
              f"and permissions.{NO_BYPASS} = {json.loads(merged.read_text())['permissions'].get(NO_BYPASS)!r}")
        print(f"  written to its process, in order: {writes}; its transcript up to the first turn (hook events "
              f"aside): {kinds}; the node's record of the answer: {json.dumps(record)}")
        print(f"  the same command line, asked by this script what is in force, answers permissions "
              f"{json.dumps(in_force)}")
        self.check(argv.count("--settings") == 1 and merged != fixture and self.node_home in merged.parents
                   and (merged.stat().st_mode & 0o777) == 0o600
                   and json.loads(merged.read_text()) == loaded | {"permissions": loaded["permissions"] | {
                       NO_BYPASS: "disable"}} and json.loads(fixture.read_text()) == loaded,
                   "bypass is switched off in it too: Claude takes one --settings, so its node wrote a file (mode "
                   "600) with the runner's settings and the key; the fixture itself is untouched")
        self.check(writes == ["get_settings", "user"] and isinstance(record, dict)
                   and record.get("bypass_disabled") is True and kinds[-1:] == ["system/init"]
                   and "host/control_response_withheld" in kinds[:-1],
                   "its process was asked first and alone whether the key is in force, said yes, and only then was "
                   "the turn written to it")
        self.check(not raw, "the answer itself (every setting in force) is not in the session's transcript: only "
                            "the node's one-line record of it")
        self.check(in_force.get(NO_BYPASS) == "disable"
                   and set(loaded["permissions"]["allow"]) <= set(in_force.get("allow") or []),
                   "... and such a process says both are in force: the fixture's grants and the key")
        gro, data, _ = self.spawn_child(sid, both("readonly-write.txt", "readonly-touch.txt"), "--agent", GRANT_AGENT,
                                        "--access", "read-only", "--role", "review", *model)
        mode, tools = self.init_facts(gro["session_id"])
        print(f"  its process reported permission mode {mode!r} and the built-in tools {tools}; hooks that ran for "
              f"it: {self.hooks_ran(gro['session_id'])}")
        self.check(data["message"]["state"] == "done" and not list(self.tmp.rglob("readonly-write.txt"))
                   and not list(self.tmp.rglob("readonly-touch.txt")),
                   "the read-only child of the SAME runner, asked the same, wrote no file and ran no command: "
                   "neither readonly-write.txt nor readonly-touch.txt exists anywhere in the temp directory")
        self.check(self.hooks_ran(gro["session_id"]) == [],
                   "NO hook ran for the read-only child, at its start or on its Read: it loads no settings, and "
                   "not the runner's plugin directory either")
        argv = self.started_with(gro["session_id"]) or []
        self.check("--plugin-dir" not in argv and str(self.plugin) not in argv and str(fixture) not in argv
                   and argv.count("--settings") == 1
                   and json.loads(argv[argv.index("--settings") + 1]) == {"permissions": {NO_BYPASS: "disable"}},
                   "its command line names no plugin directory and no settings but the key that switches bypass off")
        used = [u.get("name") for u in self.tool_uses(gro["session_id"])]
        self.check((not self.real and "READ README" in data["reply"]) or "Read" in used,
                   "... though it did use the Read tool, which is what the PreToolUse hook is on"
                   + (f" (its tool calls: {used})" if self.real else ""))
        self.check(mode == "dontAsk" and bool(tools) and set(tools) <= {"Read", "Grep", "Glob"},
                   "its process reports dontAsk and no built-in tool but Read, Grep and Glob")
        self.check(gro["access"] == "read-only" and self.show(gro["session_id"]).get("granted") == "read-only",
                   "... and read-only is what its node reported for it")
        if not self.real:
            self.check(data["reply"] == "turn 1: READ README; NO SUCH TOOL: WRITE; NO SUCH TOOL: BASH",
                       "it read the file; neither of the other tools exists for it")

        print("\n[T5] board session list / show")
        listed, _, _ = self.board("session", "list")
        print("\n".join("  | " + line for line in listed.splitlines()))
        lines = listed.splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith(sid))
        under = lines[at + 1:at + 7]
        self.check(len(under) == 6 and all(line.startswith("    ") for line in under)
                   and [line.split()[0] for line in under] == ["review-1", "implementation-1", "research-1", "test-1",
                                                                "test-2", "review-2"],
                   "the six children are indented under their parent, in the order they were started")
        self.check("access read-only, shared" in under[0] and "access workspace, worktree" in under[1]
                   and f"] {CODEX_AGENT}," in under[2] and "effort low" in under[2],
                   "each line names the child's agent, access and isolation")
        shown, _, _ = self.board("session", "show", sid)
        self.check("  children:" in shown and all(c["session_id"] in shown for c in (ro, wt, cdx, cwt, ctl, gro)),
                   "`show` of the parent lists its children")

        print("\n[T6] closing the parent ends its children")
        pids = {c["name"]: self.show(c["session_id"])["pid"] for c in (ro, wt, cdx, cwt, ctl, gro)}
        pids |= {"parent": self.show(sid)["pid"]}
        t0 = time.monotonic()
        out, _, _ = self.board("session", "close", sid)
        print(f"  {out.strip()}")
        ended = [self.wait_ended(x) for x in (sid, *(c["session_id"] for c in (ro, wt, cdx, cwt, ctl, gro)))]
        self.check([v["state"] for v in ended] == ["ended"] * 7,
                   f"the parent and all six children ended ({time.monotonic() - t0:.1f}s after the close)")
        self.check(all("its parent session ended" in (v["error"] or "") for v in ended[1:]),
                   "each child says why: its parent session ended")
        self.check(all(gone(pid) for pid in pids.values() if pid), f"their processes are gone ({pids})")
        shown, _, _ = self.board("session", "show", wt["session_id"])
        line = next(x for x in shown.splitlines() if x.strip().startswith("worktree:"))
        print(f"  {line.strip()}")
        self.check(path.is_dir() and (path / "wt-proof.txt").is_file() and str(path) in line and "it stays there" in line,
                   "the worktree holds an uncommitted file, so it stays, and `show` says where it is")
        deadline = time.monotonic() + 15
        while codex_path.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        removed = self.show(cwt["session_id"])
        self.check(not codex_path.exists() and removed.get("worktree_removed_at") is not None,
                   "the Codex child's worktree held nothing of its own, so its node removed it and told the hub")

        print("\n[T6b] a settings file's defaultMode is bypassPermissions; the session is started with an explicit "
              "mode, so it never takes effect")
        fixture = self.project_mode / ".claude" / "settings.json"
        print(f"  {fixture}: {json.dumps(json.loads(fixture.read_text()))} (loaded by {MODE_AGENT}'s runner with "
              "--settings; that runner names no permission mode and no bypass flag, and this node does not allow "
              "full access)")
        out, err, took = self.board("session", "start", str(self.project_mode), touch("bypass-proof.txt"), "--agent",
                                    MODE_AGENT, *model, "--wait", "--json")
        data = json.loads(out)
        pinned = data["session"]["session_id"]
        view = self.show(pinned)
        mode, tools = self.init_facts(pinned)
        print(f"  session {pinned}; wall {took:.2f}s; its process reported permission mode {mode!r}; reply: "
              f"{data['reply']!r}; denied: {data['message']['denials']}; access {view.get('access')}")
        self.check(mode == "default",
                   "its process reports the standard mode (`default`, which is what `--permission-mode manual` is): "
                   "the explicit mode on its command line beat the settings file's bypassPermissions")
        self.check(view["state"] != "ended" and data["message"]["state"] == "done" and view.get("access") == "workspace",
                   "the session runs on as an ordinary workspace session; its node reported that when it opened it")
        self.check(not list(self.tmp.rglob("bypass-proof.txt")) and "Bash" in data["message"]["denials"],
                   "the command was refused, not run: no bypass-proof.txt anywhere in the temp directory, and the "
                   "turn lists Bash as denied")
        argv = self.started_with(pinned) or []
        merged = Path(argv[argv.index("--settings") + 1]) if "--settings" in argv else Path("/nonexistent")
        record, kinds, raw = self.settings_check(pinned)
        writes = self.first_writes(pinned)
        in_force = self.in_force(pinned, self.project_mode)
        print(f"  started with --permission-mode {argv[argv.index('--permission-mode') + 1]} and --settings {merged}: "
              f"{merged.read_text()}")
        print(f"  written to its process, in order: {writes}; the node's record of the answer: {json.dumps(record)}")
        print(f"  the same command line, asked by this script what is in force, answers permissions "
              f"{json.dumps(in_force)}")
        self.check(argv.count("--settings") == 1 and merged != fixture and self.node_home in merged.parents
                   and json.loads(merged.read_text()) == {"permissions": {"defaultMode": "bypassPermissions",
                                                                          NO_BYPASS: "disable"}},
                   "bypass is switched off for it: its node started it from a file with the fixture's settings and "
                   f"permissions.{NO_BYPASS} = \"disable\"")
        self.check(writes == ["get_settings", "user"] and isinstance(record, dict)
                   and record.get("bypass_disabled") is True and not raw,
                   "its process was asked first and alone whether the key is in force and said yes before the turn "
                   "was written; the transcript keeps the node's record, not the answer")
        self.check(in_force.get(NO_BYPASS) == "disable" and in_force.get("defaultMode") == "bypassPermissions",
                   "such a process says both are in force: the fixture's defaultMode of bypassPermissions, and the "
                   "key")
        answer = self.try_bypass(pinned, self.project_mode)
        print(f"  the same command line, sent a set_permission_mode request for bypassPermissions, answers: "
              f"{json.dumps(answer)}")
        self.check(answer.get("subtype") == "error" and answer.get("error_code") == "bypass_disabled",
                   "an explicit attempt to select bypass is refused by Claude itself (bypass_disabled): the session "
                   "cannot be put into bypass, whatever the settings file's default")
        self.board("session", "close", pinned)
        self.check(self.wait_ended(pinned)["state"] == "ended", "closed")

        print("\n[T6c] the node is restarted with [dispatch.sessions] allow_full_access = true")
        self.node_settings(allow_full=True)
        took = self.restart_node()
        print(f"  node restarted ({took:.1f}s to stop)")

        print("\n[T7] full access: a root whose runner bypasses permission checks")
        out, _, took = self.board("session", "start", str(self.project), first, "--agent", FULL_AGENT, *model,
                                  "--wait", "--json")
        data = json.loads(out)
        full_root = data["session"]["session_id"]
        self.wait_for("the node to report what the root's process said",
                      lambda: self.show(full_root).get("ceiling") == "full", 10)
        view = self.show(full_root)
        argv = self.started_with(full_root) or []
        print(f"  session {full_root}; wall {took:.2f}s; reply: {data['reply']!r}; access {view.get('access')}; its "
              f"process reported permission mode {self.permission_mode(full_root)!r}; a child may have at most: "
              f"{view.get('ceiling')}")
        self.check(view.get("access") == "full", "its node reported its access: full (it allows it: allow_full_access)")
        self.check("--settings" not in argv and self.permission_mode(full_root) == "bypassPermissions"
                   and view.get("ceiling") == "full",
                   "a full session still works: bypass is NOT switched off on its command line, its process reports "
                   "bypassPermissions, and with that a child may be full under it")

        print("\n[T7b] the node is restarted: full counts for a child only once the root's process has said so again")
        took = self.restart_node()
        self.wait_for("the restarted node to report the root",
                      lambda: self.show(full_root).get("ceiling") == "workspace" and self.show(full_root)["pid"] is None)
        view = self.show(full_root)
        shown, _, _ = self.board("session", "show", full_root)
        line = next(x.strip() for x in shown.splitlines() if x.strip().startswith("access "))
        print(f"  node restarted ({took:.1f}s to stop); the root has no process; show: {line}")
        self.check(view.get("access") == "full" and view.get("ceiling") == "workspace" and "not confirmed" in line,
                   "the root is still a full session (what it is started as), and nothing has confirmed that in "
                   "this node process: a child can have at most workspace")
        out, err, _ = self.board("session", "start", "--parent", full_root, touch("too-early.txt"), ok=False)
        print(f"  start --parent (at the root's own access, full): {err.strip().splitlines()[-1][:230]}")
        self.check("its process has not confirmed that" in err and "children" not in self.show(full_root)
                   and not list(self.tmp.rglob("too-early.txt")),
                   "a full child is refused meanwhile: nothing was made, nothing ran")
        out, _, took = self.board("session", "send", full_root, first, "--wait", "--json")
        self.wait_for("the node to report what the root's new process said",
                      lambda: self.show(full_root).get("ceiling") == "full", 10)
        print(f"  the root's next turn: wall {took:.2f}s; its new process reported permission mode "
              f"{self.permission_mode(full_root)!r}; a child may have at most: {self.show(full_root).get('ceiling')}")
        self.check(json.loads(out)["message"]["state"] == "done" and self.show(full_root).get("ceiling") == "full",
                   "after the root's next turn its process has reported bypassPermissions: a child may be full again")
        ws, data, _ = self.spawn_child(full_root, touch("touch-workspace.txt"), "--agent", AGENT, "--access", "workspace",
                                       "--role", "test", *model)
        self.check(data["message"]["state"] == "done" and not list(self.tmp.rglob("touch-workspace.txt")),
                   "the workspace child (a dontAsk runner) did not run the command: no touch-workspace.txt")
        print(f"  tools denied in its turn: {data['message']['denials']}")
        self.check("Bash" in data["message"]["denials"], "... and its turn lists Bash as denied")
        fc, data, _ = self.spawn_child(full_root, touch("touch-full.txt"), "--role", "test", *model)
        self.check(fc["access"] == "full" and fc["agent"] == FULL_AGENT, "the other child has its parent's access: full")
        self.check(data["message"]["state"] == "done" and (self.project / "touch-full.txt").is_file()
                   and data["message"]["denials"] == [],
                   "the full child ran the same kind of command: touch-full.txt is in the checkout, nothing denied")
        modes = {x["name"]: self.permission_mode(x["session_id"]) for x in (ws, fc)}
        print(f"  permission modes the processes reported: {modes}")
        self.check(modes == {"test-1": "dontAsk", "test-2": "bypassPermissions"},
                   "the workspace child ran in dontAsk, the full child in bypassPermissions")
        before = len(self.codex_stdin())
        fx, data, _ = self.spawn_child(full_root, pong, "--agent", CODEX_AGENT, "--model", self.args.codex_model,
                                       "--access", "full", "--role", "research")
        start = next(m["params"] for m in self.codex_stdin()[before:] if m.get("method") == "thread/start")
        said = None
        for path in self.node_home.rglob(f"{fx['session_id']}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    result = json.loads(line).get("result")
                except (ValueError, AttributeError):
                    continue
                if isinstance(result, dict) and isinstance(result.get("thread"), dict):
                    said = result.get("sandbox")
        print(f"  thread/start: {json.dumps(start)}; Codex answered that the thread's sandbox is {json.dumps(said)}")
        self.check(data["message"]["state"] == "done" and bool(data["reply"].strip()) and fx["access"] == "full"
                   and self.show(fx["session_id"]).get("granted") == "full" and "cwd" not in start
                   and start.get("sandbox") == "danger-full-access",
                   "a Codex child at full under the full root: its thread was started with danger-full-access (and "
                   "no cwd), and it took its turn")
        self.check(isinstance(said, dict) and said.get("type") == "dangerFullAccess",
                   "Codex's answer to thread/start names that sandbox: what confirms a full Codex session")
        kids = (ws, fc, fx)
        pids = [self.show(x)["pid"] for x in (full_root, *(k["session_id"] for k in kids))]
        self.board("session", "close", full_root)
        ended = [self.wait_ended(x) for x in (full_root, *(k["session_id"] for k in kids))]
        self.check([v["state"] for v in ended] == ["ended"] * 4 and all(gone(pid) for pid in pids if pid),
                   "closing the root ended all three children; their processes are gone")

        print("\n[T7c] a runner Claude would refuse to start (--restricted beside the bypass mode) is refused when "
              "the session is opened")
        before = len(Path(str(self.stdin_log) + ".argv").read_text().splitlines())
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", RESTRICTED_AGENT, "--json")
        refused = self.wait_ended(json.loads(out)["session"]["session_id"])
        print(f"  {refused['session_id']} [{refused['state']}]: {refused['error']}")
        self.check(refused["state"] == "ended" and "--restricted" in (refused["error"] or "")
                   and "not supported in restricted mode" in refused["error"] and refused["pid"] is None
                   and len(Path(str(self.stdin_log) + ".argv").read_text().splitlines()) == before,
                   "its node refused it with the reason; no process was started for it, so nothing was ever taken "
                   "for a full session that Claude would not have run as one")
        listed, _, _ = self.board("session", "list")
        self.check(listed.strip() == "(no hosted sessions)", "no hosted session is left")

    def results_of(self, sid: str, kids: list[str], seconds: float = 120.0) -> tuple[dict, list[dict], dict]:
        """Once every result of these children has been delivered to the parent `sid` (the parent's own process
        said the turn carrying it started) and each such turn is over: (the parent's view, those results, the
        parent's messages by number)."""
        deadline = time.monotonic() + seconds
        while True:
            view = self.show(sid)
            res = [x for x in view.get("results", []) if x["child"] in kids]
            msgs = {m["n"]: m for m in view["messages"]}
            if (len(res) == len(kids) and all(x["state"] == "delivered" for x in res)
                    and all(msgs.get(x["carrier"], {}).get("state") in ("done", "failed") for x in res)):
                return view, res, msgs
            if time.monotonic() >= deadline:
                raise SystemExit(f"FAIL: the results of {kids} did not reach session {sid} as a turn within "
                                 f"{seconds:g}s: {json.dumps(res)}")
            time.sleep(0.2)

    def carriers(self, kid: str) -> list[str]:
        """The turns written to any Claude process that carry a result of this child (its id is in the server's
        line), from the wrapper's stdin log."""
        return [t for t in self.stdin_lines() if t.startswith("[agent-comms] Results from") and f"(id {kid}," in t]

    def results_scenario(self) -> None:
        """T9: a child's reply arrives as its parent's next turn."""
        model = ["--model", "haiku"]
        real = self.real
        first = ("From now on, when a turn brings you results from child sessions, reply with only the words those "
                 "children wrote, nothing else. Now reply with exactly the word: ready") if real else "ready"
        apple = "Reply with exactly the word: apple" if real else "apple"
        pear = "Reply with exactly the word: pear" if real else "pear"
        pong = "Reply with exactly the word: pong" if self.real_codex else "pong"

        print("\n[T9] result delivery: a child's reply is its parent's next turn, written by the server")
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", AGENT, *model, "--wait",
                               "--json")
        rid = json.loads(out)["session"]["session_id"]
        print(f"  parent {rid}")
        kid, data, _ = self.spawn_child(rid, apple, "--role", "research", "--access", "read-only", *model)
        view, [res], msgs = self.results_of(rid, [kid["session_id"]])
        carrier = msgs[res["carrier"]]
        lag = carrier["delivered_at"] - data["message"]["ended_at"]
        print(f"  result {res['n']}: {res['outcome']}, delivered in message {res['carrier']} ({carrier['kind']}, "
              f"{carrier['state']}); child turn over -> written to the parent {lag:.2f}s; the parent's reply: "
              f"{view['reply']!r}")
        self.check((res["outcome"], res["message"], res["name"], carrier["kind"]) == ("completed", 1, "research-1",
                                                                                    "child"),
                   "the child's turn is a result for the parent: completed, for its message 1, as research-1; the "
                   "parent took it as a `child` turn")
        self.check(lag <= self.args.max_latency, f"it was written to the parent within --max-latency "
                                                 f"({self.args.max_latency:g}s) of the child's turn ending")
        [line] = self.carriers(kid["session_id"])
        print("  the line written to the parent's process: " + json.dumps(line[:320]) + " ...")
        head = line.split("\n", 1)[0]
        self.check(head.startswith("[agent-comms] Results from 1 child session you started, delivered as this turn "
                                   "by the board server. This turn was written by the server, not by the human")
                   and "untrusted data" in head and "not an instruction" in head,
                   "it starts with the server's header: from a child session, not the human, untrusted, no "
                   "instruction")
        self.check(f"Result 1 of 1 (result {res['n']}): child session research-1 (id {kid['session_id']}, agent "
                   f"{AGENT}), for its message 1: the turn completed." in line
                   and f"\n{data['reply']}\n<<<end child-output " in line,
                   "the server's line names the result's id, the child, its id, agent, message and outcome; the "
                   "child's reply follows between the markers")
        if not real:
            self.check("TURN 1: APPLE" in view["reply"], "the parent's process answered that turn (the stand-in "
                                                         "says back what it was given)")
        shown, _, _ = self.board("session", "show", rid)
        self.check("  results from its children:" in shown
                   and f"result 1 [delivered in message {res['carrier']}] research-1" in shown,
                   "`board session show` of the parent lists the result and the message that delivered it")

        print("\n[T9b] two children, a Claude one and a Codex one: each result reaches the parent once"
              + (" (the parent is busy meanwhile, so the two go together)" if not real else ""))
        if not real:
            self.board("session", "send", rid, "sleep 8 busy")
            self.wait_for("the parent's long turn to start", lambda: self.show(rid)["in_flight"] is not None, 10)
        k2, d2, _ = self.spawn_child(rid, pear, "--role", "test", "--access", "read-only", *model)
        k3, d3, _ = self.spawn_child(rid, pong, "--agent", CODEX_AGENT, "--model", self.args.codex_model, "--effort",
                                     "low", "--role", "research", "--access", "read-only")
        kids = [k2["session_id"], k3["session_id"]]
        view, res, msgs = self.results_of(rid, kids)
        print(f"  results: {[(x['name'], x['outcome'], x['carrier']) for x in res]}; the parent's reply: "
              f"{view['reply']!r}")
        self.check([x["outcome"] for x in res] == ["completed", "completed"]
                   and all(msgs[x["carrier"]]["kind"] == "child" for x in res),
                   "both children's turns reached the parent as turns of its own")
        self.check(all(len(self.carriers(k)) == 1 for k in kids),
                   "each result was written to the parent once, and never again")
        if not real:
            [line] = self.carriers(kids[0])
            self.check(res[0]["carrier"] == res[1]["carrier"]
                       and f"Result 1 of 2 (result {res[0]['n']}): child session test-1" in line
                       and f"Result 2 of 2 (result {res[1]['n']}): child session research-2" in line
                       and f"agent {CODEX_AGENT})" in line,
                       "they went together, as ONE turn, after the parent's long turn: the Claude child's first, then "
                       "the Codex child's")
            out, _, _ = self.board("session", "start", "--parent", rid, "fail on purpose", "--role", "test",
                                   "--access", "read-only", "--wait", "--json", ok=False)     # exits 1: the turn failed
            k4 = json.loads(out)["session"]["session_id"]
            _, [r4], _ = self.results_of(rid, [k4])
            [line] = self.carriers(k4)
            self.check(r4["outcome"] == "failed" and "for its message 1: the turn failed." in line
                       and "Reason, as its node reported it (untrusted): " in line,
                       "a failed turn is a result too, with the outcome and the node's reason, quoted")
        else:
            print(f"  carried in message(s) {sorted({x['carrier'] for x in res})} (with real agents the timing decides "
                  "whether they go together)")

        self.board("session", "close", rid)
        self.check(self.wait_ended(rid)["state"] == "ended", "closing the parent ends it and its children")
        listed, _, _ = self.board("session", "list")
        self.check(listed.strip() == "(no hosted sessions)", "no hosted session is left")

    def bind_key(self, sid: str) -> str:
        r = subprocess.run([sys.executable, "-c", BIND_KEY, sid], cwd=str(self.tmp), capture_output=True, text=True,
                           timeout=60, env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return r.stdout.strip()

    def mcp_calls(self, agent: str, calls: list, hosted: str | None = None) -> list[dict]:
        """Board tool calls through a real `board mcp` process as `agent`: with hosted session `hosted`'s binding
        in its environment (as that session's own board server has it), or with none (an interactive session's)."""
        env = self.env | {"AGENT_COMMS_HOME": str(self.hub_home), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens")}
        if hosted is not None:
            env |= {"AGENT_COMMS_HOSTED_SESSION": hosted, "AGENT_COMMS_HOSTED_KEY": self.bind_key(hosted)}
        r = subprocess.run([sys.executable, "-c", MCP_CALLS, agent, json.dumps(calls)], cwd=str(self.tmp),
                           capture_output=True, text=True, timeout=120, env=env)
        if r.returncode != 0:
            raise SystemExit(f"the board MCP calls failed:\n{r.stderr[-2000:]}")
        return json.loads(r.stdout)

    def tree_thread_posts(self, root: str) -> list[dict]:
        """The posts of a tree's audit thread, read by another process on the hub's database."""
        code = ("import json, sys\nfrom agent_comms import hosted_spawn\nfrom agent_comms.config import Settings\n"
                "from agent_comms.core import Board\nb = Board(Settings.load())\n"
                "t = hosted_spawn._tree(b.conn, sys.argv[1])\n"
                "rows = [dict(r) for r in b.conn.execute('SELECT id, agent, type, body FROM posts WHERE thread_id = ?"
                " ORDER BY id', (t['thread'],))] if t['thread'] else []\n"
                "auto = [r['id'] for r in rows if b.conn.execute(\"SELECT 1 FROM board_state WHERE key = ?\", "
                "('auto_recovery.post.' + str(r['id']),)).fetchone()]\n"
                "print(json.dumps({'posts': rows, 'automatic': auto}))")
        r = subprocess.run([sys.executable, "-c", code, root], cwd=str(self.tmp), capture_output=True, text=True,
                           timeout=60, env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return json.loads(r.stdout)

    # ------------------------------------------------------------ structured results (DESIGN_NOTES "Structured results")

    def board_rc(self, *argv: str) -> tuple[int, str, str]:
        """`board ...` with its exit status (for the exit codes 3 and 4 of `--wait` with `--output-schema`)."""
        r = subprocess.run([sys.executable, "-m", "agent_comms.cli", *argv], cwd=str(self.tmp), capture_output=True,
                           text=True, timeout=300,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home), "BOARD_TOKEN": self.human_token})
        return r.returncode, r.stdout, r.stderr

    def review_files(self) -> tuple[Path, Path, str, str]:
        """The review schema and an unsatisfiable one (minLength above maxLength), as files; the review task."""
        review = self.tmp / "review-schema.json"
        review.write_text(json.dumps(REVIEW_SCHEMA))
        unsat = self.tmp / "unsat-schema.json"
        unsat.write_text(json.dumps(UNSAT_SCHEMA))
        task = REVIEW_TASK if self.real else "data-valid"
        codex_task = REVIEW_TASK if self.real_codex else "data-valid"
        return review, unsat, task, codex_task

    def final_reply(self, sid: str) -> str | None:
        """The session's last turn's reply as its node's transcript holds it (what the process wrote), capped as the
        adapter caps it: Claude's last `result`, Codex's last final-answer agent message."""
        from agent_comms.providers import cap_reply
        text = None
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "result" and isinstance(ev.get("result"), str):
                    text = ev["result"]
                item = (ev.get("params") or {}).get("item") if ev.get("method") == "item/completed" else None
                if (isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str)
                        and item.get("phase") in ("final_answer", None)):
                    text = item["text"]
        return cap_reply(text) if text is not None else None

    def check_data(self, label: str, data: dict, rc: int) -> None:
        from agent_comms import output_schema
        out = (data.get("message") or {}).get("output") or {}
        got = data.get("result_data")
        reasons = output_schema.validate(REVIEW_SCHEMA, got) if isinstance(got, dict) else ["no result_data"]
        print(f"  {label}: exit {rc}; verdict {out.get('state')} {out.get('code') or ''}; result_data "
              f"{json.dumps(got)[:200]}")
        self.check(rc == 0 and out.get("state") == "accepted" and not reasons,
                   f"{label}: the verdict is accepted, exit 0, and the derived result_data fits the review schema")
        planted = [f for f in (got or {}).get("findings") or [] if f.get("file") == "src/pager.py"
                   and f.get("line") == 3]
        self.check(got.get("verdict") == "changes" and bool(planted),
                   f"{label}: the review found the planted off-by-one (src/pager.py, line 3)")
        written = self.final_reply(data["session"]["session_id"])
        self.check(written is not None and data.get("reply") == written,
                   f"{label}: the reply is exactly the session's own capped reply, block included")

    def check_carrier(self, label: str, kid: str, reply: str) -> None:
        """The parent's actual turn that carried this child's result: the one hub line right above the child's exact
        reply between the turn's child-output markers."""
        from agent_comms import output_schema
        lines = self.carriers(kid)
        turn = lines[-1] if lines else ""
        token = turn.split("<<<child-output ", 1)[1][:16] if "<<<child-output " in turn else ""
        paired = (f"{output_schema.ACCEPTED_LINE}\n<<<child-output {token}>>>\n{reply}\n<<<end child-output "
                  f"{token}>>>")
        self.check(len(lines) == 1 and turn.count(output_schema.ACCEPTED_LINE) == 1 and paired in turn,
                   f"{label}: the parent's own turn carried exactly one hub line, right above the child's exact reply")

    def wait_delivered(self, sroot: str, kid: str, seconds: float = 240.0) -> None:
        deadline, mine = time.monotonic() + seconds, []
        while time.monotonic() < deadline:
            parent = self.show(sroot)
            mine = [x for x in parent.get("results") or [] if x.get("child") == kid]
            if mine and mine[-1]["state"] == "delivered" and parent.get("in_flight") is None:
                break
            time.sleep(0.5)
        verdict = (mine[-1].get("output") if mine else None) or {}
        print(f"  delivered as the parent's turn: result {mine[-1]['n'] if mine else None}, verdict {verdict}")
        self.check(bool(mine) and mine[-1]["state"] == "delivered" and verdict.get("state") == "accepted",
                   "D6: the result reached the parent as a turn, its verdict accepted")

    def structured_off_scenario(self) -> None:
        """D1-D2: a result as data from a hosted root, with child sessions OFF (as shipped)."""
        review, _, task, _ = self.review_files()
        model = ["--model", "haiku"]
        print("\n[D1] a hosted root asked for a result as data, child sessions off")
        for s in json.loads(self.board("session", "list", "--json")[0])["sessions"]:
            self.board("session", "close", s["session_id"])
            self.wait_ended(s["session_id"])
        rc, out, err = self.board_rc("session", "start", str(self.project), task, "--agent", AGENT, *model,
                                     "--output-schema", str(review), "--wait", "--json")
        if rc not in (0, 3, 4):
            raise SystemExit(f"start --output-schema exited {rc}:\n{out}{err}")
        data = json.loads(out)
        sid = data["session"]["session_id"]
        self.check_data("D1 root", data, rc)
        print("\n[D2] the same session asked again, told to leave the block out: missing, exit 3")
        none = ("Reply with only the word: skipped. Do not write any result-data block in this reply."
                if self.real else "data-none")
        rc, out, err = self.board_rc("session", "send", sid, none, "--output-schema", str(review), "--wait", "--json")
        data = json.loads(out)
        out_ = data["message"].get("output") or {}
        print(f"  exit {rc}; verdict {out_.get('state')} ({out_.get('code')}); reply {data.get('reply')!r}")
        self.check(rc == 3 and out_.get("state") == "missing" and out_.get("code") == "no_block"
                   and "result_data" not in data, "D2: no block, so missing (no_block), exit 3, and no result_data")
        rc, out, err = self.board_rc("session", "send", sid, "done" if not self.real else
                                     "Reply with exactly the word: done", "--wait")
        self.check(rc == 0 and "result as data" not in err, "a message without a schema: exit and output as ever")
        self.board("session", "close", sid)
        self.wait_ended(sid)
        if not self.real:
            self.check_off_scenario(review)

    def check_off_scenario(self, review: Path) -> None:
        """D7 with child sessions OFF (as shipped): a human root's held turn is checked through a real `board mcp`
        process as its agent (no binding, no grant, no child sessions: none is needed), and settles accepted."""
        from agent_comms import output_schema
        print("\n[D7] the in-turn check with child sessions off: a human root")
        token_file = self.tmp / "tokens" / f"{AGENT}.token"
        if not token_file.exists():
            token_file.touch(mode=0o600)
            token_file.write_text(self.agent_token + "\n")
        hold = self.tmp / "d7-off-release"
        out, _, _ = self.board("session", "start", str(self.project), f"hold {hold} data-valid", "--agent", AGENT,
                               "--output-schema", str(review), "--json")
        sid = json.loads(out)["session"]["session_id"]
        self.message(sid, 1, lambda m: m["state"] == "delivered", 60)
        t = self.asked_token(sid, 1)
        good = {"verdict": "changes", "findings": [{"file": "src/pager.py", "line": 3, "note": "off by one"}]}
        [answer] = self.mcp_calls(AGENT, [[output_schema.CHECK_TOOL, {
            "block": f"Reviewed.\n<<<result-data {t}>>>\n{json.dumps(good)}\n<<<end result-data {t}>>>"}]])
        got = json.loads(answer["text"])
        print(f"  check: {got.get('would_be')}")
        self.check(got.get("would_be") == "accepted", "D7 (spawning off): the human root's block is checked: accepted")
        hold.write_text("go")
        m = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 60)
        self.check(((m or {}).get("output") or {}).get("state") == "accepted",
                   "D7 (spawning off): the turn settled accepted, from its reply")
        self.board("session", "close", sid)
        self.wait_ended(sid)

    def structured_scenario(self) -> None:
        """D3-D6: children asked for a result as data (child sessions on)."""
        from agent_comms import output_schema
        review, unsat, task, codex_task = self.review_files()
        model = ["--model", "haiku"]
        first = "Reply with exactly the word: ready" if self.real else "ready"
        print("\n[D3] a stock read-only Claude child (its runner names no MCP config: it has no board tools)")
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", AGENT, *model, "--wait",
                               "--json")
        root = json.loads(out)["session"]["session_id"]
        rc, out, err = self.board_rc("session", "start", "--parent", root, "--access", "read-only", *model,
                                     "--output-schema", str(review), task, "--wait", "--json")
        data = json.loads(out)
        kid = data["session"]["session_id"]
        argv = self.started_with(kid) or []
        self.check("--mcp-config" not in argv and "--tools=Read,Grep,Glob" in argv,
                   "D3: the child is the stock read-only one: no MCP config at all, the read tools only")
        self.check_data("D3 read-only Claude child", data, rc)
        print("\n[D4] an unedited Codex child")
        rc, out, err = self.board_rc("session", "start", "--parent", root, "--agent", CODEX_AGENT,
                                     "--output-schema", str(review), codex_task, "--wait", "--json")
        self.check_data("D4 Codex child", json.loads(out), rc)
        print("\n[D5] an unsatisfiable schema first (missing), then the review schema asked again (accepted)")
        rc, out, err = self.board_rc("session", "start", "--parent", root, "--access", "read-only", *model,
                                     "--output-schema", str(unsat), task, "--wait", "--json")
        data = json.loads(out)
        kid5 = data["session"]["session_id"]
        out_ = data["message"].get("output") or {}
        print(f"  exit {rc}; verdict {out_.get('state')} ({out_.get('code')}); reasons {out_.get('reasons')}")
        self.check(rc == 3 and out_.get("state") == "missing" and out_.get("code") in ("no_block", "shape")
                   and (self.real or out_.get("code") == "shape"),
                   "D5: nothing can fit, so missing (no_block or shape; the stand-in: exactly shape), exit 3")
        again = ("Now review the same diff again and end your reply with the result-data block as asked."
                 if self.real else "data-valid")
        rc, out, err = self.board_rc("session", "send", kid5, again, "--output-schema", str(review), "--wait",
                                     "--json")
        self.check_data("D5 asked again", json.loads(out), rc)
        print("\n[D6] an agent asks through board_session_spawn: the claim's facts, and the parent's own turn")
        self.board("session", "grant", str(self.project), "--agent", SPAWN_AGENT, "--access", "workspace",
                   "--hours", "1")
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", SPAWN_AGENT, *model,
                               "--wait", "--json")
        sroot = json.loads(out)["session"]["session_id"]
        facts_keys = {"n", "child", "name", "agent", "message", "started_by", "outcome", "ok", "denials", "attempts",
                      "output"}
        hold = self.tmp / "d6-release"
        if not self.real:
            # The claim branch, deterministically: the parent is busy (a held turn), so the child's result waits for
            # the call that waits for it.
            self.board("session", "send", sroot, f"hold {hold}")
        [spawned] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
            "task": task, "child_agent": AGENT, "access": "read-only", "output_schema": REVIEW_TEXT, "wait": True}]],
            sroot)
        answer = json.loads(spawned["text"])
        self.check(not spawned["error"] and answer.get("output_request", {}).get("stored") is True,
                   "D6: the tool acknowledges the request (output_request.stored)")
        res, kid6 = answer.get("result"), answer["child"]["id"]
        if not self.real:
            self.check(res is not None, "D6: with the parent busy, the waiting call took the result")
            hold.write_text("go")
        if res is not None:
            facts, text = res["result"], res.get("text") or ""
            reply = self.final_reply(kid6)
            print(f"  claimed with wait=true: facts {json.dumps(facts)[:300]}")
            self.check(set(facts) == facts_keys and facts["output"] == {
                "state": "accepted", "schema_sha": output_schema.prepare_schema(REVIEW_TEXT)[2]},
                "D6: the claim's facts are the whitelist: main's keys plus `output` (state and fingerprint only)")
            token = text.split("<<<child-output ", 1)[1][:16]
            self.check(text.count(output_schema.ACCEPTED_LINE) == 1 and
                       f"{output_schema.ACCEPTED_LINE}\n<<<child-output {token}>>>\n{reply}\n<<<end child-output "
                       f"{token}>>>" in text, "D6: the claim's text has the one hub line right above the exact reply")
        else:
            self.wait_delivered(sroot, kid6)
            self.check_carrier("D6 (delivered as the parent's turn)", kid6, self.final_reply(kid6))
        # The delivered branch, deterministically, in a real run too (R5-06): the parent idle, nobody waiting, so no
        # run passes on the claim branch alone.
        [spawned] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
            "task": task, "child_agent": AGENT, "access": "read-only", "output_schema": REVIEW_TEXT}]], sroot)
        kid7 = json.loads(spawned["text"])["child"]["id"]
        self.wait_delivered(sroot, kid7)
        self.check_carrier("D6 (delivered as the parent's turn)", kid7, self.final_reply(kid7))
        for s in (root, sroot):
            self.board("session", "close", s)
            self.wait_ended(s)

    # ------------------------------------------------------------ the in-turn check (DESIGN_NOTES "In-turn result check")

    def asked_token(self, sid: str, n: int) -> str:
        """The token of message n's request, as the hub stored it (what the session's frame shows), read by another
        process on the hub's database."""
        code = ("import json, sys\nfrom agent_comms import hosted\nfrom agent_comms.config import Settings\n"
                "from agent_comms.core import Board\nfrom agent_comms.nodes import _get\nb = Board(Settings.load())\n"
                "print(_get(b.conn, hosted._msg_key(sys.argv[1], int(sys.argv[2])))['output']['token'])")
        r = subprocess.run([sys.executable, "-c", code, sid, str(n)], cwd=str(self.tmp), capture_output=True,
                           text=True, timeout=60, env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return r.stdout.strip()

    def check_calls(self, sid: str) -> list[str]:
        """The check calls in a session's transcript on its node, in order, each as the board's PARSED answer:
        `accepted`, `missing:<code>` (a shipped code), `not-checked`, `refused` (Codex declined the call: no
        approval), or `?` (an answer that does not parse as the check's). Claude: each `tool_use` of the check is
        matched to its `tool_result` by id; Codex: the check's `mcpToolCall` item and its result."""
        out, names = [], {}
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "assistant":
                    for part in (ev.get("message") or {}).get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            names[part.get("id")] = part.get("name") or ""
                if ev.get("type") == "user":            # Claude: the tool's answer comes back as a tool_result
                    for part in (ev.get("message") or {}).get("content") or []:
                        if (isinstance(part, dict) and part.get("type") == "tool_result"
                                and names.get(part.get("tool_use_id"), "").endswith("__board_session_check_result")):
                            out.append(self.answer_word(part.get("content")))
                item = (ev.get("params") or {}).get("item") if ev.get("method") == "item/completed" else None
                if isinstance(item, dict) and item.get("type") == "mcpToolCall" and \
                        item.get("tool") == "board_session_check_result":
                    out.append("refused" if item.get("status") != "completed" else
                               self.answer_word((item.get("result") or {}).get("content")))
        return out

    @staticmethod
    def answer_word(content) -> str:
        """The check's answer from a tool result's content (a string, or a list of text parts), parsed as JSON."""
        from agent_comms import output_schema
        texts = [content] if isinstance(content, str) else [
            p.get("text") for p in content or [] if isinstance(p, dict) and isinstance(p.get("text"), str)]
        for text in texts:
            try:
                answer = json.loads(text)
            except (TypeError, ValueError):
                continue
            if not isinstance(answer, dict) or answer.get("result_saved") is not False:
                continue
            if answer.get("checked") is True and answer.get("would_be") == "accepted":
                return "accepted"
            if answer.get("checked") is True and answer.get("would_be") == "missing" and \
                    answer.get("code") in output_schema.CODES:
                return "missing:" + answer["code"]
            if answer.get("checked") is False:
                return "not-checked"
        return "?"

    @staticmethod
    def validated(seen: list[str]) -> bool:
        """At least one check reached the validator and was answered with a verdict (not `?`, not refused, not
        `not-checked`)."""
        return any(x == "accepted" or x.startswith("missing:") for x in seen)

    def transcript_tools(self, sid: str) -> tuple[list[str], list[str]]:
        """(the board tools the session's process listed in its last `init`, the tools it called), from its node's
        transcript: what a check scenario prints, to tell "not called" from "not there"."""
        listed, called = [], []
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    listed = [t for t in ev.get("tools") or [] if isinstance(t, str) and t.startswith("mcp__")]
                if ev.get("type") == "assistant":
                    called += [p.get("name") for p in (ev.get("message") or {}).get("content") or []
                               if isinstance(p, dict) and p.get("type") == "tool_use"]
                item = (ev.get("params") or {}).get("item") if ev.get("method") == "item/completed" else None
                if isinstance(item, dict) and item.get("type") == "mcpToolCall":
                    called.append(f"{item.get('server')}/{item.get('tool')} ({item.get('status')})")
        return listed, called

    def check_scenario(self) -> None:
        """D7-D8: the in-turn check. D7 with the stand-ins is the correction gate, deterministically: a block that
        does not fit is checked (missing, shape, exact reasons), the fixed one is checked (accepted), and the turn
        settles accepted; a check that was accepted and then an omitted block settles missing (nothing was saved as
        a result: the durable-state comparison itself is tests/test_result_check.py's). With real CLIs: a root with
        board tools and a Codex child whose runner approves the tool, each told in the task to check a broken
        draft: a check answered by the validator and an accepted settlement are REQUIRED; the frame's sentence
        alone, and whether the first draft needed correction, are RECORDED. D8: the approval denial and `waiting`
        of a Codex runner that lists the tool but does not approve it, deterministically with the stand-in (with
        the real Codex: recorded), and the frames."""
        from agent_comms import output_schema
        review = self.tmp / "review-schema.json"
        review.write_text(json.dumps(REVIEW_SCHEMA))
        strict = self.tmp / "check-schema.json"
        strict.write_text(json.dumps(CHECK_SCHEMA))
        model = ["--model", "haiku"]
        print("\n[D7] the in-turn check: reject, fix, settle accepted; and an accepted check, then no block")
        if not self.real:
            for kind, want in (("valid", "accepted"), ("none", "missing")):
                hold = self.tmp / f"d7-release-{kind}"
                out, _, _ = self.board("session", "start", str(self.project), f"hold {hold} data-{kind}", "--agent",
                                       SPAWN_AGENT, "--output-schema", str(review), "--json")
                sid = json.loads(out)["session"]["session_id"]
                self.message(sid, 1, lambda m: m["state"] == "delivered", 60)
                t = self.asked_token(sid, 1)
                bad = {"verdict": "maybe", "findings": [{"file": "src/pager.py", "line": "3", "note": "x" * 501}]}
                good = {"verdict": "changes", "findings": [{"file": "src/pager.py", "line": 3, "note": "off by one"}]}
                block = "Reviewed.\n<<<result-data {t}>>>\n{v}\n<<<end result-data {t}>>>"
                calls = self.mcp_calls(SPAWN_AGENT, [
                    [output_schema.CHECK_TOOL, {"block": block.format(t=t, v=json.dumps(bad))}],
                    [output_schema.CHECK_TOOL, {"block": block.format(t=t, v=json.dumps(good))}]], sid)
                first, second = (json.loads(c["text"]) for c in calls)
                print(f"  {kind}: first check {first.get('would_be')} {first.get('reasons')}; second "
                      f"{second.get('would_be')}")
                self.check(first.get("would_be") == "missing" and first.get("code") == "shape" and first["reasons"] == [
                    "at $.verdict: not one of the enum's values", "at $.findings[0].line: expected integer, got string",
                    "at $.findings[0].note: longer than maxLength 500: it has 501 characters"],
                    f"D7 ({kind}): the block that does not fit is missing (shape), with the settlement's exact reasons")
                self.check(second == {"checked": True, "would_be": "accepted", "result_saved": False,
                                      "next": output_schema.CHECK_FITS}, f"D7 ({kind}): the fixed block is accepted")
                hold.write_text("go")
                m = self.message(sid, 1, lambda m: m["state"] in ("done", "failed"), 60)
                verdict = (m or {}).get("output") or {}
                print(f"  {kind}: settled {verdict.get('state')} {verdict.get('code') or ''}")
                self.check(verdict.get("state") == want and (want == "accepted" or verdict.get("code") == "no_block"),
                           f"D7 ({kind}): the verdict is the reply's: {want}"
                           + ("" if want == "accepted" else " (no_block): an accepted check is not a result"))
                self.board("session", "close", sid)
                self.wait_ended(sid)
        else:
            # The frame's sentence alone (the task does not name the tool): RECORDED, since that step is optional by
            # construction. Told in the task too: a check answered by the validator, and an accepted settlement,
            # REQUIRED.
            for how, text, required in (("the frame only", CHECK_TASK, False),
                                        ("told in the task too", CHECK_TASK + CHECK_TELL, True)):
                out, _, _ = self.board("session", "start", str(self.project), text, "--agent", SPAWN_AGENT, *model,
                                       "--output-schema", str(strict), "--wait", "--json", ok=False)
                data = json.loads(out)
                sid = data["session"]["session_id"]
                seen = self.check_calls(sid)
                verdict = data["message"].get("output") or {}
                D7_RECORD.append({"who": f"Claude root (board tools), {how}", "checks": seen,
                                  "verdict": verdict.get("state"), "code": verdict.get("code")})
                listed, called = self.transcript_tools(sid)
                print(f"  Claude root, {how}: checks {seen}; verdict {verdict.get('state')} "
                      f"{verdict.get('code') or ''}; tools called {called}")
                if required:
                    self.check(self.validated(seen) and verdict.get("state") == "accepted",
                               "D7 (Claude, board tools, told in the task): a check answered by the validator, and "
                               "the turn settled accepted")
                print(f"  recorded: {'no check' if not seen else 'the first draft needed correction' if seen[0] != 'accepted' else 'the first check was accepted'}")
                self.board("session", "close", sid)
                self.wait_ended(sid)
        if self.real_codex:
            out, _, _ = self.board("session", "start", str(self.project), "Reply with exactly the word: ready",
                                   "--agent", AGENT, *model, "--wait", "--json")
            root = json.loads(out)["session"]["session_id"]
            rc, out, err = self.board_rc("session", "start", "--parent", root, "--agent", CHECK_CODEX,
                                         "--output-schema", str(strict), CHECK_TASK + CHECK_TELL, "--wait", "--json")
            data = json.loads(out)
            kid = data["session"]["session_id"]
            seen = self.check_calls(kid)
            verdict = data["message"].get("output") or {}
            D7_RECORD.append({"who": "Codex child (approved)", "checks": seen, "verdict": verdict.get("state")})
            print(f"  Codex child (approved): checks {seen}; verdict {verdict.get('state')} "
                  f"{verdict.get('code') or ''}; tools called {self.transcript_tools(kid)[1]}")
            self.check(self.validated(seen) and "refused" not in seen and verdict.get("state") == "accepted",
                       "D7 (Codex, approved, told in the task): a check answered by the validator, and the turn "
                       "settled accepted")
            print(f"  recorded: the first draft {'needed' if seen and seen[0] != 'accepted' else 'did not need'} "
                  "correction")
            print("\n[D8] a Codex runner that lists the check tool but does not approve it (recorded)")
            rc, out, err = self.board_rc("session", "start", "--parent", root, "--agent", DENIED_CODEX,
                                         "--output-schema", str(strict), CHECK_TASK + CHECK_TELL, "--wait", "--json")
            data = json.loads(out)
            kid8 = data["session"]["session_id"]
            seen8 = self.check_calls(kid8)
            verdict = data["message"].get("output") or {}
            view = self.show(kid8)
            denials = (view.get("last_result") or {}).get("denials")
            print(f"  checks {seen8}; verdict {verdict.get('state')} {verdict.get('code') or ''}; denials {denials}; "
                  f"state {view.get('state')}; tools called {self.transcript_tools(kid8)[1]}")
            self.check(not self.validated(seen8) and verdict.get("state") in ("accepted", "missing"),
                       "D8 (real Codex): no check reached the validator without the approval; the verdict is still "
                       "made from the reply")
            D7_RECORD.append({"who": "Codex child (listed, not approved)", "checks": seen8,
                              "verdict": verdict.get("state"), "denials": denials, "state": view.get("state")})
            self.board("session", "close", root)
            self.wait_ended(root)
        else:
            print("\n[D8] a Codex runner that lists the check tool but does not approve it (the stand-in)")
            out, _, _ = self.board("session", "start", str(self.project), "ready", "--agent", AGENT, "--wait",
                                   "--json")
            root = json.loads(out)["session"]["session_id"]
            rc, out, err = self.board_rc("session", "start", "--parent", root, "--agent", DENIED_CODEX,
                                         "--output-schema", str(review),
                                         f"mcp {CHECK_SERVER}/{output_schema.CHECK_TOOL} data-valid", "--wait",
                                         "--json")
            data = json.loads(out)
            kid8 = data["session"]["session_id"]
            deadline = time.monotonic() + 20
            while True:         # the node reports the state with or right after the turn's end
                view = self.show(kid8)
                if view.get("state") == "waiting" or time.monotonic() > deadline:
                    break
                time.sleep(0.2)
            denials = (view.get("last_result") or {}).get("denials") or []
            verdict = data["message"].get("output") or {}
            print(f"  denials {denials}; state {view.get('state')}; verdict {verdict.get('state')}")
            self.check(denials == [f"mcp__{CHECK_SERVER}__{output_schema.CHECK_TOOL}"] and view.get("state") == "waiting",
                       "D8: the refused check is recorded as a denial of exactly that tool, and the session shows "
                       "waiting")
            self.check(verdict.get("state") == "accepted", "D8: the verdict is still the reply's: accepted")
            self.board("session", "close", root)
            self.wait_ended(root)
        print("\n[D8] the frames: the sentence for a Claude session, none for a Codex one")
        lines = self.stdin_lines()
        claude_frames = [x for x in lines if "<<<output-schema " in x]
        sentence = output_schema._CHECK.format(asker="that session")[:60]      # (a Codex TASK may name the tool)
        human = output_schema._CHECK.format(asker="the human")[:60]
        self.check(bool(claude_frames) and all(sentence in x or human in x for x in claude_frames),
                   "D8: every asked frame written to a Claude process recommends the check (read-only children too)")
        codex_frames = [json.dumps(x) for x in self.codex_stdin() if "output-schema " in json.dumps(x)]
        self.check(bool(codex_frames) and all(sentence not in x and human not in x for x in codex_frames),
                   "D8: asked frames were written to Codex processes, and none of them recommends the check")
        if D7_RECORD:
            print("  record: " + json.dumps(D7_RECORD))

    def spawn_scenario(self) -> None:
        """T10: an agent session starts child sessions itself (hosted_spawn.py), through the board's tools."""
        real = self.real
        print("\n[T10] spawn tools: a hosted session starts child sessions of its own")
        first = "Reply with exactly the word: ready" if real else "ready"
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", SPAWN_AGENT, "--model",
                               "haiku", "--wait", "--json")
        rid = json.loads(out)["session"]["session_id"]
        self.show(rid)
        print(f"  parent {rid} ({SPAWN_AGENT})")
        [caps, refused] = self.mcp_calls(SPAWN_AGENT, [["board_session_capabilities", {}],
                                                      ["board_session_spawn", {"task": "x", "child_agent": AGENT}]], rid)
        caps = json.loads(caps["text"])
        self.check(caps["enabled"] and not caps["may_spawn"] and caps["caller"]["session_id"] == rid
                   and any("no live spawn grant" in w for w in caps["why_not"]),
                   "capabilities, from the session's own board server: it is known by its binding, and without a "
                   "grant it may not spawn (and says why)")
        self.check(refused["error"] and "no live spawn grant" in refused["text"], "spawn without a grant: refused")
        g, _, _ = self.board("session", "grant", str(self.project), "--agent", SPAWN_AGENT, "--access", "workspace",
                             "--hours", "1")
        print("  " + g.strip())
        [forged, above, deny_tree] = self.mcp_calls(SPAWN_AGENT, [
            ["board_session_capabilities", {}],
            ["board_session_spawn", {"task": "x", "child_agent": AGENT, "access": "full"}],
            ["board_session_status", {}]], rid)
        caps = json.loads(forged["text"])
        self.check(caps["may_spawn"] and caps["grant"]["access"] == "workspace"
                   and {t["agent"] for t in caps["targets"]} >= {AGENT, CODEX_AGENT},
                   "with the grant it may, and capabilities lists the targets (Claude and Codex) on its node")
        self.check(above["error"] and "above what you may give a child now" in above["text"],
                   "a child above the grant's ceiling (full, over workspace): refused")
        self.check(not deny_tree["error"] and json.loads(deny_tree["text"])["children"] == [], "no child yet")
        env_bad = self.env | {"AGENT_COMMS_HOME": str(self.hub_home), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens"),
                              "AGENT_COMMS_HOSTED_SESSION": rid, "AGENT_COMMS_HOSTED_KEY": "0" * 64}
        r = subprocess.run([sys.executable, "-c", MCP_CALLS, SPAWN_AGENT,
                            json.dumps([["board_session_spawn", {"task": "x"}]])], cwd=str(self.tmp),
                           capture_output=True, text=True, timeout=120, env=env_bad)
        [forged] = json.loads(r.stdout)
        self.check(forged["error"] and "does not verify" in forged["text"], "a forged binding key: refused")

        if real:
            ask = (f"Delegate two small tasks with the board tool board_session_spawn, wait=true each time: one with "
                   f"child_agent \"{AGENT}\", model \"haiku\", access \"read-only\" and task \"Reply with exactly the word: "
                   f"apple\"; one with child_agent \"{CODEX_AGENT}\", model \"{self.args.codex_model}\", effort \"low\", "
                   f"access \"read-only\" and task \"Reply with exactly the word: pong\". Then reply with exactly the "
                   f"two words the children replied, separated by one space, and nothing else.")
            out, _, took = self.board("session", "send", rid, ask, "--wait", "--json")
            data = json.loads(out)
            print(f"  the human asked it to delegate; its turn took {took:.1f}s; its tool calls: "
                  f"{self.tool_calls(rid)}; its reply: {data['reply']!r}")
            calls = [b for b in self.tool_uses(rid) if b.get("name") == "mcp__agent-comms__board_session_spawn"]
            self.check(len(calls) >= 2, "it called board_session_spawn itself, for both children")
        else:
            spawned = self.mcp_calls(SPAWN_AGENT, [
                ["board_session_spawn", {"task": "apple", "child_agent": AGENT, "access": "read-only", "role": "research",
                                         "title": "Say apple"}],
                ["board_session_spawn", {"task": "pong", "child_agent": CODEX_AGENT, "access": "read-only"}]], rid)
            self.check(not any(x["error"] for x in spawned), "the session's board server started both children")
        view = self.show(rid)
        kids = [k for k in view.get("children", []) if (k.get("spawned_by") or {}).get("session") == rid]
        print(f"  children it started: {[(k['name'], k['agent'], k['access'], k['state']) for k in kids]}")
        self.check(len(kids) == 2 and {k["agent"] for k in kids} == {AGENT, CODEX_AGENT}
                   and all(k["spawned_by"]["agent"] == SPAWN_AGENT for k in kids),
                   "two children, one Claude and one Codex, each recorded as started by the session")
        ids = [k["session_id"] for k in kids]
        deadline = time.monotonic() + 180
        while True:
            res = [x for x in self.show(rid).get("results", []) if x["child"] in ids]
            if len(res) == 2 and all(x["state"] in ("claimed", "delivered") for x in res):
                break
            if time.monotonic() > deadline:
                raise SystemExit(f"FAIL: the children's results did not reach the parent: {json.dumps(res)}")
            time.sleep(0.3)
        print(f"  results: {[(x['name'], x['outcome'], x['state'], x['started_by']) for x in res]}")
        self.check(all(x["outcome"] == "completed" and x["started_by"] == "parent" for x in res),
                   "both turns completed, and each result says the parent started that work")
        task_turns = [t for t in self.stdin_lines() if t.startswith(f"[agent-comms] A task from hosted session {rid} "
                                                                    f"(agent {SPAWN_AGENT})")]
        self.check(len(task_turns) == 1 and "not the human" in task_turns[0] and "untrusted data" in task_turns[0],
                   "the Claude child's turn starts with the server's frame: a task from that session, not the human, "
                   "untrusted data")
        codex_tasks = [m for m in self.codex_stdin() if m.get("method") == "turn/start"
                       and str(((m.get("params") or {}).get("input") or [{}])[0].get("text", "")).startswith(
                           f"[agent-comms] A task from hosted session {rid}")]
        self.check(len(codex_tasks) == 1, "and so does the Codex child's")
        if real:
            reply = (self.show(rid).get("reply") or "").lower()
            self.check("apple" in reply and "pong" in reply, "the parent answered from its children's results")
        audit = self.tree_thread_posts(rid)
        print(f"  audit thread: {len(audit['posts'])} post(s), first: {audit['posts'][0]['body'][:160]!r} ...")
        self.check(len(audit["posts"]) >= 2 and set(audit["automatic"]) == {p["id"] for p in audit["posts"]}
                   and all(p["agent"] == "human" and p["body"].startswith("Automatic audit") for p in audit["posts"]),
                   "one automatic audit post per child, written by the hub, in the tree's thread")
        listed, _, _ = self.board("session", "list")
        self.check(listed.count(f"started by {SPAWN_AGENT}") == 2, "`board session list` says who started them")

        print("\n[T10b] an interactive session (a board server with no binding) starts a child")
        if not real:
            p = str(self.project)
            out = self.mcp_calls(SPAWN_AGENT, [
                ["board_session_capabilities", {"project": p}],
                ["board_session_spawn", {"task": "pear", "child_agent": AGENT, "access": "read-only", "project": p}]])
            caps = json.loads(out[0]["text"])
            self.check(caps["may_spawn"] and caps["caller"]["kind"] == "interactive"
                       and caps["budget"]["live_now"] == 2,
                       "a board server with no binding is the agent's interactive root for the project, and counts "
                       "against the same budget as the hosted session's children (2 live)")
            kid = json.loads(out[1]["text"])["child"]
            # Another process (the board server restarted): the same root, its child there, its result taken.
            [st] = self.mcp_calls(SPAWN_AGENT, [["board_session_status", {"project": p, "wait_seconds": 30}]])
            st = json.loads(st["text"])
            print(f"  interactive child {kid['name']} {kid['id']}; taken by another process: "
                  f"{json.dumps((st.get('result') or {}).get('result'))}")
            self.check(not out[1]["error"] and kid["id"] in [c["id"] for c in st["children"]]
                       and st["result"] and st["result"]["result"]["child"] == kid["id"]
                       and "<<<child-output" in st["result"]["text"]
                       and "turn 1: [AGENT-COMMS] A TASK FROM AN INTERACTIVE SESSION" in st["result"]["text"],
                       "its child ran on the node, a restarted board server sees it, and its result came back to the "
                       "status call (never a turn: nothing can write that session one)")
            self.board("session", "close", kid["parent"])
            self.check(self.wait_ended(kid["id"])["state"] == "ended", "closing the interactive tree ends its child")
        else:
            print("  (stand-in runs only: it would cost one more haiku turn)")

        self.board("session", "close", rid)
        self.check(self.wait_ended(rid)["state"] == "ended", "closing the parent ends it")
        self.check(all(self.wait_ended(k)["state"] == "ended" for k in ids), "and the children it started")
        audit = self.tree_thread_posts(rid)
        self.check(len(audit["posts"]) >= 4 and sum("ended: closed because the session it was started under ended"
                                                    in p["body"] for p in audit["posts"]) == 2,
                   "and one audit post for each child's end")
        listed, _, _ = self.board("session", "list")
        self.check(listed.strip() == "(no hosted sessions)", "no hosted session is left")

    def work_homes(self, sid: str) -> list:
        """The CLAUDE_CONFIG_DIR each start of this session's process had, as the work runner's wrapper recorded it."""
        try:
            lines = [json.loads(x) for x in self.work_env_log.read_text().splitlines()]
        except OSError:
            return []
        return [x["CLAUDE_CONFIG_DIR"] for x in lines if sid in x["argv"]]

    def accounts_scenario(self) -> None:
        """A1-A3: two accounts on one machine (agent_accounts.py)."""
        print("\n[A1] another account: a runner with an account folder, opted in to one project")
        if self.real:
            print("  (stand-in runs only: a real CLI in a fresh account folder has no sign-in)")
            return
        out, _, _ = self.board("session", "start", str(self.project), "ready", "--agent", WORK_AGENT, "--wait",
                               "--json")
        sid = json.loads(out)["session"]["session_id"]
        view = self.show(sid)
        print(f"  {WORK_AGENT} session {sid}: account folder {view.get('account_home')}")
        self.check(self.work_homes(sid) == [str(self.work_home)],
                   "its process was started with CLAUDE_CONFIG_DIR set to the runner's account folder")
        self.check(view.get("account_home") == str(self.work_home), "`show` says which account folder")
        listed, _, _ = self.board("session", "list")
        self.check(f"{WORK_AGENT} (account folder {self.work_home}) in {self.project}" in listed,
                   "and so does `list`")

        print("\n[A2] a project it is not opted in to")
        out, err, _ = self.board("session", "start", str(self.project_mode), "ready", "--agent", WORK_AGENT,
                                 ok=False)
        print(f"  {(out + err).strip()[:200]}")
        self.check(f"{WORK_AGENT} is not opted in to {self.project_mode}" in out + err,
                   "a session in the second checkout is refused, with the reason")
        listed, _, _ = self.board("session", "list")
        self.check(str(self.project_mode) not in listed, "and nothing was made")

        print("\n[A3] the other agent starts the work agent's helper where it is opted in, and only there")
        out, _, _ = self.board("session", "start", str(self.project), "ready", "--agent", SPAWN_AGENT, "--wait",
                               "--json")
        rid = json.loads(out)["session"]["session_id"]
        self.show(rid)
        self.board("session", "grant", str(self.project), "--agent", SPAWN_AGENT, "--access", "workspace",
                   "--hours", "1")
        self.board("session", "grant", str(self.project_mode), "--agent", SPAWN_AGENT, "--access", "workspace",
                   "--hours", "1")
        [caps, spawned] = self.mcp_calls(SPAWN_AGENT, [
            ["board_session_capabilities", {}],
            ["board_session_spawn", {"task": "ready", "child_agent": WORK_AGENT, "access": "read-only"}]], rid)
        caps = json.loads(caps["text"])
        self.check(caps["may_spawn"] and WORK_AGENT in {t["agent"] for t in caps["targets"]},
                   f"in the checkout {WORK_AGENT} is opted in to, it is among the session's targets")
        self.check(not spawned["error"], f"and the session started a child of {WORK_AGENT} there")
        kid = json.loads(spawned["text"])["child"]["id"]
        self.wait_for("the work child's process", lambda: self.work_homes(kid) != [])
        self.check(self.work_homes(kid) == [str(self.work_home)], "the child's process has the account folder too")
        p = str(self.project_mode)
        [caps, refused] = self.mcp_calls(SPAWN_AGENT, [
            ["board_session_capabilities", {"project": p}],
            ["board_session_spawn", {"task": "x", "child_agent": WORK_AGENT, "access": "read-only", "project": p}]])
        caps = json.loads(caps["text"])
        print(f"  targets in the second checkout: {sorted(t['agent'] for t in caps.get('targets', []))}")
        self.check(caps["may_spawn"] and WORK_AGENT not in {t["agent"] for t in caps.get("targets", [])},
                   f"in the second checkout {WORK_AGENT} is not offered")
        self.check(refused["error"] and f"{WORK_AGENT} is not opted in to {p}" in refused["text"],
                   "and starting it there is refused, with the reason")
        for x in (rid, sid):
            self.board("session", "close", x)
            self.check(self.wait_ended(x)["state"] == "ended", f"closing {x} ends it")
        self.check(self.wait_ended(kid)["state"] == "ended", "and the child under it")

    def pool_settings(self, rules: dict | None = None) -> None:
        """P1-P4: the hub's board.toml with the account pool (and, from P3 on, a rule for the temp checkout), written
        while the hub runs: it takes it at its next reload, as any settings edit. Its spawn budget is raised for
        these extra starts."""
        path = self.hub_home / "board.toml"
        if not hasattr(self, "hub_toml_before_pools"):
            self.hub_toml_before_pools = path.read_text()
        text = self.hub_toml_before_pools
        lines = [f"pools = {{ {json.dumps(POOL)} = {toml_list([WORK_AGENT, AGENT])} }}",
                 "max_spawns_per_hour = 100", "max_live_children = 20"]
        if rules:
            lines.append("pool_rules = { " + ", ".join(
                f"{json.dumps(p)} = {{ " + ", ".join(f"{k} = {toml_list(v)}" for k, v in r.items()) + " }"
                for p, r in rules.items()) + " }")
        head, sep, tail = text.partition("[dispatch.sessions.spawn]\nenabled = true\n")
        if not sep:
            raise SystemExit("the hub's board.toml has no [dispatch.sessions.spawn] table to add the pool to")
        path.write_text(head + sep + "\n".join(lines) + "\n" + tail)

    def usage_seen(self) -> dict:
        """What the hub keeps of the usage the node reported (account_pools.USAGE_PREFIX), read from its database by
        another process."""
        code = ("import json\nfrom agent_comms import account_pools\nfrom agent_comms.config import Settings\n"
                "from agent_comms.core import Board\nb = Board(Settings.load())\n"
                f"print(json.dumps(account_pools.usage_view(b.conn, {NODE!r})))")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(self.tmp), capture_output=True, text=True,
                           timeout=60, env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return json.loads(r.stdout)

    def pool_spawn(self, rid: str, project: str | None = None) -> dict:
        args = {"task": "ready", "child_agent": POOL, "access": "read-only"} | ({"project": project} if project else {})
        [made] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", args]], None if project else rid)
        return made

    def pools_scenario(self) -> None:
        """P1-P4: an account pool of two agents (account_pools.py), the stand-in reporting the first one's usage."""
        print(f"\n[P1] an account pool: {POOL} = {WORK_AGENT} first, then {AGENT}")
        if self.real:
            print("  (stand-in runs only: the first account would have to be near its real limit)")
            return
        self.pool_settings()
        out, _, _ = self.board("session", "start", str(self.project), "ready", "--agent", SPAWN_AGENT, "--wait",
                               "--json")
        rid = json.loads(out)["session"]["session_id"]
        self.show(rid)
        made = self.pool_spawn(rid)
        self.check(not made["error"], f"the session started a child of pool {POOL}"
                   + (f": {made['text'][:300]}" if made["error"] else ""))
        first = json.loads(made["text"])["child"]
        self.check(first["agent"] == WORK_AGENT and first.get("pool") == POOL,
                   f"the first spawn went to the first member, {WORK_AGENT}")
        self.wait_for("the audit post", lambda: len(self.tree_thread_posts(rid)["posts"]) >= 1)
        audit = self.tree_thread_posts(rid)["posts"][-1]["body"]
        self.check(f"agent {WORK_AGENT}" in audit and f"Its agent was asked for as pool {POOL} and chosen by the hub "
                   "(first choice); `board session show` gives the full reason." in audit,
                   "its audit post says which member and, in brief, why (first choice)")
        print(f"\n[P2] the stand-in reports {WORK_AGENT}'s account at 95%")
        out, _, _ = self.board("session", "send", first["id"], "usage 95 and reply", "--wait")
        self.wait_for("the usage report at the hub",
                      lambda: (self.usage_seen().get(WORK_AGENT) or {}).get("five_hour", {}).get("percent") == 95.0, seconds=40)
        made = self.pool_spawn(rid)
        second = json.loads(made["text"])["child"] if not made["error"] else {}
        self.check(second.get("agent") == AGENT, f"the next spawn went to the second member, {AGENT}")
        view = self.show(second["id"])
        print(f"  {view['pool']['why']}")
        self.check(f"overflow, because {WORK_AGENT} was at 95% of its five_hour window" in view["pool"]["why"],
                   "and `show` says why: overflow, with the first member's usage")
        detail, _, _ = self.board("session", "show", second["id"])
        self.check(f"asked for as pool {POOL}; the hub chose {AGENT} from pool {POOL}: overflow" in detail,
                   "`board session show` says it too")
        print(f"\n[P3] a project rule: only {WORK_AGENT} in the temp checkout")
        self.pool_settings({str(self.project): {"only": [WORK_AGENT]}, str(self.project_mode): {"only": [WORK_AGENT]}})
        made = self.pool_spawn(rid)
        third = json.loads(made["text"])["child"] if not made["error"] else {}
        why = self.show(third["id"])["pool"]["why"] if third else ""
        print(f"  {why}")
        self.check(third.get("agent") == WORK_AGENT and f"only {WORK_AGENT}" in why
                   and "headroom never refuses work" in why,
                   f"{AGENT} is refused there: {WORK_AGENT} is taken although it is near its limit, and the reason "
                   "says so")
        made = self.pool_spawn(rid, project=str(self.project_mode))
        print(f"  in the second checkout: {made['text'][:240]}")
        self.check(made["error"] and f"no member of pool {POOL} can be started in {self.project_mode}" in made["text"]
                   and f"{WORK_AGENT} is not opted in" in made["text"],
                   f"where {WORK_AGENT} may not run, `only` leaves nothing: refused with the reason")
        print(f"\n[P4] a project rule: never {WORK_AGENT}")
        self.pool_settings({str(self.project): {"never": [WORK_AGENT]}})
        out, _, _ = self.board("session", "send", first["id"], "usage 10 and reply", "--wait")
        self.wait_for("the new usage report", lambda: (self.usage_seen().get(WORK_AGENT) or {}).get("five_hour", {}).get("percent") == 10.0,
                      seconds=40)
        made = self.pool_spawn(rid)
        fourth = json.loads(made["text"])["child"] if not made["error"] else {}
        why = self.show(fourth["id"])["pool"]["why"] if fourth else ""
        self.check(fourth.get("agent") == AGENT and f"never {WORK_AGENT}" in why,
                   f"{WORK_AGENT} is skipped although it has room now: {why}")
        out, _, _ = self.board("session", "start", "--parent", rid, "ready", "--agent", POOL, "--access", "read-only")
        print(f"  {out.strip()[:300]}")
        self.check(f"asked for as pool {POOL}; the hub chose {AGENT} from pool {POOL}" in out,
                   "the human's `start --parent ... --agent <pool>` is decided the same way, and says so")
        self.board("session", "close", rid)
        self.check(self.wait_ended(rid)["state"] == "ended", f"closing {rid} ends it")
        for kid in (first, second, third, fourth):
            self.check(self.wait_ended(kid["id"])["state"] == "ended", "and every child under it")

    def deep_scenario(self) -> None:
        """T11: a deeper tree (max_depth 2, shipped): a hosted session starts a child that starts a grandchild, and the
        results come back up level by level (DESIGN_NOTES "Session host: deeper trees")."""
        real = self.real
        print("\n[T11] a deeper tree: a parent starts a child that starts a grandchild")
        first = "Reply with exactly the word: ready" if real else "ready"
        out, _, _ = self.board("session", "start", str(self.project), first, "--agent", SPAWN_AGENT, "--model",
                               "haiku", "--wait", "--json")
        rid = json.loads(out)["session"]["session_id"]
        self.show(rid)
        print(f"  root {rid} ({SPAWN_AGENT})")
        if real:
            inner = (f"Use the board tool board_session_spawn once, with wait=true, child_agent '{AGENT}', model "
                     "'haiku', access 'read-only' and task 'Reply with exactly the word: plum'. Then reply with "
                     "exactly the word that session replied, and nothing else.")
            ask = (f"Use the board tool board_session_spawn once, with wait=true, child_agent '{SPAWN_AGENT}', model "
                   f"'haiku', access 'workspace' and this task: \"{inner}\" Then reply with exactly the word your "
                   "child session replied, and nothing else.")
            out, _, took = self.board("session", "send", rid, ask, "--wait", "--json")
            print(f"  the human asked the root to delegate two levels down; its turn took {took:.1f}s; its tool "
                  f"calls: {self.tool_calls(rid)}; its reply: {json.loads(out)['reply']!r}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not [k for k in self.show(rid).get("children", [])
                                                       if (k.get("spawned_by") or {}).get("session") == rid]:
                time.sleep(0.3)
        else:
            [made] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
                "task": "olive", "child_agent": SPAWN_AGENT, "access": "workspace", "role": "research"}]], rid)
            self.check(not made["error"], "the root's board server started the child")
        kids = [k for k in self.show(rid).get("children", []) if (k.get("spawned_by") or {}).get("session") == rid]
        self.check(len(kids) == 1 and kids[0]["agent"] == SPAWN_AGENT and kids[0]["depth"] == 1,
                   "the root started one child, at depth 1, of the same agent (recursion is bounded by depth and "
                   "budget)")
        kid = kids[0]["session_id"]
        if not real:
            deadline = time.monotonic() + 30        # the child's first turn (`olive`) is over: then it is idle
            while self.show(kid).get("turns", 0) < 1 and time.monotonic() < deadline:
                time.sleep(0.2)
            [made] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
                "task": "plum", "child_agent": AGENT, "access": "read-only", "role": "test"}]], kid)
            self.check(not made["error"], "the child's own board server (its binding) started the grandchild")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not self.show(kid).get("children"):
            time.sleep(0.3)
        grands = self.show(kid).get("children", [])
        if not grands:
            print(f"  the child's own reply: {self.show(kid).get('reply')!r}")     # (why it started none)
        self.check(len(grands) == 1 and grands[0]["depth"] == 2 and grands[0]["root"] == rid
                   and grands[0]["parent"] == kid and grands[0]["access"] == "read-only"
                   and (grands[0].get("spawned_by") or {}).get("session") == kid,
                   "the child started one grandchild, at depth 2, under it and in the root's tree")
        grand = grands[0]["session_id"]
        deadline = time.monotonic() + 240
        while True:
            from_grand = [x for x in self.show(kid).get("results", []) if x["child"] == grand]
            from_kid = [x for x in self.show(rid).get("results", []) if x["child"] == kid]
            # What each run proves. Real CLIs: each level waits in its own turn (wait=true), so every result is
            # `claimed` by the call that waited. Stand-ins: nobody waits, so every result is a turn (`delivered`).
            settled = ("claimed",) if real else ("delivered",)
            done = ("claimed", "delivered", "undelivered")
            if (from_grand and all(x["state"] in done for x in from_grand)
                    and len(from_kid) >= (1 if real else 2) and all(x["state"] in done for x in from_kid)
                    and self.show(rid)["state"] == "idle" and not self.show(rid).get("queued")):
                break
            if time.monotonic() > deadline:
                raise SystemExit(f"FAIL: the results did not come back up: grandchild {json.dumps(from_grand)}, "
                                 f"child {json.dumps(from_kid)}")
            time.sleep(0.5)
        lines = self.stdin_lines()     # (once both turns are over)
        self.check(any("You may start helper sessions of your own" in t and "depth 1 of at most 2" in t
                       and "cannot start any of their own" in t for t in lines),
                   "the child's frame says it may start helpers, and that theirs may not")
        self.check(any("You cannot start helper sessions of your own: you are at depth 2, the deepest level" in t
                       for t in lines), "the grandchild's frame says plainly that it cannot")
        print(f"  grandchild -> child: {[(x['outcome'], x['state'], x['started_by']) for x in from_grand]}")
        print(f"  child -> root: {[(x['outcome'], x['state'], x['started_by']) for x in from_kid]}")
        self.check(all(x["state"] in settled for x in from_grand + from_kid),
                   "every result came back by the path this run takes: " + (
                       "each level waited for it in its own turn (claimed)" if real
                       else "each was its parent's next turn (delivered)"))
        self.check(all(x["outcome"] == "completed" and x["started_by"] == "parent" for x in from_grand),
                   "the grandchild's result went to the child, its direct parent")
        self.check(not [x for x in self.show(rid).get("results", []) if x["child"] == grand],
                   "and never to the root: nothing skips a level")
        grand2 = None
        if real:
            reply = (self.show(rid).get("reply") or "").strip()
            print(f"  the root's last reply: {reply!r}")
            self.check(reply == "plum", "the root answered with exactly the grandchild's word, passed up through the "
                                        "child")
        else:
            self.check(from_kid[-1]["started_by"] == "child" and from_kid[-1]["outcome"] == "completed",
                       "the child took the grandchild's result as a turn, and its reply to it is a result for the root")
            # (A stand-in replies `turn <k>: <its turn's text in capitals>`.)
            turns = self.stdin_lines()
            self.check(any(t.startswith("[agent-comms] Results from 1 child session")
                           and "\nturn 1: [AGENT-COMMS] A TASK FROM CHILD SESSION" in t and "PLUM" in t for t in turns),
                       "the child's turn carried the grandchild's reply, framed by the server")
            self.check(any(t.startswith("[agent-comms] Results from 1 child session")
                           and "turn 2: [AGENT-COMMS] RESULTS FROM 1 CHILD SESSION" in t and "PLUM" in t
                           for t in turns),
                       "the root's turn carried the child's reply to it, which holds the grandchild's")
            # The other path, deterministic with stand-ins: each level waits in its turn (wait=true) and takes the
            # result in the tool's answer; it is `claimed`, and never also a turn. A waiting session is busy in its
            # turn, so the human gives each a turn that is held open (`hold <file>`: it lasts until the file exists;
            # no result for anybody) while its board server waits; otherwise an idle stand-in's result could become a
            # turn before the call takes it. Each is released explicitly, and each wait call is made only once its
            # session is seen in that turn.
            release = {x: self.tmp / f"release-{x}" for x in ("root", "child")}
            self.board("session", "send", rid, f"hold {release['root']} the root is in a turn")
            self.board("session", "send", kid, f"hold {release['child']} the child is in a turn")

            def in_turn(sid: str, want: bool, what: str) -> None:
                deadline = time.monotonic() + 30
                while bool(self.show(sid).get("in_flight")) != want:
                    if time.monotonic() > deadline:
                        raise SystemExit(f"FAIL: {what} was not {'in' if want else 'out of'} its held turn within 30 s")
                    time.sleep(0.1)

            in_turn(rid, True, "the root")
            in_turn(kid, True, "the child")
            self.check(bool(self.show(rid).get("in_flight")) and bool(self.show(kid).get("in_flight")),
                       "the root and the child are each in a turn held open")
            [made] = self.mcp_calls(SPAWN_AGENT, [["board_session_spawn", {
                "task": "pear", "child_agent": AGENT, "access": "read-only", "role": "test", "wait": True}]], kid)
            if made["error"]:
                raise SystemExit(f"FAIL: the second grandchild was refused: {made['text'][:600]}")
            out = json.loads(made["text"])
            grand2 = out["child"]["id"]
            self.check(bool(out.get("result")) and "PEAR" in out["result"]["text"]
                       and out["result"]["result"]["child"] == grand2,
                       "the child's board server waited for a second grandchild and took its result in the answer")
            release["child"].touch()                    # the child's held turn ends; the root's is still open
            in_turn(kid, False, "the child")
            self.check(bool(self.show(rid).get("in_flight")), "the root is still in its held turn")
            [sent] = self.mcp_calls(SPAWN_AGENT, [["board_session_send", {"child": kid, "message": "fig",
                                                                          "wait": True}]], rid)
            out = json.loads(sent["text"]) if not sent["error"] else {}
            self.check(bool(out.get("result")) and "FIG" in out["result"]["text"],
                       "the root's board server waited for the child's reply and took it in the answer")
            self.check(bool(self.show(rid).get("in_flight")), "the root was in its held turn all through its wait")
            fig = out["result"]["result"]["message"]
            mine = [x for x in self.show(kid).get("results", []) if x["child"] == grand2]
            took = [x for x in self.show(rid).get("results", []) if x["child"] == kid and x["message"] == fig]
            self.check([x["state"] for x in mine] == ["claimed"] and [x["state"] for x in took] == ["claimed"],
                       "both claimed, one level each, and neither became a turn")
            release["root"].touch()
        audit = self.tree_thread_posts(rid)
        self.check(sum("started child session" in p["body"] for p in audit["posts"]) == (2 if real else 3)
                   and any(f"depth 2 under parent {kid} in tree {rid}" in p["body"] for p in audit["posts"]),
                   "one audit post per start, at each level, naming the direct parent and the root")
        self.board("session", "close", rid)
        below = [kid, grand] + ([grand2] if grand2 else [])
        self.check(all(self.wait_ended(x)["state"] == "ended" for x in [rid] + below),
                   "closing the root ends the child and the grandchild" + ("ren" if grand2 else ""))
        audit = self.tree_thread_posts(rid)
        self.check(sum("ended: closed because the session it was started under ended" in p["body"]
                       for p in audit["posts"]) == len(below), "one audit post for each end below the root")
        listed, _, _ = self.board("session", "list")
        self.check(listed.strip() == "(no hosted sessions)", "no hosted session is left")

    # ------------------------------------------------------------ teardown

    def stop(self) -> None:
        for name, proc in reversed(self.procs):     # the node first: it stops its session processes
            if proc.poll() is None:
                t0 = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(30)
                    print(f"  {name} stopped {time.monotonic() - t0:.1f}s after SIGTERM")
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
                    print(f"  {name} did not stop on SIGTERM within 30s and was killed")
        self.procs = []

    def leftovers(self) -> list[int]:
        """Session processes still there. Each was started in its own process group (its pid), and the wrapper's
        child (the stand-in, or Claude itself) is in that group: the whole group must be gone."""
        return [pid for pid in self.pids if alive(pid) or group_alive(pid)]

    def cleanup(self) -> None:
        self.stop()
        for pid in self.leftovers():    # only pids this run's own hub reported for this run's own session
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.args.keep:
            print(f"kept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--claude-bin", metavar="PATH", help="the real Claude Code CLI (default: a stand-in script)")
    ap.add_argument("--codex-bin", metavar="PATH", help="the real Codex CLI (default: a stand-in script)")
    ap.add_argument("--codex-model", default="gpt-6-luna", metavar="NAME",
                    help="with --codex-bin: the model of the hosted Codex session (default gpt-6-luna, the cheapest "
                         "one codex-cli 0.171.0 lists)")
    ap.add_argument("--only", choices=("claude", "codex", "tree", "steer", "structured", "check", "correlation",
                                       "steering"),
                    help="run only that scenario: one agent's, parent and child sessions, the send modes on a "
                         "running turn, results as data (D1-D8), or the in-turn check alone (D7-D8) "
                         "(default: all)")
    ap.add_argument("--max-latency", type=float, default=2.0, metavar="SECONDS",
                    help="most seconds from a message being sent to it being written to the process (default 2)")
    ap.add_argument("--max-wake", type=float, default=3.0, metavar="SECONDS",
                    help="most seconds from a post addressed to the agent to its wake being written (default 3)")
    ap.add_argument("--gateway", action="store_true", help="the node reaches the hub through `board gateway`")
    ap.add_argument("--with-board", action="store_true",
                    help="with --claude-bin: another scenario, in which the session has the temp board's MCP tools "
                         "(and only those). A post addressed to its agent must wake it AND be read by it")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory (logs, transcript, database)")
    args = ap.parse_args()
    if args.claude_bin is not None:
        args.claude_bin = os.path.abspath(os.path.expanduser(args.claude_bin))
        if os.path.basename(args.claude_bin) != "claude" or not os.access(args.claude_bin, os.X_OK):
            ap.error("--claude-bin must be an executable named claude")
    elif args.with_board:
        ap.error("--with-board needs --claude-bin: the stand-in calls no tools")
    if args.codex_bin is not None:
        args.codex_bin = os.path.abspath(os.path.expanduser(args.codex_bin))
        if os.path.basename(args.codex_bin) != "codex" or not os.access(args.codex_bin, os.X_OK):
            ap.error("--codex-bin must be an executable named codex")
    if args.with_board and args.only in ("codex", "tree", "steer", "structured", "check", "correlation", "steering"):
        ap.error("--with-board is a Claude scenario")
    codex, claude = args.only in (None, "codex") and not args.with_board, args.only in (None, "claude")
    tree = args.only in (None, "tree") and not args.with_board
    steer = args.only in (None, "steer") and not args.with_board
    correlation = args.only in (None, "correlation") and not args.with_board
    steering = args.only in (None, "steering") and not args.with_board     # Q0-Q8: its own hub and node, last
    structured = args.only in (None, "structured") and not args.with_board
    check = args.only == "check"        # the in-turn check's scenarios alone (D7-D8; structured runs them too)
    run = Run(args)
    if claude or tree or steer or structured or check or correlation:
        print("agent: " + (f"real Claude Code at {args.claude_bin}, --model haiku" if run.real
                           else "stand-in (tests/stand_in_claude.py), no model")
              + (", with the temp board's MCP tools" if args.with_board else ""))
    if codex or tree or steer or structured or check or steering:
        print("codex agent: " + (f"real Codex at {args.codex_bin}, --model {args.codex_model}" if run.real_codex
                                 else "stand-in (tests/stand_in_codex.py), no model"))
    print(f"temp directory: {run.tmp}")

    def terminated(signum, frame):
        # Whoever runs this script (a test with a time limit) can stop it with SIGTERM and still have the hub, the
        # node and every session process stopped and the temp directory removed (`finally`, below). Once only.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit("FAIL: terminated (SIGTERM) before the run was over")

    try:
        signal.signal(signal.SIGTERM, terminated)
        config_before, claude_before = None, run.claude_settings() if run.real else None
        if (codex or tree or steer or structured or check or steering) and run.real_codex:
            run.fresh_git_checkout()
            config_before = run.codex_config()      # before Codex is run for anything, `codex mcp list` included
        run.write_settings()
        if codex or claude or args.with_board or steer or structured or check:
            run.start_services()
            if codex:
                run.codex_scenario()
            if args.with_board:
                run.board_scenario()
            elif claude:
                run.scenario()
            if steer:
                run.steer_scenario()
            if structured:
                run.structured_off_scenario()
            if check and not run.real:
                run.check_off_scenario(run.review_files()[0])
            print("\n[3] stopping the node (SIGTERM) and the hub" if args.with_board
                  else "\n[8] stopping the node (SIGTERM) and the hub" if claude
                  else "\n[C6] stopping the node (SIGTERM) and the hub" if codex
                  else "\n[S7] stopping the node (SIGTERM) and the hub")
            run.stop()
            if claude:
                run.check(not alive(run.open_pid),
                          f"the open session's process (pid {run.open_pid}) was stopped with the node")
            run.check(run.leftovers() == [], "no session process is left behind")
            if (codex or steer) and run.real_codex:
                run.check_codex_config(config_before)
        if correlation:
            # Its own hub and node (the same temp directory and settings, nothing else running): the node with the
            # CLI's version in VERIFIED, never one an earlier or a later scenario runs on.
            run.node_verified = {run.cli_version()}
            try:
                run.start_services()
                run.correlation_scenario()
                print("\n[X5] stopping the node (SIGTERM) and the hub")
                run.stop()
                run.check(run.leftovers() == [], "no session process is left behind")
            finally:
                run.node_verified = None
        if tree or structured or check:
            run.enable_tree()
            run.start_services()
            if tree:
                run.tree_scenario()
                run.results_scenario()
                run.spawn_scenario()
                run.deep_scenario()
                run.accounts_scenario()
                run.pools_scenario()
            if structured:
                run.structured_scenario()
            if structured or check:
                run.check_scenario()
            print("\n[T8] stopping the node (SIGTERM) and the hub")
            run.stop()
            run.check(run.leftovers() == [], "no session process is left behind")
            if run.real_codex:
                run.check_codex_config(config_before)
            if run.real:
                run.check(run.claude_settings() == claude_before,
                          "the user's own Claude settings files (~/.claude/settings.json, settings.local.json) are "
                          "what they were before the run: the settings fixtures live in the temp directory only")
        if steering:
            # Its own hub and node, with child sessions on (the tree scenario's settings), after the others stopped;
            # no `claude_steer` key: Claude steering as by default (on).
            run.enable_tree()
            run.start_services()
            run.steering_scenario()
            print("\n[Q8] stopping the node (SIGTERM) and the hub")
            run.stop()
            run.check(run.leftovers() == [], "no session process is left behind")
            if run.real_codex:
                run.check_codex_config(config_before)
            # Claude steering (step 3): its own hub and node, VERIFIED and STEER_MODES naming the CLI under test.
            run.node_verified = {run.cli_version()}
            run.node_steer_modes = {v: "next_turn" for v in run.node_verified}
            try:
                run.start_services()
                run.claude_steering_scenario()
                print("\n[Z4] stopping the node (SIGTERM) and the hub")
                run.stop()
                run.check(run.leftovers() == [], "no session process is left behind")
                run.check(run.Z_STEER not in run.log_text("node"), "no steer text in the node's log")
            finally:
                run.node_verified = run.node_steer_modes = None
        print("\nPASS")
        return 0
    except SystemExit as e:
        print(e if isinstance(e.code, str) else f"exit {e.code}")
        for name in ("node", "hub", "gateway"):
            text = run.log_text(name)
            if text:
                print(f"--- {name} log (last lines) ---\n" + "\n".join(text.splitlines()[-25:]))
        return 1
    finally:
        run.cleanup()


if __name__ == "__main__":
    sys.exit(main())
