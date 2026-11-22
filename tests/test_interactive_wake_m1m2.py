"""Designing around a Claude Code start-up race (DESIGN_NOTES "Known Claude Code behaviour"): M1, a SessionStart
hook's wake is written no sooner than waiter.STARTUP_GRACE after the hook started. And the read evidence and the
reservations of review round 5 (S28, S29, S30), and the prompt check that covers a delivered wake Claude Code never
acted on (the unread return, M2, was removed in that round)."""

import io
import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from agent_comms import hosted, hosted_wake, interactive_wake as iw, waiter

from conftest import PROJECT
from test_hosted_sessions import senv  # noqa: F401
from test_hosted_wake import GRACE, ask, text_for
from test_interactive_wake_round2 import NB, NC, TWO, Hub, call, on, rec, woke

ROOT = Path(__file__).resolve().parents[1]
WAKE = hosted_wake.wake_text("claude", [[7, "codex", False, 1]])
CONV = "11111111-1111-4111-8111-111111111111"


def pair(env) -> dict:
    return iw._pair(env.board.conn, "claude", PROJECT)


# ------------------------------------------------------------ M1: the start-up grace of a SessionStart hook


class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def run_hook(event, hub, clock, offered_after=0.0):
    """board wait for a hook of `event`; the hub offers its wake `offered_after` seconds into the process."""
    real_post = hub.post

    def post(path, json, **kw):
        if json.get("ack") is None and not hub.bodies:
            clock.t += offered_after
        hub.acked_at = clock.t if json.get("ack") is not None else getattr(hub, "acked_at", None)
        return real_post(path, json, **kw)
    hub.post = post
    err = io.StringIO()
    hook = {"session_id": CONV, "cwd": "/work/repo", "hook_event_name": event}
    code = waiter.run("claude", [PROJECT], 600, stdin=io.StringIO(json.dumps(hook)), stderr=err, environ={},
                      client=hub, sleep=clock.sleep, clock=clock, watch=False)
    return code, err.getvalue()


def test_m1_a_session_start_wake_is_acknowledged_at_once_and_written_after_the_grace():
    clock = Clock()
    hub = Hub({"state": "wake", "text": WAKE, "id": "1" * 16})
    assert run_hook("SessionStart", hub, clock, offered_after=2.0) == (2, WAKE + "\n")
    assert hub.acked_at == 1002.0, "acknowledged as soon as it was offered: the hub's deadline stands"
    assert clock.t >= 1000.0 + waiter.STARTUP_GRACE, "written no sooner than the grace after the hook started"
    assert waiter.STARTUP_GRACE == 10.0


def test_m1_a_session_start_wake_after_the_grace_and_a_stop_hooks_wake_are_written_at_once():
    clock = Clock()
    assert run_hook("SessionStart", Hub({"state": "wake", "text": WAKE, "id": "1" * 16}), clock,
                    offered_after=12.0) == (2, WAKE + "\n")
    assert clock.slept == []
    clock = Clock()
    assert run_hook("Stop", Hub({"state": "wake", "text": WAKE, "id": "1" * 16}), clock,
                    offered_after=1.0) == (2, WAKE + "\n")
    assert clock.slept == [] and clock.t == 1001.0, "the Stop hook is unchanged: no delay"


SNIPPET = textwrap.dedent('''
    import io, json, os, sys, httpx
    from agent_comms import hosted_wake, waiter
    WAKE = hosted_wake.wake_text("claude", [[7, "codex", False, 1]])
    class Hub:
        def post(self, path, json, **kw):
            if json.get("ack"):
                print("acked", flush=True)
                return httpx.Response(200, json={"state": "acked"})
            return httpx.Response(200, json={"state": "wake", "text": WAKE, "id": "1" * 16})
    hook = {"session_id": "11111111-1111-4111-8111-111111111111", "cwd": "/work/repo",
            "hook_event_name": "SessionStart"}
    sys.exit(waiter.run("claude", ["/work/repo"], 600, stdin=io.StringIO(json.dumps(hook)), client=Hub(),
                        environ={"CLAUDE_PID": sys.argv[1]}))
''')


def grace_process(parent_pid: int) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", SNIPPET, str(parent_pid)], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=str(ROOT))
    assert p.stdout.readline().strip() == "acked", p.stderr.read()
    return p


def test_m1_sigterm_during_the_grace_exits_0_with_no_wake():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    p = grace_process(sleeper.pid)
    try:
        time.sleep(0.5)
        assert p.poll() is None, "waiting out the grace"
        p.send_signal(signal.SIGTERM)
        assert p.wait(5) == 0 and p.stderr.read() == ""
    finally:
        sleeper.terminate()
        sleeper.wait(5)
        if p.poll() is None:
            p.terminate()
            p.wait(5)


def test_m1_claude_gone_during_the_grace_exits_0_with_no_wake():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    p = grace_process(sleeper.pid)
    try:
        sleeper.terminate()
        sleeper.wait(5)                    # reaped: gone, as Claude Code's own parent would reap it
        assert p.wait(5) == 0 and p.stderr.read() == ""
    finally:
        if p.poll() is None:
            p.terminate()
            p.wait(5)


# ------------------------------------------------------------ read evidence: the reader's project, the waiter's moves


def delivered(env, tid):
    call(env)
    post = ask(env, tid)
    assert woke(env) == text_for(env, 1, [tid])
    return post


def test_a_read_in_the_posts_project_by_a_waiter_that_moved_on_still_holds(senv):
    """Found by the randomized run (seed 6) while the unread return existed: the read evidence of a waiter that
    moved to another project after it read its post was looked for among the project's current waiters only. It is
    kept on the pair, and every waiter's record counts wherever it is now; `holds` after the deadline sees it."""
    from test_hosted_sessions import P2
    on(senv)
    tid = senv.thread()
    post = delivered(senv, tid)
    reader = senv.board.register_session(senv.p["claude"], PROJECT, client=("claude-code", CONV))["session_id"]
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")      # woken, it reads the post
    assert call(senv, nonce=NB, projects=(P2,)) == {"state": "idle"}         # then works in another project
    senv.clock.advance(GRACE + 1)
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "the read still holds it"


def test_a_read_in_another_project_is_no_read_evidence_for_the_posts_project(senv):
    """S30: the conversation moved to P2 and its P2 session read (addressed-only) the P1 post: that is no sign the
    request is worked on in P1, so the hold ends at its deadline as for an unread wake."""
    from test_hosted_sessions import P2
    on(senv)
    tid = senv.thread()
    post = delivered(senv, tid)
    assert call(senv, nonce=NB, projects=(P2,)) == {"state": "idle"}         # moved, before reading anything
    elsewhere = senv.board.register_session(senv.p["claude"], P2, client=("claude-code", CONV))["session_id"]
    out = senv.board.read_updates(senv.p["claude"], elsewhere, only="addressed")
    assert post["id"] in [d["id"] for d in out["posts"]], "the P2 session is shown the P1 post"
    senv.clock.advance(GRACE + 1)
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "and it holds nothing there"
    assert iw._pair(senv.board.conn, "claude", P2)["read"] == {} and rec(senv)["read"] == {}


def test_s30_a_receipt_from_a_read_in_another_project_is_no_read_evidence_after_re_registering(senv):
    """S30 (review round 6): the conversation's board session read (unfiltered) while registered in P2, which wrote
    a delivery receipt for the P1 post; then it re-registered in P1. The receipt names a session now in P1, yet it
    was not shown the post there: after the grace nothing holds the request and the dispatcher can reserve it. Only
    the project-qualified read mark (`handed`) is read evidence for an interactive hold."""
    from agent_comms import delivery
    from test_hosted_sessions import P2
    on(senv)
    tid = senv.thread()
    post = delivered(senv, tid)                                               # the P1 waiter is woken for it
    s = senv.board.register_session(senv.p["claude"], P2, client=("claude-code", CONV))["session_id"]
    out = senv.board.read_updates(senv.p["claude"], s)                        # unfiltered, registered in P2
    assert post["id"] in [d["id"] for d in out["posts"]], "the P2 session is shown the P1 post"
    assert delivery.receipt(senv.board.conn, post["id"], "claude")["session_id"] == s, "and a receipt is written"
    senv.board.register_session(senv.p["claude"], PROJECT, resume_session_id=s, client=("claude-code", CONV))
    senv.clock.advance(GRACE + 1)
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "the receipt holds nothing"
    assert hosted_wake.reserve(senv.board, "claude", PROJECT, post["id"], senv.clock()), "the dispatcher takes it"
    assert pair(senv)["read"] == {} and rec(senv)["read"] == {}


# ------------------------------------------------------------ S28: a reservation with no waiter left in the pair


def test_s28_a_reserved_request_is_never_held_or_offered_again_once_its_waiter_moved_on(senv):
    from test_hosted_sessions import P2
    on(senv)
    tid = senv.thread()
    post = delivered(senv, tid)
    assert call(senv, nonce=NB, projects=(P2,)) == {"state": "idle"}         # no waiter left in PROJECT
    senv.clock.advance(GRACE + 1)
    assert hosted_wake.reserve(senv.board, "claude", PROJECT, post["id"], senv.clock()), "the dispatcher takes it"
    assert str(post["id"]) in iw._pair(senv.board.conn, "claude", PROJECT)["claimed"], "recorded on the pair"
    call(senv, conv=TWO, nonce=NC)                                         # later, a new waiter in PROJECT
    reader = senv.board.register_session(senv.p["claude"], PROJECT, client=("claude-code", CONV))["session_id"]
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")      # and a read in PROJECT ...
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "... holds nothing again"
    senv.clock.advance(31)
    assert call(senv, conv=TWO, nonce=NC) == {"state": "idle"}, "and it is never offered the reserved request"


# ------------------------------------------------------------ S29: a post at the thread cap waits for the human


def test_s29_a_post_at_the_thread_cap_waits_for_the_humans_post_and_then_goes(senv):
    on(senv, max_wakes_without_human=1, wake_min_interval_seconds=0)
    tid = senv.thread()
    delivered(senv, tid)
    reader = senv.session("claude")
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")
    ask(senv, tid)
    for _ in range(3):
        senv.clock.advance(3600)
        assert woke(senv, nonce=NB) is None, "at the cap: it waits, however long"
    senv.post("human", tid, "carry on", to=[])
    assert woke(senv, nonce=NB) == text_for(senv, 1, [tid]), "the human's post lifts the stop; nothing waits forever"


# ------------------------------------------------------------ the prompt check still tells the session


def test_a_delivered_wake_the_session_never_acted_on_is_still_reported_by_the_prompt_check(senv):
    """The worst case without the unread return: Claude Code started no turn for a delivered wake. The session's
    UserPromptSubmit check (`board brief --session-from-stdin`, Board.brief's cursor-free view above the session's
    own mark) still counts the post at the next prompt: the interactive wake's delivery and read marks are not
    looked at there."""
    on(senv)
    tid = senv.thread()
    mark = senv.board.brief(senv.p["claude"], [PROJECT], after_seq=0)["latest_addressed_seq"] or 0
    post = delivered(senv, tid)
    b = senv.board.brief(senv.p["claude"], [PROJECT], after_seq=mark)
    assert b["addressed_after_seq"] == 1 and b["latest_addressed_seq"] == post["seq"]


def test_s32_the_grace_never_runs_past_the_commands_own_deadline():
    """`board wait --max-seconds 2` from a SessionStart hook: the wake is written at the deadline (the ack is
    committed, so exiting 0 would lose it), not at the ten-second grace."""
    clock = Clock()
    hub = Hub({"state": "wake", "text": WAKE, "id": "1" * 16})
    err = io.StringIO()
    hook = {"session_id": CONV, "cwd": "/work/repo", "hook_event_name": "SessionStart"}
    code = waiter.run("claude", [PROJECT], 2, stdin=io.StringIO(json.dumps(hook)), stderr=err, environ={},
                      client=hub, sleep=clock.sleep, clock=clock, watch=False)
    assert (code, err.getvalue()) == (2, WAKE + "\n")
    assert clock.t == 1002.0, "at its two-second deadline"
