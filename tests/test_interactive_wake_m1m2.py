"""Designing around a Claude Code start-up race (DESIGN_NOTES "Known Claude Code behaviour"): M1, a SessionStart
hook's wake is written no sooner than waiter.STARTUP_GRACE after the hook started; M2 (the state machine's O5), a
delivered wake nobody read within interactive_wake.UNREAD_SECONDS comes back once, uncharged, to the next waiter."""

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
from test_interactive_wake_round2 import NA, NB, NC, TWO, Hub, ack, call, on, rec, woke

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


# ------------------------------------------------------------ M2: O5, a delivered wake nobody read comes back


def delivered_unread(env, tid):
    call(env)
    post = ask(env, tid)
    assert woke(env) == text_for(env, 1, [tid])
    return post


def test_m2_a_wake_nobody_read_comes_back_once_and_is_not_charged_again(senv):
    on(senv)
    tid = senv.thread()
    post = delivered_unread(senv, tid)
    senv.clock.advance(iw.UNREAD_SECONDS - 1)
    assert call(senv, nonce=NB) == {"state": "idle"}, "not before UNREAD_SECONDS"
    senv.clock.advance(1)
    assert woke(senv, nonce=NB) == text_for(senv, 1, [tid]), "the session's next hook is offered it again"
    assert len(pair(senv)["sent"]) == 1 and pair(senv)["threads"][str(tid)]["count"] == 1, "not charged again"
    assert str(post["id"]) in pair(senv)["returned"]
    senv.clock.advance(iw.UNREAD_SECONDS + 1)
    for nonce in (NC, "d" * 32):
        assert call(senv, nonce=nonce) == {"state": "idle"}, "never a second time"


def test_m2_a_post_read_meanwhile_never_comes_back(senv):
    on(senv)
    tid = senv.thread()
    delivered_unread(senv, tid)
    reader = senv.board.register_session(senv.p["claude"], PROJECT)["session_id"]
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")      # the woken session read it
    senv.clock.advance(iw.UNREAD_SECONDS + 1)
    assert call(senv, nonce=NB) == {"state": "idle"}


def test_m2_a_post_another_session_read_past_never_comes_back(senv):
    on(senv)
    tid = senv.thread()
    delivered_unread(senv, tid)
    other = senv.board.register_session(senv.p["claude"], "/somewhere/else")["session_id"]
    out = senv.board.read_updates(senv.p["claude"], other)
    senv.board.ack(senv.p["claude"], other, out["ack_through"])              # read and acknowledged elsewhere
    senv.clock.advance(iw.UNREAD_SECONDS + 1)
    assert call(senv, nonce=NB) == {"state": "idle"}


def test_m2_the_dispatchers_hold_is_not_extended(senv):
    on(senv)
    tid = senv.thread()
    t0 = senv.clock()
    post = delivered_unread(senv, tid)
    senv.clock.advance(iw.UNREAD_SECONDS)
    assert woke(senv, nonce=NB) is not None, "returned and offered again"
    senv.clock.advance(GRACE - iw.UNREAD_SECONDS - 1)
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock())
    senv.clock.advance(1)
    assert senv.clock() == t0 + GRACE and not hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), \
        "it still ends at the request's first-offer deadline"


def test_m2_the_returned_wake_goes_to_the_next_waiter_that_arms_and_to_one_only(senv):
    on(senv)
    tid = senv.thread()
    call(senv, conv=TWO, nonce=NA)                      # another session of the agent, waiting since before
    senv.clock.advance(1)
    call(senv)                                          # armed last
    ask(senv, tid)
    assert woke(senv) is not None                       # delivered to CONV; its session took no turn and is gone
    senv.clock.advance(iw.UNREAD_SECONDS + 1)
    out = call(senv, conv=TWO, nonce=NB)                # TWO's next hook arms
    assert out["state"] == "wake" and out["text"] == text_for(senv, 1, [tid])
    assert call(senv, nonce=NC)["state"] == "idle", "offered to one conversation at a time"
    assert ack(senv, out, conv=TWO, nonce=NB) == {"state": "acked"}
    assert len(pair(senv)["sent"]) == 1, "and still charged once"
    assert rec(senv, TWO)["delivered"]


def test_m2_a_read_by_a_waiter_that_moved_on_to_another_project_still_counts(senv):
    """Found by the randomized run (seed 6): the read evidence of a waiter that moved to another project after it
    read the post was looked for among the old project's current waiters only, so O5 returned a read post. It is
    kept on the pair now, and every waiter's record counts wherever it is."""
    from test_hosted_sessions import P2
    on(senv)
    tid = senv.thread()
    call(senv, conv=TWO, nonce=NA)                      # another session of the agent, waiting in PROJECT
    senv.clock.advance(1)
    call(senv)
    post = ask(senv, tid)
    assert woke(senv) is not None
    reader = senv.board.register_session(senv.p["claude"], PROJECT, client=("claude-code", CONV))["session_id"]
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")      # woken, it reads the post
    assert call(senv, nonce=NB, projects=(P2,)) == {"state": "idle"}         # then works in another project
    senv.clock.advance(GRACE + 1)
    assert call(senv, conv=TWO, nonce=NC) == {"state": "idle"}, "a post that was read never comes back"
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "and the read still holds it"
