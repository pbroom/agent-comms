"""Waking a session the human opened himself (interactive_wake.py, `board wait`, install.sh --wake): the hub's wait
route, the decision of whether and whom to wake, the dispatcher's hold, the CLI side and the installer. Real hub
routes and a fake clock, as in test_hosted_wake. Never a real agent CLI (scripts/session-host-e2e.py runs one)."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agent_comms import claude_hooks, delivery, gateway, hosted, hosted_wake, interactive_wake as iw, waiter
from agent_comms.dispatch import DispatchConfig, SessionsConfig

from conftest import PROJECT
from test_channel import INJECTION, _revoke
from test_hosted_sessions import HUB_DISPATCH, P2, client, senv  # noqa: F401
from test_hosted_wake import GRACE, MARKER, allow, ask, denv, launched, ready, text_for, wakes  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
ONE, TWO = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
NA, NB, NC = "a" * 32, "b" * 32, "c" * 32


def on(env, **sessions) -> None:
    env.settings.dispatch = {**env.settings.dispatch, "sessions": {"interactive": {"wake": True}, **sessions}}


def wait(env, conv=ONE, nonce=NA, who="claude", projects=(PROJECT,), ack=True, **kw) -> dict:
    """One call, as `board wait` makes it; a wake it is offered is acknowledged (as `board wait` does) unless
    ack=False, and returned without its id."""
    body = {"client_session_id": conv, "projects": list(projects), "nonce": nonce, **kw}
    r = client(env, who).post("/api/wait", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    if out.get("state") == "wake":
        wake_id = out.pop("id")
        assert iw.ACK_RE.fullmatch(wake_id)
        if ack:
            a = client(env, who).post("/api/wait", json=body | {"ack": wake_id})
            assert a.json() == {"state": "acked"}, a.text
            assert rec(env, conv, who)["offer"] is None
    return out


def pair(env, agent="claude", project=PROJECT) -> dict:
    return iw._pair(env.board.conn, agent, project)


def rec(env, conv=ONE, agent="claude") -> dict:
    return iw._load(env.board.conn, iw._key(agent, conv))


def state(env) -> list:
    return env.board.conn.execute("SELECT key, value, updated_at FROM board_state ORDER BY key").fetchall()


def woke(env, **kw) -> str | None:
    out = wait(env, **kw)
    assert out["state"] in ("wake", "idle"), out
    return out.get("text")


def holds(env, post, agent="claude", project=PROJECT) -> bool:
    return hosted.holds(env.board, agent, project, post["id"], env.clock())


def text(env, n, threads, authors="codex", needs=0) -> str:
    return text_for(env, n, threads, authors, needs)


# ------------------------------------------------------------ off by default


def test_off_by_default_the_route_answers_off_and_nothing_changes(senv):
    assert SessionsConfig().interactive.wake is False
    tid = senv.thread()
    before = state(senv)
    assert wait(senv) == {"state": "off"}
    post = ask(senv, tid)
    after_post = state(senv)
    assert wait(senv, nonce=NB, wait=1) == {"state": "off"}
    iw.tick(senv.board)
    assert state(senv) == after_post and not holds(senv, post), "no record, no hold, no write"
    assert [k for k, *_ in before if k.startswith(iw.PREFIX)] == []


def test_settings_are_parsed_strictly():
    c = DispatchConfig.from_dict({"sessions": {"interactive": {"wake": True}}})
    assert c.sessions.interactive.wake is True
    for bad, why in (({"interactive": {"wake": "yes"}}, "wake must be true or false"),
                     ({"interactive": {"wak": True}}, "unknown setting [dispatch.sessions.interactive] wak"),
                     ({"interactive": True}, "must be a table")):
        with pytest.raises(ValueError, match=why.replace("[", r"\[").replace("]", r"\]")):
            SessionsConfig.from_dict(bad)


def test_records_left_from_when_it_was_on_are_dropped_once_it_is_off(senv):
    on(senv)
    wait(senv)
    assert rec(senv) is not None
    senv.settings.dispatch = HUB_DISPATCH
    iw.tick(senv.board)
    assert rec(senv) is None


# ------------------------------------------------------------ the wake


def test_a_post_addressed_to_the_agent_wakes_its_waiting_session_once(senv):
    on(senv)
    tid = senv.thread()
    assert wait(senv) == {"state": "idle"}, "the first call makes the record: woken for posts from now on"
    post = ask(senv, tid)
    assert wait(senv) == {"state": "wake", "text": text(senv, 1, [tid])}
    r = rec(senv)
    assert r["delivered"] == {str(post["id"]): senv.clock()} and r["pending"] == {} and r["offer"] is None
    assert wait(senv, nonce=NB) == {"state": "idle"}, "the hook armed again after the wake turn: one post, one wake"


def test_history_is_not_replayed_and_only_qualifying_posts_wake(senv):
    on(senv)
    tid = senv.thread()
    other = senv.board.create_thread(senv.p["human"], senv.sid["human"], "elsewhere", P2)["id"]
    ask(senv, tid, body="before the session waited")
    assert woke(senv) is None
    ask(senv, tid, to=["grok"])                         # addressed to another agent
    ask(senv, tid, to=[])                               # to nobody
    ask(senv, other)                                    # a thread of another project
    ask(senv, tid, as_="claude", type="status")         # the agent's own post
    assert woke(senv) is None and rec(senv)["pending"] == {}
    assert woke(senv, conv=TWO, projects=(P2,)) is None, "the other project's waiter starts from now"
    ask(senv, other, as_="grok")
    assert woke(senv, conv=TWO, projects=(P2,)) == text(senv, 1, [other], authors="grok")
    assert woke(senv) is None


def test_the_project_is_the_first_one_sent_whether_or_not_it_has_threads(senv):
    on(senv)
    tid = senv.thread()
    wait(senv, projects=(PROJECT + "/", "/somewhere/worktree"))
    assert rec(senv)["project"] == PROJECT
    ask(senv, tid)
    assert woke(senv) == text(senv, 1, [tid])
    wait(senv, conv=TWO, nonce=NB, projects=("/somewhere/new", PROJECT))
    assert rec(senv, TWO)["project"] == "/somewhere/new", "bound to the canonical project the client names"


def test_an_inactive_author_a_sealed_post_and_a_revoked_agent(senv):
    on(senv)
    tid = senv.thread()
    wait(senv)
    task = senv.accepted_task(tid, title="t")
    sealed = ask(senv, tid, body="SEALED " + MARKER, type="finding", task_id=task, sealed=True,
                 refs=[{"kind": "commit", "path": "/work/repo", "rev": "abc"}])
    assert woke(senv) is None
    senv.board.unseal(senv.p["human"], sealed["id"])
    assert woke(senv) == text(senv, 1, [tid]), "a sealed post wakes once it is unsealed"
    senv.clock.advance(31)
    ask(senv, tid)
    _revoke(senv, "codex")
    assert woke(senv) is None and rec(senv)["pending"] == {}, "its author is no longer an active identity"
    _revoke(senv, "claude")
    r = client(senv, "claude").post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT],
                                                       "nonce": NA})
    assert r.status_code == 401


def test_nothing_is_decided_while_paused_and_the_post_goes_after_the_unpause(senv):
    on(senv)
    tid = senv.thread()
    wait(senv)
    senv.board.set_paused(senv.p["human"], True)
    senv.post("human", tid, "for when we are back " + MARKER, "request", to=["claude"])
    before = state(senv)
    assert wait(senv) == {"state": "idle"}
    iw.tick(senv.board)
    assert state(senv) == before, "no write while paused"
    senv.board.set_paused(senv.p["human"], False)
    assert woke(senv) == text(senv, 1, [tid], authors="human")


def test_nothing_an_agent_wrote_reaches_the_wake_text_or_the_record(senv):
    on(senv)
    wait(senv)
    tid = senv.board.create_thread(senv.p["codex"], senv.sid["codex"], "TITLE " + INJECTION, PROJECT)["id"]
    senv.board.set_summary(senv.p["codex"], senv.sid["codex"], tid, "SUMMARY " + INJECTION)
    task = senv.board.create_task(senv.p["codex"], senv.sid["codex"], tid, title="TASKTITLE " + INJECTION)["id"]
    ask(senv, tid, body=INJECTION, task_id=task, needs_response=True,
        refs=[{"kind": "file", "path": "/work/repo/REFPATH-" + MARKER, "rev": "abc"}])
    out = wait(senv)
    assert out == {"state": "wake", "text": text(senv, 1, [tid], needs=1)}
    stored = json.dumps(rec(senv))
    for hay in (json.dumps(out), stored):
        for needle in ("IGNORE", "rm -rf", "human.token", "TITLE", "SUMMARY", "TASKTITLE", "REFPATH", MARKER):
            assert needle not in hay, needle
    assert waiter.WAKE_RE.fullmatch(out["text"]), "the CLI passes it on"


# ------------------------------------------------------------ limits


def test_the_interval_coalesces_posts_into_the_next_wake(senv):
    on(senv)
    t1, t2 = senv.thread(), senv.thread()
    wait(senv)
    ask(senv, t1)
    assert woke(senv) == text(senv, 1, [t1])
    ask(senv, t1)
    ask(senv, t2, as_="grok", needs_response=True)
    senv.clock.advance(29)
    assert woke(senv, nonce=NB) is None, "inside wake_min_interval_seconds"
    senv.clock.advance(1)
    assert woke(senv, nonce=NB) == text(senv, 2, [t1, t2], authors="codex, grok", needs=1)


def test_the_hourly_maximum_holds_posts_until_the_hour_has_room(senv):
    on(senv, max_wakes_per_hour=2, wake_min_interval_seconds=0)
    tid = senv.thread()
    wait(senv)
    reader = senv.session("claude")             # the woken session reads what it was woken for
    for _ in range(2):
        ask(senv, tid)
        assert woke(senv) is not None
        senv.board.read_updates(senv.p["claude"], reader, only="addressed")
        senv.clock.advance(60)
    ask(senv, tid)
    assert woke(senv) is None and len(rec(senv)["pending"]) == 1
    senv.clock.advance(3600 - 121)
    assert woke(senv) is None
    senv.clock.advance(1)
    assert woke(senv) == text(senv, 1, [tid]), "the oldest wake left the hour"
    assert senv.board.snapshot(senv.p["human"])["needs_you"] == [], "no Needs-you item for a waiter"


def test_the_thread_maximum_holds_until_the_human_posts_there(senv):
    on(senv, max_wakes_without_human=2, wake_min_interval_seconds=0)
    tid, other = senv.thread(), senv.thread()
    wait(senv)
    for _ in range(2):
        ask(senv, tid)
        assert woke(senv) is not None
    ask(senv, tid)
    assert woke(senv) is None and pair(senv)["threads"][str(tid)]["stopped"] == "thread"
    ask(senv, other)
    assert woke(senv) == text(senv, 1, [other]), "another thread is not stopped"
    senv.post("human", tid, "carry on")
    assert woke(senv) == text(senv, 1, [tid]), "a post of the human's there lifts the stop; the waiting post goes"


# ------------------------------------------------------------ whom: supersede, one per project, hosted first


def test_a_newer_process_of_the_same_conversation_supersedes_the_older(senv):
    on(senv)
    tid = senv.thread()
    wait(senv, nonce=NA)
    wait(senv, nonce=NB)
    assert wait(senv, nonce=NA) == {"state": "superseded"}
    ask(senv, tid)
    assert wait(senv, nonce=NA) == {"state": "superseded"}, "never woken once superseded"
    assert woke(senv, nonce=NB) == text(senv, 1, [tid])
    assert wait(senv, nonce=NB, who="codex") == {"state": "idle"}, "another agent's conversation is its own record"


def test_one_waiter_per_agent_and_project_the_one_armed_last(senv):
    on(senv)
    tid = senv.thread()
    wait(senv, conv=ONE)
    senv.clock.advance(1)
    wait(senv, conv=TWO, nonce=NB)
    post = ask(senv, tid)
    assert woke(senv, conv=ONE) is None, "not the most recently active one"
    assert woke(senv, conv=TWO, nonce=NB) == text(senv, 1, [tid])
    senv.clock.advance(31)      # the second session's turn runs (its hook is not waiting); past the interval
    assert woke(senv, conv=ONE) is None, "a post another waiter was woken for wakes nobody else"
    assert str(post["id"]) not in rec(senv, ONE)["pending"]
    ask(senv, tid, as_="grok")
    assert woke(senv, conv=ONE) == text(senv, 1, [tid], authors="grok"), "the only connected one now"


def test_a_hosted_root_session_of_the_agent_in_the_project_is_woken_instead(senv):
    on(senv)
    tid = senv.thread()
    wait(senv)
    sid = ready(senv)
    ask(senv, tid)
    hosted_wake_tick(senv)
    assert woke(senv) is None and len(wakes(senv, sid)) == 1
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    from test_hosted_sessions import tick
    tick(senv, times=2)
    senv.clock.advance(31)
    ask(senv, tid, as_="grok")
    assert woke(senv) == text(senv, 1, [tid], authors="grok"), "the hosted session ended: the waiter is woken"


def hosted_wake_tick(env):
    from agent_comms import hosted_wake
    hosted_wake.tick(env.board)


def test_bad_bodies_and_the_human_are_refused(senv):
    on(senv)
    agent = client(senv, "claude")
    for body in ({"client_session_id": "nope", "projects": [PROJECT], "nonce": NA},
                 {"client_session_id": ONE, "projects": [PROJECT], "nonce": "short"},
                 {"client_session_id": ONE, "projects": [], "nonce": NA},
                 {"client_session_id": ONE, "projects": ["relative"], "nonce": NA},
                 {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA, "client": "other"}):
        assert agent.post("/api/wait", json=body).status_code == 400, body
    assert agent.post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT], "nonce": NA,
                                         "extra": 1}).status_code == 422
    assert senv.human.post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT],
                                              "nonce": NA}).status_code == 403
    assert senv.laptop.post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT],
                                               "nonce": NA}).status_code == 403, "a node identity"
    assert any(m == "POST" and rx.fullmatch("/api/wait") for m, rx in gateway.API_ROUTES)


def test_an_open_call_waits_and_returns_the_wake_when_it_comes(senv):
    on(senv)
    tid = senv.thread()
    wait(senv)
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(wait(senv, wait=10)))
    t.start()
    import time
    time.sleep(1.5)
    ask(senv, tid)
    t.join(15)
    assert out == {"state": "wake", "text": text(senv, 1, [tid])}


# ------------------------------------------------------------ the dispatcher and read marks


def test_holds_within_the_grace_then_only_with_the_waiters_own_read(senv):
    on(senv)
    tid = senv.thread()
    wait(senv)
    post = ask(senv, tid)
    assert not holds(senv, post)
    wait(senv)
    assert holds(senv, post) and holds(senv, post, project=PROJECT + "/")
    assert not hosted.holds(senv.board.conn, "claude", PROJECT, post["id"], senv.clock()), \
        "a bare connection cannot tell whether the feature is on: it holds nothing"
    assert not holds(senv, post, agent="codex") and not holds(senv, post, project=P2)
    senv.clock.advance(GRACE - 1)
    assert holds(senv, post)
    senv.clock.advance(1)
    assert not holds(senv, post), "after the grace, unread"
    other = senv.board.register_session(senv.p["claude"], PROJECT, client=("claude-code", TWO))["session_id"]
    senv.board.read_updates(senv.p["claude"], other, only="addressed")
    assert not holds(senv, post), "a board session that is another conversation's"
    mine = senv.board.register_session(senv.p["claude"], PROJECT, client=("claude-code", ONE))["session_id"]
    senv.board.read_updates(senv.p["claude"], mine, only="addressed")
    assert rec(senv)["read"] == {str(post["id"]): senv.clock()} and holds(senv, post)
    senv.clock.advance(3600)
    assert holds(senv, post)


def test_a_dispatched_runs_read_does_not_count(senv):
    on(senv)
    tid = senv.thread()
    wait(senv)
    post = ask(senv, tid)
    wait(senv)
    senv.clock.advance(GRACE)
    senv.board.conn.execute("UPDATE sessions SET dispatch_run_id = 'run-x' WHERE id = ?", (senv.sid["claude"],))
    senv.board.read_updates(senv.p["claude"], senv.sid["claude"])
    assert delivery.receipt(senv.board.conn, post["id"], "claude") is not None
    assert not holds(senv, post)


def test_the_dispatcher_does_not_launch_beside_a_waiter_it_just_woke_and_does_after_the_grace(denv):
    on(denv)
    allow(denv)
    wait(denv)
    post = denv.post("human", denv.tid, "your turn", "request", to=["claude"])
    denv.d.tick()               # the dispatcher's own pass offers the wake before it decides
    assert launched(denv) == [] and list(rec(denv)["offer"]["entries"]) == [str(post["id"])]
    assert woke(denv) == text(denv, 1, [denv.tid], authors="human"), "the open call hands it"
    denv.clock.advance(GRACE - 20)
    denv.d.tick()
    assert launched(denv) == [], "inside the grace"
    denv.clock.advance(30)
    denv.d.tick()
    assert launched(denv) == ["claude-fake"], "after the grace with no read: exactly as without a waiter"
    assert str(post["id"]) in rec(denv)["claimed"], "the request is the dispatcher's now"


def test_a_waiter_that_read_the_request_keeps_it_from_the_dispatcher(denv):
    on(denv)
    allow(denv)
    wait(denv)
    denv.post("human", denv.tid, "your turn", "request", to=["claude"])
    assert woke(denv) is not None
    denv.board.read_updates(denv.p["claude"], denv.session("claude"))       # the woken session reads the board
    denv.clock.advance(30 * 60)
    denv.d.tick()
    assert launched(denv) == []


def test_a_wake_nobody_acknowledged_is_taken_back_and_offered_again(senv):
    """The hub cannot see that the caller of an open call has gone: a wake offered into a dead call is not lost."""
    on(senv)
    tid = senv.thread()
    wait(senv)
    post = ask(senv, tid)
    first = client(senv, "claude").post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT],
                                                           "nonce": NA}).json()
    assert first["state"] == "wake" and holds(senv, post), "offered (the process died before acknowledging): held"
    senv.clock.advance(iw.ACK_SECONDS - 1)
    again = client(senv, "claude").post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT],
                                                           "nonce": NA}).json()
    assert again == first, "the same offer while it stands, to the same process"
    senv.clock.advance(1)
    assert pair(senv)["sent"] == [], "never acknowledged: not counted"
    assert woke(senv, nonce=NB) == text(senv, 1, [tid]), "taken back and offered to the conversation's next process"
    stale = client(senv, "claude").post("/api/wait", json={"client_session_id": ONE, "projects": [PROJECT],
                                                           "nonce": NB, "ack": "f" * 16})
    assert stale.json() == {"state": "rejected"}, "an id that is not the open offer's acknowledges nothing"


def test_the_dispatchers_pass_takes_back_an_offer_nobody_acknowledged(senv):
    on(senv)
    tid = senv.thread()
    wait(senv, conv=ONE)
    senv.clock.advance(1)
    wait(senv, conv=TWO, nonce=NB)
    ask(senv, tid)
    assert woke(senv, conv=TWO, nonce=NB, ack=False) is not None    # the session armed last; it died meanwhile
    senv.clock.advance(iw.ACK_SECONDS)
    iw.tick(senv.board)
    senv.clock.advance(iw.PRESENT_SECONDS - iw.ACK_SECONDS + 1)
    iw.tick(senv.board)
    assert rec(senv, TWO)["offer"] is None, "its waiter is gone: taken back"
    assert woke(senv, conv=ONE) == text(senv, 1, [tid]), "the other waiting session of the agent gets it"


def test_the_dispatchers_pass_writes_nothing_for_a_waiter_with_nothing_new(senv):
    on(senv)
    senv.thread()
    wait(senv)
    before = state(senv)
    iw.tick(senv.board)
    assert state(senv) == before
    senv.clock.advance(iw.KEEP_SECONDS + 1)
    iw.tick(senv.board)
    assert rec(senv) is None, "a waiter gone for KEEP_SECONDS is dropped"


def test_a_wake_queued_by_the_dispatcher_loses_a_post_read_meanwhile(denv):
    on(denv)
    wait(denv)
    tid = denv.thread()
    post = ask(denv, tid)
    denv.d.tick()
    assert list(rec(denv)["offer"]["entries"]) == [str(post["id"])]
    s = denv.session("claude")
    out = denv.board.read_updates(denv.p["claude"], s)
    denv.board.ack(denv.p["claude"], s, out["ack_through"])
    assert woke(denv) is None and rec(denv)["offer"] is None and pair(denv)["sent"] == [], "dropped, not counted"


# ------------------------------------------------------------ board wait (the CLI side)


class FakeHub:
    def __init__(self, *answers):
        self.answers, self.bodies = list(answers), []

    def post(self, path, json, **kw):
        assert path == "/api/wait"
        self.bodies.append(json)
        if json.get("ack") is not None:
            return httpx.Response(200, json={"state": "acked"})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return httpx.Response(a[0], json=a[1]) if isinstance(a, tuple) else httpx.Response(200, json=a)


def run_wait(hub, stdin=None, **kw):
    err = io.StringIO()
    hook = {"session_id": ONE, "cwd": "/work/repo", "hook_event_name": "Stop"}
    code = waiter.run("claude", [PROJECT], 600, stdin=io.StringIO(json.dumps(hook) if stdin is None else stdin),
                      stderr=err, environ={}, client=hub, sleep=lambda s: None, watch=False, **kw)
    return code, err.getvalue()


WAKE = hosted_wake.wake_text("claude", [[4, "codex", True, 9]])


def test_board_wait_exits_2_with_the_wake_text_and_0_otherwise():
    hub = FakeHub({"state": "idle"}, {"state": "wake", "text": WAKE, "id": "0" * 16})
    assert run_wait(hub) == (2, WAKE + "\n")
    body = hub.bodies[0]
    assert body["client_session_id"] == ONE and body["projects"] == [PROJECT] and len(body["nonce"]) == 32
    assert hub.bodies[1]["nonce"] == body["nonce"], "one nonce per process"
    assert hub.bodies[2] == body | {"ack": "0" * 16, "wait": 0}, "the wake is acknowledged before the exit"
    class AckLost(FakeHub):
        def post(self, path, json, **kw):
            if json.get("ack") is not None:
                raise httpx.ConnectError("gone")
            return super().post(path, json)
    assert run_wait(AckLost({"state": "wake", "text": WAKE, "id": "1" * 16})) == (2, WAKE + "\n"), \
        "woken even when the acknowledgement could not be delivered"
    for answers in ([{"state": "off"}], [{"state": "superseded"}], [(401, {"error": "unauthorized"})],
                    [{"state": "wake", "text": "not the fixed form", "id": "0" * 16}],
                    [{"state": "wake", "text": WAKE + "\nlines", "id": "0" * 16}], [{"state": "something new"}],
                    [{"state": "wake", "text": WAKE}],
                    [httpx.ConnectError("refused")]):
        assert run_wait(FakeHub(*answers)) == (0, ""), answers
    assert run_wait(FakeHub(), stdin="not json") == (0, "")
    assert run_wait(FakeHub(), stdin=json.dumps({"session_id": "x"})) == (0, "")


def test_board_wait_rides_out_a_short_outage_then_gives_up():
    t = [0.0]
    hub = FakeHub({"state": "idle"}, httpx.ConnectError("x"), (502, {}),
                  {"state": "wake", "text": WAKE, "id": "0" * 16})
    assert run_wait(hub, clock=lambda: t[0])[0] == 2
    errors = [httpx.ConnectError("x")] * 100

    def clock():
        t[0] += 10
        return t[0]
    assert run_wait(FakeHub({"state": "idle"}, *errors), clock=clock) == (0, "")


def test_board_wait_ends_when_its_time_is_up():
    t = [0.0]

    def clock():
        t[0] += 300
        return t[0]
    hub = FakeHub(*[{"state": "idle"}] * 10)
    assert run_wait(hub, clock=clock) == (0, "") and len(hub.bodies) == 1
    assert hub.bodies[0]["wait"] == waiter.CALL_SECONDS


def test_board_wait_leaves_when_claude_is_gone():
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    import threading
    left = threading.Event()
    waiter.WATCH_SECONDS, old = 0.05, waiter.WATCH_SECONDS
    try:
        waiter._watch({"CLAUDE_PID": str(gone.pid)}, left.set)
        assert left.wait(2), "the Claude Code process that ran the hook has exited"
        alive = threading.Event()
        waiter._watch({"CLAUDE_PID": str(os.getpid())}, alive.set)
        assert not alive.wait(0.3)
    finally:
        waiter.WATCH_SECONDS = old


def test_wait_sh_passes_on_only_the_wake_text(tmp_path):
    """The hook script never wakes the session for anything but `board wait`'s own wake (an exit 2 of uv or
    argparse must not, or each such wake would start the hook that wakes it again)."""
    shim = tmp_path / "integrations" / "claude-code"
    shim.mkdir(parents=True)
    (shim / "wait.sh").write_text((ROOT / "integrations/claude-code/wait.sh").read_text())
    for rc, err, expect in ((2, WAKE, 2), (2, "usage: board ...", 0), (2, "agent-comms: x", 0),
                            (2, WAKE.replace("thread 4", "thread 4\x07"), 0), (0, WAKE, 0), (1, "boom", 0)):
        (tmp_path / "err").write_text(err)
        (shim / "board.sh").write_text(f"cat >/dev/null; cat '{tmp_path / 'err'}' >&2; exit {rc}\n")
        r = subprocess.run(["bash", str(shim / "wait.sh")], input="{}", capture_output=True, text=True, timeout=10)
        assert (r.returncode, r.stderr) == (expect, err + "\n" if expect else ""), (rc, err)
        assert r.stdout == ""


# ------------------------------------------------------------ the installer


def test_claude_hooks_add_is_idempotent_and_remove_takes_out_only_ours(tmp_path):
    f = tmp_path / "settings.json"
    mine = {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}],
                                    "PreToolUse": [{"matcher": "Bash",
                                                    "hooks": [{"type": "command", "command": "b"}]}]}}
    f.write_text(json.dumps(mine))
    os.chmod(f, 0o640)
    cmd = claude_hooks.command("/b", "claude", "/x/wait.sh")
    add = ["add", "--settings", str(f), "--script", "/x/wait.sh", "--home", "/b", "--agent", "claude"]
    for _ in range(2):
        assert claude_hooks.main(add) == 0
    got = json.loads(f.read_text())
    ours = {"type": "command", "command": cmd, "asyncRewake": True, "timeout": 86400}
    assert got["hooks"]["Stop"] == mine["hooks"]["Stop"] + [{"hooks": [ours]}], "Stop: as before"
    assert got["hooks"]["SessionStart"] == [{"hooks": [ours | {"async": True}]}], "SessionStart: async too"
    assert got["model"] == "x" and got["hooks"]["PreToolUse"] == mine["hooks"]["PreToolUse"]
    assert f.stat().st_mode & 0o777 == 0o640
    assert claude_hooks.main(["remove", "--settings", str(f), "--script", "/x/wait.sh"]) == 0
    assert json.loads(f.read_text()) == mine
    f.write_text("[1]")
    assert claude_hooks.main(add) == 1
    assert f.read_text() == "[1]"
    assert claude_hooks.main(["remove", "--settings", str(tmp_path / "none.json"), "--script", "/x/wait.sh"]) == 0
    assert not (tmp_path / "none.json").exists()


def test_install_sh_wake_and_remove_wake_touch_only_the_temp_settings(tmp_path):
    """Against a temporary HOME and CLAUDE_CONFIG_DIR: never the real ~/.claude."""
    from agent_comms.config import create_agent, save_agent_token  # noqa: F401
    home, board_home, cfg = tmp_path / "home", tmp_path / "board", tmp_path / "claude-config"
    (home / ".config" / "agent-comms").mkdir(parents=True)
    board_home.mkdir()
    create_agent(board_home / "agents.toml", "human", "human", is_human=True)
    token = create_agent(board_home / "agents.toml", "claude", "claude-code")
    (home / ".config" / "agent-comms" / "claude.token").write_text(token + "\n")
    os.chmod(home / ".config" / "agent-comms" / "claude.token", 0o600)
    uv_cache = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True).stdout.strip()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_COMMS", "CLAUDE"))} | {
        "HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg), "AGENT_COMMS_HOME": str(board_home),
        "UV_CACHE_DIR": uv_cache}
    script = ROOT / "integrations/claude-code/install.sh"
    for _ in range(2):
        r = subprocess.run(["bash", str(script), "--wake"], env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
    got = json.loads((cfg / "settings.json").read_text())
    [stop], [start] = got["hooks"]["Stop"], got["hooks"]["SessionStart"]
    assert start["hooks"][0] == stop["hooks"][0] | {"async": True}, "the SessionStart entry also has async"
    assert stop["hooks"][0]["asyncRewake"] is True and stop["hooks"][0]["timeout"] == 86400
    assert stop["hooks"][0]["command"] == (f"AGENT_COMMS_HOME='{board_home}' AGENT_COMMS_AGENT='claude' "
                                           f"bash '{ROOT}/integrations/claude-code/wait.sh'")
    assert (cfg / "settings.json").stat().st_mode & 0o777 == 0o600
    assert not (home / ".claude").exists() and not (home / ".claude.json").exists()
    r = subprocess.run(["bash", str(script), "--remove-wake"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and json.loads((cfg / "settings.json").read_text()) == {}
    r = subprocess.run(["bash", str(script), "--wake", "--agent", "nobody"], env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode != 0 and json.loads((cfg / "settings.json").read_text()) == {}, "a token that fails: no hook"


def test_claude_hooks_upgrade_an_entry_an_earlier_version_wrote(tmp_path):
    """Earlier versions wrote SessionStart like Stop ("asyncRewake" alone). --wake again replaces it with the new
    shape, once; --remove-wake takes out either shape; nothing else is touched."""
    f = tmp_path / "settings.json"
    cmd = claude_hooks.command("/b", "claude", "/x/wait.sh")
    old = {"type": "command", "command": cmd, "asyncRewake": True, "timeout": 86400}
    other = {"hooks": [{"type": "command", "command": "echo /x/wait.sh", "async": True}]}
    f.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [old]}, other], "Stop": [{"hooks": [old]}]}}))
    add = ["add", "--settings", str(f), "--script", "/x/wait.sh", "--home", "/b", "--agent", "claude"]
    for _ in range(2):
        assert claude_hooks.main(add) == 0
    got = json.loads(f.read_text())["hooks"]
    assert got["SessionStart"] == [other, {"hooks": [old | {"async": True}]}], "the old entry replaced, once"
    assert got["Stop"] == [{"hooks": [old]}]
    assert claude_hooks.main(["remove", "--settings", str(f), "--script", "/x/wait.sh"]) == 0
    assert json.loads(f.read_text()) == {"hooks": {"SessionStart": [other]}}
    f.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [old]}], "Stop": [{"hooks": [old]}]}}))
    assert claude_hooks.main(["remove", "--settings", str(f), "--script", "/x/wait.sh"]) == 0
    assert json.loads(f.read_text()) == {}, "an old install is removed whole"
    odd = old | {"async": False}                        # not a shape this module ever wrote
    f.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [odd]}]}}))
    assert claude_hooks.main(["remove", "--settings", str(f), "--script", "/x/wait.sh"]) == 0
    assert json.loads(f.read_text()) == {"hooks": {"SessionStart": [{"hooks": [odd]}]}}
