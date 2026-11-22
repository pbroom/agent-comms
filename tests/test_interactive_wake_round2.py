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
from agent_comms.core import LimitExceeded

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


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_randomized_offers_keep_their_invariants(senv, seed):
    """Interleaved arms, re-arms, project changes, posts, human posts, pauses, the setting turned off and on,
    dispatcher passes with reservations, acks in time and late, replayed acks, reads (filtered, and another session
    reading past posts with its cursor) and a hosted root that comes and goes. After every step: at most one standing
    offer per pair; every hold ends at its request's first-offer deadline unless the post was read after delivery.
    At every accepted ack: not paused, not off, no hosted root there; no post delivered to two conversations; every
    post of the batch still unread; each accepted wake charged once, for its own threads. A replayed ack is answered
    as the first was and charges nothing: an acknowledged batch wakes the client exactly once."""
    rnd = random.Random(seed)
    # Limits loose enough that many wakes happen, with a per-thread maximum the run reaches now and then.
    sessions = {"wake_min_interval_seconds": 2, "max_wakes_per_hour": 1000, "max_wakes_without_human": 3}
    on(senv, **sessions)
    projects = [PROJECT, P2]
    threads = {PROJECT: [senv.thread(), senv.thread()],
               P2: [senv.board.create_thread(senv.p["human"], senv.sid["human"], f"t{i}", P2)["id"] for i in range(2)]}
    convs = {c: {"nonce": None, "project": PROJECT, "n": 0} for c in (ONE, TWO, THREE)}
    readers, others = {}, {}
    issued: list[tuple] = []                     # (conv, nonce, project, offer id, threads, post keys)
    accepted: dict[str, tuple] = {}              # offer id -> (conv, nonce, project, when)
    deliveries: dict[str, list[str]] = {}       # post -> the conversations it was delivered to, in order
    came_back: set[str] = set()                # posts O5 returned (once each)
    expected = {(pr, t): 0 for pr in projects for t in threads[pr]}
    first_offer: dict[str, float] = {}
    posts: list[tuple[int, str]] = []
    flags = {"paused": False, "root": None, "off": False}
    seen = {"issued": 0, "acked": 0, "rejected": 0, "replayed": 0, "held": 0, "returned": 0}

    def new_nonce(c):
        convs[c]["n"] += 1
        convs[c]["nonce"] = f"{seed:02x}{ord(c[0]):02x}{convs[c]['n']:028x}"

    def look(c):
        v = convs[c]
        if v["nonce"] is None:
            new_nonce(c)
        out = call(senv, c, v["nonce"], (v["project"],))
        assert out["state"] != "wake" or not flags["off"]
        if out["state"] == "wake":
            seen["issued"] += 1
            r = rec(senv, c)
            issued.append((c, v["nonce"], v["project"], out["id"], dict(r["offer"]["entries"])))

    def unchanged(project, before) -> None:
        after = iw._pair(senv.board.conn, "claude", project)
        assert (after["sent"], after["threads"]) == (before["sent"], before["threads"]), "a replay charged again"

    def unread(keys) -> bool:
        for k in keys:
            row = senv.board.conn.execute("SELECT thread_id, seq FROM posts WHERE id = ?", (int(k),)).fetchone()
            cursor = senv.board.conn.execute("SELECT COALESCE(MAX(last_seq), 0) FROM cursors WHERE agent = 'claude' "
                                             "AND thread_id = ?", (row["thread_id"],)).fetchone()[0]
            if row["seq"] <= cursor:
                return False
        return True

    def do_ack(late):
        if not issued:
            return
        c, nonce, project, wake_id, entries = issued.pop(rnd.randrange(len(issued)))
        keys = list(entries)
        if late:
            senv.clock.advance(iw.ACK_SECONDS)
        before = iw._pair(senv.board.conn, "claude", project)
        was_unread = unread(keys)
        out = call(senv, c, nonce, (project,), ack=wake_id)
        if out["state"] != "acked":
            seen["rejected"] += 1
            return
        if wake_id in accepted:                 # the same id issued twice, acknowledged again: a replay
            seen["replayed"] += 1
            unchanged(project, before)
            return
        seen["acked"] += 1
        accepted[wake_id] = (c, nonce, project, senv.clock())
        assert not flags["paused"] and not flags["off"] and flags["root"] != project, "accepted when it may not be"
        assert was_unread, "accepted for a post another session had read past"
        for k in keys:
            got = deliveries.setdefault(k, [])
            assert not got or (k in came_back and len(got) == 1), f"post {k} delivered again without coming back"
            got.append(c)
        # Charged for the threads of the posts delivered for the first time (O5's returned ones are not charged).
        batch = sorted({e[0] for k, e in entries.items() if k not in before["returned"]})
        for t in batch:
            expected[(project, t)] += 1
        if not batch:
            unchanged(project, before)
        pair = iw._pair(senv.board.conn, "claude", project)
        for t in threads[project]:
            got = (pair["threads"].get(str(t)) or {}).get("count", 0)
            if t in batch:
                assert got == expected[(project, t)], (t, got, expected[(project, t)])
            else:
                assert got <= (before["threads"].get(str(t)) or {}).get("count", 0), "charged outside its batch"

    def replay():
        if not accepted:
            return
        wake_id = rnd.choice(list(accepted))
        c, nonce, project, when = accepted[wake_id]
        before = iw._pair(senv.board.conn, "claude", project)
        r = rec(senv, c)
        out = call(senv, c, nonce, (project,), ack=wake_id)
        if flags["off"]:
            assert out == {"state": "off"}, "nothing is answered acked while off"
        elif r is not None and (r["acked"] or {}).get("id") == wake_id and senv.clock() - when < iw.REPLAY_SECONDS:
            assert out == {"state": "acked"}, "the same answer as the first time"
            seen["replayed"] += 1
        else:
            assert out["state"] != "acked"
        unchanged(project, before)

    for step in range(400):
        delivered_before = {pr: dict(iw._pair(senv.board.conn, "claude", pr)["delivered"]) for pr in projects}
        op = rnd.choices(["look", "rearm", "move", "post", "human", "pause", "tick", "ack", "late", "root", "read",
                          "cursor", "replay", "flip", "time", "unread"],
                         weights=[8, 2, 1, 5, 1, 1, 3, 6, 1, 1, 2, 1, 2, 1, 3, 3])[0]
        c = rnd.choice(list(convs))
        if flags["paused"] and rnd.random() < 0.25:     # a pause lasts a few steps
            op = "pause"
        if flags["off"] and rnd.random() < 0.3:         # and so does the setting being off
            op = "flip"
        if op == "look":
            look(c)
        elif op == "rearm":
            new_nonce(c)
            look(c)
        elif op == "move":
            new_nonce(c)
            convs[c]["project"] = rnd.choice(projects)
            look(c)
        elif op == "post" and not flags["paused"]:          # agents cannot post while paused; the human can
            pr = rnd.choice(projects)
            t = rnd.choice(threads[pr])
            try:
                posts.append((ask(senv, t, as_=rnd.choice(["codex", "grok"]))["id"], pr))
            except LimitExceeded:                   # the board's own cap: the human steps into the thread
                senv.post("human", t, "human", to=[])
                expected[(pr, t)] = 0
        elif op == "human":
            pr = rnd.choice(projects)
            t = rnd.choice(threads[pr])
            senv.post("human", t, "human", to=[])
            expected[(pr, t)] = 0
        elif op == "pause":
            flags["paused"] = not flags["paused"]
            senv.board.set_paused(senv.p["human"], flags["paused"])
        elif op == "flip":
            flags["off"] = not flags["off"]
            if flags["off"]:
                senv.settings.dispatch = {**senv.settings.dispatch, "sessions": sessions}
            else:
                on(senv, **sessions)
        elif op == "tick":
            iw.tick(senv.board)
            if flags["off"]:                        # the pass dropped every record: the pairs start again
                expected = {key: 0 for key in expected}
            if posts and rnd.random() < 0.5:
                pid, pr = rnd.choice(posts)
                with db.write_tx(senv.board.conn):
                    hosted_wake.reserve(senv.board, "claude", pr, pid, senv.clock())
        elif op in ("ack", "late"):
            do_ack(op == "late")
        elif op == "replay":
            replay()
        elif op == "root":
            if flags["root"] is None:
                flags["root"] = rnd.choice(projects)
                hosted_root(senv, flags["root"])
            else:
                hosted_root(senv, flags["root"], present=False)
                flags["root"] = None
        elif op == "read" and not flags["paused"]:
            v = convs[c]
            key = (c, v["project"])
            if key not in readers:
                readers[key] = senv.board.register_session(senv.p["claude"], v["project"],
                                                           client=("claude-code", c))["session_id"]
            senv.board.read_updates(senv.p["claude"], readers[key], only="addressed")
        elif op == "cursor" and not flags["paused"]:
            pr = rnd.choice(projects)              # another session of the agent reads everything and acknowledges
            if pr not in others:
                others[pr] = senv.board.register_session(senv.p["claude"], pr)["session_id"]
            out = senv.board.read_updates(senv.p["claude"], others[pr])
            if out.get("ack_through"):
                senv.board.ack(senv.p["claude"], others[pr], out["ack_through"])
        elif op == "unread":                        # a delivered wake nobody acts on: time passes, no read,
            senv.clock.advance(iw.UNREAD_SECONDS + 1)   # and a waiter's next look
            look(c)
        elif op == "time":
            senv.clock.advance(rnd.choice([1, 1, 2, 3, 8, 25]))
        returned_now = returns(senv, projects, delivered_before)
        for k, pr in returned_now:
            assert k not in came_back, f"post {k} came back twice"
            assert unread([k]) and not iw.read_after_delivery(senv.board.conn, "claude", pr, int(k)), \
                f"post {k} came back although it was read"
            came_back.add(k)
            seen["returned"] += 1
        seen["held"] += check(senv, projects, first_offer, posts)
    assert (seen["issued"] >= 10 and seen["acked"] >= 3 and seen["rejected"] >= 1 and seen["replayed"] >= 1
            and seen["held"] and seen["returned"] >= 1), \
        ("the run exercised too little", seen)


def returns(env, projects, delivered_before) -> list[tuple[str, str]]:
    """The posts O5 returned during the last step: they left a pair's `delivered` set and wait in `returning` (or
    were offered at once). A post that left with its pair (the setting off and a pass's clean-up) is not one."""
    out = []
    for pr in projects:
        raw = iw._get(env.board.conn, iw._pair_key("claude", pr))
        if raw is None:
            continue
        pair = iw._pair(env.board.conn, "claude", pr)
        out += [(k, pr) for k in delivered_before[pr] if k not in pair["delivered"] and k in pair["returned"]]
    return out


def check(env, projects, first_offer, posts) -> int:
    conn, now = env.board.conn, env.clock()
    offered = [k for r in iw._records(conn, "claude").values() for k in (r["offer"] or {}).get("entries", {})]
    assert len(offered) == len(set(offered)), "a post offered to two conversations at once"
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
