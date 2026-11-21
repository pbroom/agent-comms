"""Review round 2 of the interactive wake (DESIGN_NOTES "Waking an interactive session: review round 2"): one or more
tests per finding (S4, S14-S19), each written so that it failed on f05a3c3 (the head under review) and passes with
its fix; and a seeded randomized run of the hub that checks the offer state machine's invariants after every step."""

import contextlib
import io
import json
import random

import httpx
import pytest

from agent_comms import db, hosted, hosted_wake, interactive_wake as iw, waiter

from conftest import PROJECT
from test_hosted_sessions import P2, client, senv  # noqa: F401
from test_hosted_wake import GRACE, ask, text_for

ONE, TWO, THREE = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                   "33333333-3333-4333-8333-333333333333")
NA, NB, NC = "a" * 32, "b" * 32, "c" * 32
WAKE = hosted_wake.wake_text("claude", [[7, "codex", False, 1]])
ROOT_ID = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"


def on(env, **sessions) -> None:
    env.settings.dispatch = {**env.settings.dispatch, "sessions": {"interactive": {"wake": True}, **sessions}}


def call(env, conv=ONE, nonce=NA, projects=(PROJECT,), **kw) -> dict:
    r = client(env, "claude").post("/api/wait", json={"client_session_id": conv, "projects": list(projects),
                                                      "nonce": nonce, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def ack(env, out, conv=ONE, nonce=NA, projects=(PROJECT,)) -> dict:
    return call(env, conv, nonce, projects, ack=out["id"])


def woke(env, conv=ONE, nonce=NA, projects=(PROJECT,)) -> str | None:
    out = call(env, conv, nonce, projects)
    if out["state"] != "wake":
        return None
    assert ack(env, out, conv, nonce, projects) == {"state": "acked"}
    return out["text"]


def rec(env, conv=ONE) -> dict:
    return iw._load(env.board.conn, iw._key("claude", conv))


def hosted_root(env, project=PROJECT, present=True) -> None:
    """A hosted root session of claude in the project, as hosted.py stores one (no node needed for this)."""
    key = hosted.SESSION_PREFIX + ROOT_ID
    with db.write_tx(env.board.conn) as c:
        if present:
            hosted._put(c, key, {"session_id": ROOT_ID, "agent": "claude", "project": project, "state": "idle",
                                 "node": "laptop"}, "test", env.clock())
        else:
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))


# ------------------------------------------------------------ S4: the clock and the client's reading of the reply


def test_s4_an_ack_that_waited_for_the_write_lock_past_the_deadline_is_rejected(senv, monkeypatch):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    assert out["state"] == "wake"
    real = db.write_tx

    @contextlib.contextmanager
    def slow(conn):
        senv.clock.advance(iw.ACK_SECONDS)      # the lock came free only after the deadline
        with real(conn) as c:
            yield c
    monkeypatch.setattr(iw.db, "write_tx", slow)
    assert ack(senv, out)["state"] != "acked"


@pytest.mark.parametrize("reply", [b"not json", b'{"state": "something new"}', b"[]"])
def test_s4_an_unreadable_200_to_the_ack_never_wakes(reply):
    hub = Hub({"state": "wake", "text": WAKE, "id": "0" * 16}, {"state": "off"}, ack=httpx.Response(200, content=reply))
    assert run_wait(hub) == (0, ""), "not taken for an accepted acknowledgement"


# ------------------------------------------------------------ S14: re-checked inside the ack's transaction


def test_s14_no_ack_is_accepted_while_the_board_is_paused(senv):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    senv.board.set_paused(senv.p["human"], True)
    assert ack(senv, out)["state"] != "acked"
    assert rec(senv)["delivered"] == {}


def test_s14_no_ack_is_accepted_once_a_hosted_root_takes_precedence(senv):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    hosted_root(senv)
    assert ack(senv, out)["state"] != "acked"
    assert rec(senv)["delivered"] == {}


def test_s14_the_dispatchers_offer_is_checked_against_a_pause_inside_its_transaction(senv, monkeypatch):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    real, calls = hosted_wake.limits, []

    def pause_meanwhile(board):
        calls.append(1)
        if len(calls) == 2:             # the pass's own read of the limits: after it looked at the pause
            senv.board.set_paused(senv.p["human"], True)
        return real(board)
    monkeypatch.setattr(iw.hosted_wake, "limits", pause_meanwhile)
    iw.tick(senv.board)
    assert rec(senv)["offer"] is None


# ------------------------------------------------------------ S15: a project change while an offer is open


def test_s15_a_conversation_that_moves_projects_drops_the_old_projects_offer(senv):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    assert call(senv)["state"] == "wake"                   # offered in PROJECT, not acknowledged
    assert call(senv, nonce=NB, projects=(P2,)) == {"state": "idle"}
    assert rec(senv)["project"] == P2 and rec(senv)["offer"] is None
    for _ in range(3):
        assert call(senv, nonce=NB, projects=(P2,))["state"] == "idle", "PROJECT's wake never reaches it in P2"


# ------------------------------------------------------------ S16: a reservation shrinking an issued batch


def test_s16_a_reservation_of_one_post_invalidates_the_issued_id(senv):
    on(senv)
    t1, t2 = senv.thread(), senv.thread()
    call(senv)
    p1, _ = ask(senv, t1), ask(senv, t2)
    out = call(senv)
    assert out["text"] == text_for(senv, 2, [t1, t2])     # issued: the client has this text
    iw.claim(senv.board, "claude", PROJECT, p1["id"], senv.clock())
    assert ack(senv, out)["state"] != "acked", "that id names two posts; one of them is the dispatcher's now"
    assert woke(senv) == text_for(senv, 1, [t2]), "the rest come again under a new id"


# ------------------------------------------------------------ S17: de-duplication that outlives waiter changes


def test_s17_a_delivered_post_wakes_no_second_conversation_after_the_first_moves_on(senv):
    on(senv)
    tid = senv.thread()
    call(senv, conv=TWO, nonce=NB)
    senv.clock.advance(1)
    call(senv, conv=ONE)                                  # armed last
    ask(senv, tid)
    assert woke(senv, conv=ONE) is not None
    call(senv, conv=ONE, nonce=NC, projects=(P2,))        # ONE's next process works in another project
    for _ in range(3):
        senv.clock.advance(15)
        assert woke(senv, conv=TWO, nonce=NB) is None, "already delivered in the pair"


# ------------------------------------------------------------ S18: the human's post before the charge


def test_s18_a_human_post_between_offer_and_ack_does_not_erase_the_charge(senv):
    on(senv, max_wakes_without_human=1, wake_min_interval_seconds=0)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    senv.post("human", tid, "noted", to=[])               # the human posts there before the session acknowledges
    assert ack(senv, out) == {"state": "acked"}
    ask(senv, tid)
    assert woke(senv) is None, "one wake since the human's post: the thread is at its maximum"


# ------------------------------------------------------------ S19: a recoverable rejection


def test_s19_a_rejected_ack_goes_on_waiting_with_the_same_nonce():
    hub = Hub({"state": "wake", "text": WAKE, "id": "1" * 16}, {"state": "wake", "text": WAKE, "id": "2" * 16},
              ack=[{"state": "rejected"}, {"state": "acked"}])
    assert run_wait(hub) == (2, WAKE + "\n")
    assert len({b["nonce"] for b in hub.bodies}) == 1 and [b.get("ack") for b in hub.bodies] == [
        None, "1" * 16, None, "2" * 16]
    endless = Hub(*[{"state": "wake", "text": WAKE, "id": "1" * 16}] * 10, ack=[{"state": "rejected"}] * 10)
    assert run_wait(endless) == (0, "") and len(endless.bodies) == 2 * waiter.MAX_REJECTED, "and gives up in time"
    for final in ({"state": "superseded"}, {"state": "off"}):
        assert run_wait(Hub({"state": "wake", "text": WAKE, "id": "1" * 16}, ack=[final])) == (0, "")


def test_s19_end_to_end_an_offer_that_expires_in_transit_is_offered_again(senv):
    on(senv)
    tid = senv.thread()
    call(senv)
    ask(senv, tid)
    out = call(senv)
    senv.clock.advance(iw.ACK_SECONDS)
    assert ack(senv, out) == {"state": "rejected"}, "recoverable: the nonce is still the conversation's"
    assert woke(senv) == text_for(senv, 1, [tid]), "the same process is offered it again"


# ------------------------------------------------------------ the state machine, driven at random


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_randomized_offers_keep_their_invariants(senv, seed):
    """Interleaved arms, re-arms, project changes, posts, human posts, pauses, dispatcher passes with reservations,
    acks in time and late, reads and a hosted root that comes and goes. After every step: at most one standing offer
    per pair; no post delivered to two conversations; no ack accepted while paused or beside a hosted root; each
    accepted wake charged once, for its own threads; every hold ends at its request's first-offer deadline unless the
    post was read after delivery."""
    rnd = random.Random(seed)
    # Limits loose enough that many wakes happen, with a per-thread maximum the run reaches now and then.
    on(senv, wake_min_interval_seconds=2, max_wakes_per_hour=1000, max_wakes_without_human=3)
    projects = [PROJECT, P2]
    threads = {PROJECT: [senv.thread(), senv.thread()],
               P2: [senv.board.create_thread(senv.p["human"], senv.sid["human"], f"t{i}", P2)["id"] for i in range(2)]}
    convs = {c: {"nonce": None, "project": PROJECT, "n": 0} for c in (ONE, TWO, THREE)}
    readers = {}
    issued: list[tuple] = []                     # (conv, nonce, project, offer id, threads)
    delivered_to: dict[str, str] = {}
    expected = {(pr, t): 0 for pr in projects for t in threads[pr]}
    first_offer: dict[str, float] = {}
    posts: list[tuple[int, str]] = []
    paused, root = False, None
    seen = {"issued": 0, "acked": 0, "rejected": 0, "held": 0}

    def new_nonce(c):
        convs[c]["n"] += 1
        convs[c]["nonce"] = f"{seed:02x}{ord(c[0]):02x}{convs[c]['n']:028x}"

    def look(c):
        v = convs[c]
        if v["nonce"] is None:
            new_nonce(c)
        out = call(senv, c, v["nonce"], (v["project"],))
        if out["state"] == "wake":
            seen["issued"] += 1
            r = rec(senv, c)
            issued.append((c, v["nonce"], v["project"], out["id"],
                           sorted({e[0] for e in r["offer"]["entries"].values()}), list(r["offer"]["entries"])))

    def do_ack(late):
        if not issued:
            return
        c, nonce, project, wake_id, batch, keys = issued.pop(rnd.randrange(len(issued)))
        if late:
            senv.clock.advance(iw.ACK_SECONDS)
        pair_before = iw._pair(senv.board.conn, "claude", project)
        out = call(senv, c, nonce, (project,), ack=wake_id)
        if out["state"] != "acked":
            seen["rejected"] += 1
            return
        seen["acked"] += 1
        assert not paused and root != project, "accepted while paused or beside a hosted root"
        for k in keys:
            assert delivered_to.setdefault(k, c) == c, f"post {k} delivered to two conversations"
        for t in batch:
            expected[(project, t)] += 1
        pair = iw._pair(senv.board.conn, "claude", project)
        for t in threads[project]:
            got = (pair["threads"].get(str(t)) or {}).get("count", 0)
            if t in batch:
                assert got == expected[(project, t)], (t, got, expected[(project, t)])
            else:
                assert got <= (pair_before["threads"].get(str(t)) or {}).get("count", 0), "charged outside its batch"

    for step in range(300):
        op = rnd.choices(["look", "rearm", "move", "post", "human", "pause", "tick", "ack", "late", "root", "read",
                          "time"], weights=[8, 2, 1, 5, 1, 1, 3, 6, 1, 1, 2, 3])[0]
        c = rnd.choice(list(convs))
        if op == "look":
            look(c)
        elif op == "rearm":
            new_nonce(c)
            look(c)
        elif op == "move":
            new_nonce(c)
            convs[c]["project"] = rnd.choice(projects)
            look(c)
        elif op == "post" and not paused:           # agents cannot post while paused; the human can
            pr = rnd.choice(projects)
            posts.append((ask(senv, rnd.choice(threads[pr]), as_=rnd.choice(["codex", "grok"]))["id"], pr))
        elif op == "human":
            pr = rnd.choice(projects)
            t = rnd.choice(threads[pr])
            senv.post("human", t, "human", to=[])
            expected[(pr, t)] = 0
        elif op == "pause" or (paused and rnd.random() < 0.25):     # a pause lasts a few steps
            paused = not paused
            senv.board.set_paused(senv.p["human"], paused)
        elif op == "tick":
            iw.tick(senv.board)
            if posts and rnd.random() < 0.5:
                pid, pr = rnd.choice(posts)
                with db.write_tx(senv.board.conn):
                    hosted_wake.reserve(senv.board, "claude", pr, pid, senv.clock())
        elif op in ("ack", "late"):
            do_ack(op == "late")
        elif op == "root":
            if root is None:
                root = rnd.choice(projects)
                hosted_root(senv, root)
            else:
                hosted_root(senv, root, present=False)
                root = None
        elif op == "read" and not paused:
            v = convs[c]
            key = (c, v["project"])
            if key not in readers:
                readers[key] = senv.board.register_session(senv.p["claude"], v["project"],
                                                           client=("claude-code", c))["session_id"]
            senv.board.read_updates(senv.p["claude"], readers[key], only="addressed")
        elif op == "time":
            senv.clock.advance(rnd.choice([1, 1, 2, 3, 8, 25]))
        seen["held"] += check(senv, projects, first_offer, posts)
    assert seen["issued"] >= 10 and seen["acked"] >= 3 and seen["rejected"] >= 1 and seen["held"], \
        ("the run exercised too little", seen)


def check(env, projects, first_offer, posts) -> int:
    conn, now = env.board.conn, env.clock()
    for pr in projects:
        recs = iw._records(conn, "claude", pr)
        standing = [k for k, r in recs.items() if r["offer"] and iw._stands(env.board, conn, k, r, now, recs)]
        assert len(standing) <= 1, standing
        for r in recs.values():
            for k in (r["offer"] or {}).get("entries", {}):
                first_offer.setdefault(k, r["offer"]["at"])
        pair = iw._pair(conn, "claude", pr)
        for k, until in pair["holds"].items():
            assert until == first_offer[k] + GRACE, "one deadline per request, from its first offer"
    held = 0
    for pid, pr in posts:
        k = str(pid)
        if hosted.holds(env.board, "claude", pr, pid, now):
            held += 1
            until = iw._pair(conn, "claude", pr)["holds"].get(k)
            assert until is not None
            assert now < until or iw.read_after_delivery(conn, "claude", pr, pid), "a hold past its deadline"
    return held


# ------------------------------------------------------------ helpers


class Hub:
    """A fake hub for `board wait`: answers in order; acknowledgements get `ack` (one reply, or one per ack)."""

    def __init__(self, *answers, ack=None):
        self.answers, self.bodies = list(answers), []
        self.acks = ack if isinstance(ack, list) else None
        self.ack = {"state": "acked"} if ack is None else ack

    @staticmethod
    def _answer(a):
        if isinstance(a, httpx.Response):
            return a
        if isinstance(a, Exception):
            raise a
        return httpx.Response(a[0], json=a[1]) if isinstance(a, tuple) else httpx.Response(200, json=a)

    def post(self, path, json, **kw):
        assert path == "/api/wait"
        self.bodies.append(json)
        if json.get("ack") is not None:
            return self._answer(self.acks.pop(0) if self.acks is not None else self.ack)
        return self._answer(self.answers.pop(0) if self.answers else {"state": "off"})


def run_wait(hub):
    err = io.StringIO()
    hook = {"session_id": ONE, "cwd": "/work/repo", "hook_event_name": "Stop"}
    code = waiter.run("claude", [PROJECT], 600, stdin=io.StringIO(json.dumps(hook)), stderr=err, environ={},
                      client=hub, sleep=lambda s: None, watch=False)
    return code, err.getvalue()
