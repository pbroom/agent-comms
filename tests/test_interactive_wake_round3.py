"""Review round 3 of the interactive wake (DESIGN_NOTES "Waking an interactive session: review round 3"): one or more
tests per finding (S19-S23), each written so that it failed on 55b6ed7 (the head under review) and passes with its
fix. The randomized run in test_interactive_wake_round2 covers these moves too (replays, the setting flipping, a
read past the posts between hand-over and ack)."""

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import httpx

from agent_comms import db, hosted_wake, interactive_wake as iw, waiter

from conftest import PROJECT
from test_hosted_sessions import HUB_DISPATCH, senv  # noqa: F401
from test_hosted_wake import ask, text_for
from test_interactive_wake_round2 import Hub, ack, call, on, rec, run_wait, woke

ROOT = Path(__file__).resolve().parents[1]
WAKE = hosted_wake.wake_text("claude", [[7, "codex", False, 1]])


def pair(env) -> dict:
    return iw._pair(env.board.conn, "claude", PROJECT)


# ------------------------------------------------------------ S19: only rejections in a row end the wait


def test_s19_isolated_rejections_over_a_long_session_do_not_disarm_the_hook():
    answers, acks = [], []
    for i in range(waiter.MAX_REJECTED + 1):                # more isolated rejections than the cap, each followed
        answers += [{"state": "wake", "text": WAKE, "id": f"{i:016x}"}, {"state": "idle"}]    # by an ordinary look
        acks.append({"state": "rejected"})
    answers.append({"state": "wake", "text": WAKE, "id": "f" * 16})
    acks.append({"state": "acked"})
    assert run_wait(Hub(*answers, ack=acks)) == (2, WAKE + "\n")


# ------------------------------------------------------------ S20: a lost reply to an accepted ack


def test_s20_the_same_nonce_replaying_an_accepted_ack_is_answered_acked_and_charged_once(senv):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    assert ack(senv, out) == {"state": "acked"}
    assert ack(senv, out) == {"state": "acked"}, "its reply was lost: the same answer again"
    assert len(pair(senv)["sent"]) == 1 and pair(senv)["threads"][str(tid)]["count"] == 1, "charged once"
    assert call(senv, nonce="b" * 32, ack=out["id"])["state"] != "acked", "only to the nonce it was given to"
    senv.clock.advance(iw.REPLAY_SECONDS)
    assert ack(senv, out)["state"] != "acked", "for a bounded time"


def test_s20_board_wait_retries_an_ack_whose_reply_it_could_not_read():
    hub = Hub({"state": "wake", "text": WAKE, "id": "1" * 16},
              ack=[httpx.Response(200, content=b"\x00garbled"), {"state": "acked"}])
    assert run_wait(hub) == (2, WAKE + "\n"), "the hub had committed it: the session is woken"
    assert [b.get("ack") for b in hub.bodies] == [None, "1" * 16, "1" * 16]


def test_s20_end_to_end_a_committed_ack_with_a_garbled_reply_still_wakes(senv, monkeypatch):
    """Against the real hub: the first reply to the ack is lost after the hub committed it."""
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    agent = senv.agent
    sent = []

    class Garble:
        def post(self, path, json, **kw):
            r = agent.post(path, json=json | {"wait": 0})           # never a real long poll in a test
            sent.append(json.get("ack"))
            if json.get("ack") is not None and sent.count(json["ack"]) == 1:
                return httpx.Response(200, content=b"{not json")     # committed at the hub; the reply is lost
            return httpx.Response(r.status_code, content=r.content, headers=r.headers)
    hook = {"session_id": "11111111-1111-4111-8111-111111111111", "cwd": "/work/repo", "hook_event_name": "Stop"}
    err = io.StringIO()
    t = [0.0]

    def clock():                                            # each look of board wait costs it 30 s: bounded
        t[0] += 30
        return t[0]
    code = waiter.run("claude", [PROJECT], 600, stdin=io.StringIO(json.dumps(hook)), stderr=err, environ={},
                      client=Garble(), sleep=lambda s: None, clock=clock, watch=False)
    assert (code, err.getvalue()) == (2, text_for(senv, 1, [tid]) + "\n")
    assert len(pair(senv)["sent"]) == 1


# ------------------------------------------------------------ S21: the setting, read inside the transaction


def test_s21_an_ack_waiting_for_the_lock_while_the_setting_is_turned_off_is_not_accepted(senv, monkeypatch):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    real = db.write_tx

    @contextlib.contextmanager
    def turned_off_meanwhile(conn):
        senv.settings.dispatch = HUB_DISPATCH
        with real(conn) as c:
            yield c
    monkeypatch.setattr(iw.db, "write_tx", turned_off_meanwhile)
    assert ack(senv, out)["state"] != "acked"
    monkeypatch.setattr(iw.db, "write_tx", real)
    assert rec(senv)["delivered"] == {}


def test_s21_no_offer_is_handed_over_once_the_setting_is_turned_off_in_the_transaction(senv, monkeypatch):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    real = db.write_tx

    @contextlib.contextmanager
    def turned_off_meanwhile(conn):
        senv.settings.dispatch = HUB_DISPATCH
        with real(conn) as c:
            yield c
    monkeypatch.setattr(iw.db, "write_tx", turned_off_meanwhile)
    assert call(senv)["state"] != "wake"


# ------------------------------------------------------------ S22: a read past the batch between hand-over and ack


def test_s22_an_ack_after_another_session_read_past_the_sole_post_is_not_accepted(senv):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    other = senv.board.register_session(senv.p["claude"], PROJECT)["session_id"]
    read = senv.board.read_updates(senv.p["claude"], other)
    senv.board.ack(senv.p["claude"], other, read["ack_through"])          # read and acknowledged elsewhere
    assert ack(senv, out)["state"] != "acked"
    assert rec(senv)["delivered"] == {} and pair(senv)["sent"] == [], "nothing delivered or charged"
    assert woke(senv) is None, "and it is not offered again: it has been read"


# ------------------------------------------------------------ S23: the e2e script keeps its evidence


def test_s23_the_e2e_script_keeps_its_logs_when_a_step_raises(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("iw_e2e_script", ROOT / "scripts" / "interactive-wake-e2e.py")
    e2e = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e2e)
    kept = tmp_path / "run"
    monkeypatch.setattr(e2e.tempfile, "mkdtemp", lambda prefix: (kept.mkdir(), str(kept))[1])

    def broken(self):
        raise RuntimeError("an unexpected failure")
    monkeypatch.setattr(e2e.Run, "setup", broken)
    monkeypatch.setattr(sys, "argv", ["interactive-wake-e2e.py"])
    monkeypatch.setattr(e2e.signal, "signal", lambda *a: None)     # not the script's SIGTERM handler in pytest
    try:
        e2e.main()
    except RuntimeError:
        pass
    assert kept.exists() and (kept / "logs").exists(), "the evidence is kept"
