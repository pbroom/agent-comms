"""Review round 4 of the interactive wake (DESIGN_NOTES "Waking an interactive session: review round 4"): S21 and S23
in their narrower form (each test failed on 122a659), and tests that pin the two declined findings, S24 and S25: a
replay of an accepted ack stays valid after a take-over and after a pause began, because the hub had already
committed it."""

import contextlib
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

from agent_comms import db, interactive_wake as iw

from conftest import PROJECT
from test_hosted_sessions import HUB_DISPATCH, senv  # noqa: F401
from test_hosted_wake import ask, text_for
from test_interactive_wake_round2 import NA, NB, TWO, ack, call, on, rec

ROOT = Path(__file__).resolve().parents[1]


def waiter_state(env) -> list:
    return env.board.conn.execute("SELECT key, value FROM board_state WHERE key LIKE 'wake.%' ORDER BY key").fetchall()


def pair(env) -> dict:
    return iw._pair(env.board.conn, "claude", PROJECT)


def off_as_the_lock_is_taken(env, monkeypatch) -> None:
    """From now on, the setting is turned off just as any write transaction of the module starts."""
    real = db.write_tx

    @contextlib.contextmanager
    def flipping(conn):
        env.settings.dispatch = HUB_DISPATCH
        with real(conn) as c:
            yield c
    monkeypatch.setattr(iw.db, "write_tx", flipping)


# ------------------------------------------------------------ S21: the setting, re-read once the lock is held


def test_s21_a_first_poll_turned_off_while_it_waits_for_the_lock_writes_nothing(senv, monkeypatch):
    on(senv)
    off_as_the_lock_is_taken(senv, monkeypatch)
    assert call(senv) == {"state": "off"}
    assert waiter_state(senv) == [], "no waiter and no pair record"


def test_s21_a_read_mark_is_not_written_once_the_setting_is_off(senv, monkeypatch):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    assert ack(senv, out) == {"state": "acked"}
    reader = senv.board.register_session(senv.p["claude"], PROJECT)["session_id"]
    off_as_the_lock_is_taken(senv, monkeypatch)
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")
    assert rec(senv)["read"] == {}


def test_s21_the_dispatchers_pass_makes_no_offer_once_the_setting_is_off(senv, monkeypatch):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    off_as_the_lock_is_taken(senv, monkeypatch)
    iw.tick(senv.board)
    assert (rec(senv) or {}).get("offer") is None


# ------------------------------------------------------------ S23: a survivor keeps the logs


def test_s23_a_process_that_survives_cleanup_keeps_the_logs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("iw_e2e_script_r4", ROOT / "scripts" / "interactive-wake-e2e.py")
    e2e = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e2e)
    kept = tmp_path / "run"
    monkeypatch.setattr(e2e.tempfile, "mkdtemp", lambda prefix: (kept.mkdir(), str(kept))[1])
    run = e2e.Run(SimpleNamespace(keep=False, claude_bin=None, max_wake=5, max_real_wake=60))

    class Survivor:
        pid = 2 ** 22 + 7              # no such process: cleanup's signal goes nowhere

        def poll(self):
            return None

        def send_signal(self, sig):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("survivor", timeout)
    run.procs.append(Survivor())
    run.ok = True                       # every step passed; only the survivor is wrong
    run.cleanup()
    assert run.failures and "processes left" in run.failures[0]
    assert kept.exists() and (kept / "logs").exists(), "the logs that explain it are kept"


# ------------------------------------------------------------ S24, S25 (declined): pinning the intended behaviour


def test_s24_a_replaced_process_replaying_its_accepted_ack_is_answered_acked_once_charged(senv):
    """Ruling: the hub committed that ack (posts delivered, wake charged); refusing the replay would wake nobody for
    it. The replaced process wakes the same conversation once; the newer one is not offered those posts again."""
    on(senv)
    tid = senv.thread()
    call(senv, nonce=NA)
    post = ask(senv, tid)
    out = call(senv, nonce=NA)
    assert ack(senv, out, nonce=NA) == {"state": "acked"}       # committed; its reply was unreadable
    assert call(senv, nonce=NB) == {"state": "idle"}            # the hook ran again: NB takes the conversation over
    assert ack(senv, out, nonce=NA) == {"state": "acked"}, "the replaced process gets the answer it was owed"
    assert len(pair(senv)["sent"]) == 1 and pair(senv)["threads"][str(tid)]["count"] == 1, "charged once"
    assert call(senv, conv=TWO, nonce=NA, ack=out["id"])["state"] != "acked", "never for another conversation"
    assert call(senv, nonce=NB, ack=out["id"])["state"] != "acked", "nor for another process of this one"
    reader = senv.board.register_session(senv.p["claude"], PROJECT)["session_id"]
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")      # the woken session reads it
    for _ in range(3):
        senv.clock.advance(31)
        assert call(senv, nonce=NB) == {"state": "idle"}, "the newer process is not offered those posts again"
    assert str(post["id"]) in rec(senv)["delivered"] and rec(senv)["offer"] is None
    tid2 = senv.thread()
    ask(senv, tid2)
    assert call(senv, nonce=NB)["text"] == text_for(senv, 1, [tid2]), "and is woken for new posts as usual"


def test_s25_a_replay_just_after_a_pause_began_is_answered_acked_and_nothing_new_moves(senv):
    """Ruling: the ack was accepted before the pause; the replay repeats the answer owed, within the window. While
    paused, no new offer is made, handed over or acknowledged."""
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    assert ack(senv, out) == {"state": "acked"}
    senv.board.set_paused(senv.p["human"], True)
    assert ack(senv, out) == {"state": "acked"}, "the answer it was owed"
    senv.post("human", tid, "while paused", "request", to=["claude"])
    senv.clock.advance(31)
    assert call(senv) == {"state": "idle"}, "no new offer while paused"
    iw.tick(senv.board)
    assert rec(senv)["offer"] is None and len(pair(senv)["sent"]) == 1, "nothing new offered or charged"
    senv.board.set_paused(senv.p["human"], False)
    later = call(senv)
    assert later["state"] == "wake" and later["text"] == text_for(senv, 1, [tid], authors="human")
    assert later["id"] != out["id"]
