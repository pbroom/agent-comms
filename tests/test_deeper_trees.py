"""Deeper trees (DESIGN_NOTES "Session host: deeper trees"): a child session starts children of its own, up to
[dispatch.sessions.spawn] max_depth (1 to 4, 2 as shipped). Each rule the spawn tools hold at depth 1 is pinned here
at depth 2 or more: who is calling, access narrowed along the whole chain (also inside the deciding transaction), the
grant's budget across every level and root, the per-parent cap, request ids per starter, the allowance (live, and off
for a whole subtree), results one level up as the next turn, the turn count across the tree, the cascade, capacity
at a small max_live, placement and isolation, and the audit lines. Real hub routes and the node's SessionRunner with
fake processes, as in test_hosted_spawn; never a real agent CLI."""

import asyncio
import json

import httpx
import pytest

from agent_comms import hosted, hosted_results, hosted_spawn as hs, hosted_tree, nodes
from agent_comms.core import Conflict, Forbidden, NotFound

from conftest import PROJECT
from test_hosted_sessions import children, make_node, msg, send, tick  # noqa: F401
from test_hosted_spawn import (a_root, bound, budget, grant, interactive, kid_turns, listing, posts_in, results_of,
                               revoke, spawn, tree_rec, with_spawn)
from test_hosted_tree import (_reports_follow_the_command_line, answer, proc_of, root, senv, stored,  # noqa: F401
                              tree_config, wtenv)


@pytest.fixture(autouse=True)
def _no_codex_home_of_the_users(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def chain(env, levels=2, **spawn_kw) -> list[str]:
    """A hosted root and `levels` agent-started sessions under it, each started by the one above (each opened by its
    node, so that its own access is known): [root, child, grandchild, ...]."""
    out = [a_root(env)]
    for _ in range(levels):
        out.append(spawn(env, bound(env, out[-1]), **spawn_kw)["child"]["id"])
        tick(env)
    return out


def state(env, sid) -> tuple:
    r = stored(env, sid)
    return r["state"], r.get("stop")


def live_procs(env, node=None) -> int:
    return sum(1 for p in children(env, node) if p.code is None)


# ------------------------------------------------------------ identity


def test_identity_a_grandchild_is_known_by_its_own_binding_and_reaches_only_its_own_children(senv):
    grant(senv)
    sid, kid, grand = chain(senv)
    r = stored(senv, grand)
    assert (r["parent"], r["root"], r["depth"], r["spawned_by"]["session"]) == (kid, sid, 2, kid)
    shown = listing(senv)
    assert {x: shown[x]["bind"] for x in (sid, kid, grand)} == {x: hs.bind_key(senv.board.conn, x)
                                                                for x in (sid, kid, grand)}
    caller = hs.resolve(senv.board, senv.p["claude"], bound(senv, grand))
    assert (caller.kind, caller.id, caller.depth, caller.root) == ("hosted", grand, 2, sid)
    assert [c["id"] for c in hs.status(senv.board, senv.p["claude"], bound(senv, kid))["children"]] == [grand]
    assert [c["id"] for c in hs.status(senv.board, senv.p["claude"], bound(senv, sid))["children"]] == [kid]
    for who, other in ((sid, grand), (grand, kid), (grand, sid)):
        with pytest.raises(NotFound, match="no child session of yours"):
            hs.send(senv.board, senv.p["claude"], bound(senv, who), other, message="x")
        with pytest.raises(NotFound, match="no child session of yours"):
            hs.cancel(senv.board, senv.p["claude"], bound(senv, who), other)
    with pytest.raises(Forbidden, match="does not verify"):
        hs.spawn(senv.board, senv.p["claude"], hs.Ident(hosted=grand, key=hs.bind_key(senv.board.conn, kid),
                                                         claimed=True), task="x")
    g = [x for x in hs.list_grants(senv.board, senv.p["human"])["grants"]][0]
    revoke(senv, g)
    shown = listing(senv)
    assert "bind" in shown[sid] and "bind" in shown[kid], "each still has a live child it started"
    assert "bind" not in shown[grand], "no grant and no children of its own: its binding is withdrawn"


# ------------------------------------------------------------ access along the whole chain


def test_access_a_grandchild_never_exceeds_any_session_above_it(senv):
    grant(senv)
    sid = a_root(senv)
    kid = spawn(senv, bound(senv, sid), access="read-only")["child"]["id"]
    tick(senv)
    with pytest.raises(Conflict, match="above what you may give a child now"):
        spawn(senv, bound(senv, kid), access="workspace")
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    assert stored(senv, grand)["access"] == "read-only", "the default is the narrowest of the chain"
    assert hs.resolve(senv.board, senv.p["claude"], bound(senv, kid)).ceiling == "read-only"
    wide = spawn(senv, bound(senv, sid), access="workspace")["child"]["id"]
    tick(senv)
    # The root's node now says the root runs at read-only (its runner was edited): the whole chain is read, not only
    # the caller's own word.
    assert senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "idle", "access": "read-only",
                                                                 "ceiling": "read-only"}).status_code == 200
    assert hs.resolve(senv.board, senv.p["claude"], bound(senv, wide)).ceiling == "read-only"
    with pytest.raises(Conflict, match="above what you may give a child now"):
        spawn(senv, bound(senv, wide), access="workspace")
    assert stored(senv, spawn(senv, bound(senv, wide))["child"]["id"])["access"] == "read-only"


def test_access_a_grant_that_narrows_inside_the_deciding_transaction_gives_a_wider_grandchild_nothing(senv,
                                                                                                       monkeypatch):
    with_spawn(senv, max_live_children=10)
    wide = grant(senv, access="workspace")
    grant(senv, access="read-only")
    sid, kid = chain(senv, levels=1)
    real = hs._revalidate

    def narrowed(c2, *a, **kw):        # the wide grant is revoked while the transaction runs, before the enqueue
        g = hs._get(c2, hs.GRANT_PREFIX + str(wide["id"]))
        hs._put(c2, hs.GRANT_PREFIX + str(wide["id"]), g | {"revoked_at": senv.clock(), "revoked_by": "human"},
                "human", senv.clock())
        return real(c2, *a, **kw)
    monkeypatch.setattr(hs, "_revalidate", narrowed)
    before = len(hosted._sessions(senv.board.conn))
    with pytest.raises(Conflict, match=r"access workspace is above what you may give a child now \(read-only\)"):
        spawn(senv, bound(senv, kid))
    assert len(hosted._sessions(senv.board.conn)) == before, "nothing made"
    monkeypatch.undo()
    hs._put(senv.board.conn, hs.GRANT_PREFIX + str(wide["id"]),
            hs._get(senv.board.conn, hs.GRANT_PREFIX + str(wide["id"])) | {"revoked_at": None}, "human", senv.clock())
    senv.board.conn.commit()
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    assert stored(senv, grand)["access"] == "workspace"
    revoke(senv, wide)
    with pytest.raises(Conflict, match=f"child session {grand} has workspace access, above what you may give"):
        hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, message="more")
    with pytest.raises(Conflict, match=f"child session {kid} has workspace access"):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="more")
    assert stored(senv, spawn(senv, bound(senv, kid))["child"]["id"])["access"] == "read-only"


# ------------------------------------------------------------ the budget, the per-parent cap, request ids


def test_budget_counts_every_descendant_at_every_level_and_root_and_max_children_is_each_parents(senv):
    with_spawn(senv, max_live_children=3, max_children=1)
    grant(senv)
    sid, kid, grand = chain(senv)
    with pytest.raises(Conflict, match="already has 1 child sessions"):
        spawn(senv, bound(senv, sid))           # the root's own cap (one), whatever its child has
    assert hs._usage(senv.board.conn, "claude", PROJECT, senv.clock())["live"] == 2
    ichild = spawn(senv, interactive(senv))["child"]["id"]      # another root of the same agent in the project
    tick(senv)
    with pytest.raises(Conflict, match="limit: 3 child sessions claude started in this project"):
        spawn(senv, bound(senv, ichild))        # counted across every level and every root
    assert len(budget(senv)["tasks"]) == 3, "one task turn charged per start, at each level"


def test_a_retry_with_the_same_request_id_is_scoped_to_its_starter_at_every_level(senv):
    with_spawn(senv, max_live_children=10)
    grant(senv)
    sid = a_root(senv)
    kid = spawn(senv, bound(senv, sid), request_id="job")["child"]["id"]
    kid2 = spawn(senv, bound(senv, sid), request_id="job-2")["child"]["id"]
    tick(senv)
    grand = spawn(senv, bound(senv, kid), request_id="job")
    assert grand["created"] and grand["child"]["id"] != kid, "the root's id says nothing for its child"
    again = spawn(senv, bound(senv, kid), request_id="job")
    assert (again["created"], again["child"]["id"]) == (False, grand["child"]["id"])
    other = spawn(senv, bound(senv, kid2), request_id="job")
    assert other["created"] and other["child"]["id"] not in (kid, grand["child"]["id"]), "a sibling's is its own"
    assert spawn(senv, bound(senv, sid), request_id="job")["child"]["id"] == kid
    assert len(budget(senv)["tasks"]) == 4, "the retries charged nothing"


# ------------------------------------------------------------ the allowance


def test_allowance_off_for_a_session_is_off_for_its_whole_subtree_and_a_parent_gives_no_more_than_its_own(senv):
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    sid, kid, grand = chain(senv)
    calls = len(senv.node.session_spawner.calls)
    assert hs.capabilities(senv.board, senv.p["claude"], bound(senv, grand))["may_spawn"], "on by default, depth 2"
    hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, spawn_share=1)
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, may_spawn=False)       # the parent, live
    with pytest.raises(Forbidden, match="your allowance says you may not"):
        spawn(senv, bound(senv, kid))
    with pytest.raises(Forbidden, match="switched off for a session above you"):
        spawn(senv, bound(senv, grand))
    caps = hs.capabilities(senv.board, senv.p["claude"], bound(senv, grand))
    assert not caps["may_spawn"] and caps["allowance"] == {"may_spawn": True, "share": 1, "off_above": True}
    assert hs.OFF_ABOVE in caps["why_not"]
    with pytest.raises(Forbidden, match="more than you have yourself"):
        hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, spawn_share=-1)
    hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, may_spawn=False)     # narrowing: always
    with pytest.raises(Forbidden, match="more than you have yourself"):
        hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, may_spawn=True)
    assert senv.human.post(f"/api/hosted-sessions/{kid}/allowance", json={"may_spawn": True, "share": 2}).json()[
        "allowance"] == {"may_spawn": True, "share": 2}                                   # the human, live
    with pytest.raises(Forbidden, match="your allowance says"):
        spawn(senv, bound(senv, grand))         # its own is still off
    with pytest.raises(Forbidden, match="your own share is 2"):
        hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, may_spawn=True, spawn_share=3)
    hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, may_spawn=True, spawn_share=2)
    great = spawn(senv, bound(senv, grand))["child"]["id"]
    assert stored(senv, great)["depth"] == 3
    assert stored(senv, great)["allowance"] == {"may_spawn": True, "share": 2}, \
        "a child's default allowance is never wider than its starter's own (its share)"
    assert len(senv.node.session_spawner.calls) == calls, "no process was restarted for any of it"


def _wider(a: dict, b: dict) -> bool:
    """The test's own reading of "wider": may_spawn true over false; a share of none (unlimited) over any number, a
    larger number over a smaller one."""
    return (a["may_spawn"] and not b["may_spawn"]) or (
        b["share"] is not None and (a["share"] is None or a["share"] > b["share"]))


def test_r1_d1_a_parent_never_leaves_its_child_an_allowance_wider_than_its_own_effective_one(senv):
    """Review round 1, D1. A table over the child's stored allowance, the call's arguments, and the parent's own
    allowance with the session above it on or off: whatever the arguments, the RESULT is never wider than the
    parent's own effective allowance unless it was not widened at all (a narrowing leaves what the human set), and
    nothing is widened while the parent or one above it is off."""
    import itertools
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    sid, above, kid, grand = chain(senv, levels=3)        # `kid` is the parent here; `above` is the one above it

    def put(sid_, allowance):
        with hosted.db.write_tx(senv.board.conn) as c:
            r = hosted._session(c, sid_)
            hosted._put(c, hosted.SESSION_PREFIX + sid_, r | {"allowance": allowance}, "test", senv.clock())

    stored_kids = [{"may_spawn": m, "share": s} for m in (True, False) for s in (None, 1, 3)]
    parents = [{"may_spawn": m, "share": s} for m in (True, False) for s in (None, 2)]
    arguments = [(m, s) for m in (None, True, False) for s in (None, -1, 1, 2, 3) if (m, s) != (None, None)]
    seen = {"refused": 0, "widened": 0, "narrowed": 0}
    for child_a, parent_a, above_on, (may, share) in itertools.product(stored_kids, parents, (True, False),
                                                                        arguments):
        put(grand, child_a)
        put(kid, parent_a)
        put(above, {"may_spawn": above_on, "share": None})
        eff = {"may_spawn": parent_a["may_spawn"] and above_on, "share": parent_a["share"]}
        want = dict(child_a, **({"may_spawn": may} if may is not None else {}),
                    **({"share": None if share < 0 else share} if share is not None else {}))
        widening = _wider(want, child_a)
        try:
            hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, may_spawn=may, spawn_share=share)
            refused = False
        except Forbidden as e:
            assert "more than you have yourself" in e.message
            refused = True
        got = stored(senv, grand)["allowance"]
        case = (child_a, parent_a, above_on, may, share)
        assert refused == (widening and (not eff["may_spawn"] or _wider(want, eff))), case
        assert got == (child_a if refused else want), case
        if widening and not refused:
            assert eff["may_spawn"] and not _wider(got, eff), case
        seen["refused" if refused else "widened" if widening else "narrowed"] += 1
    assert all(seen.values()), seen


# ------------------------------------------------------------ results and turns


def test_results_go_up_level_by_level_each_as_the_next_turn_of_the_direct_parent(senv):
    grant(senv)
    sid = a_root(senv)
    kid = spawn(senv, bound(senv, sid), task="plan it")["child"]["id"]
    tick(senv)
    grand = spawn(senv, bound(senv, kid), task="the leaf part")["child"]["id"]     # during the child's turn
    answer(senv, kid, "a helper is on it")
    tick(senv)
    assert [x["child"] for x in results_of(senv, sid)] == [kid] and msg(senv, sid, 2)["kind"] == "child"
    assert "the leaf part" in kid_turns(senv, grand)[0]
    answer(senv, grand, "leaf answer")
    assert [x["child"] for x in results_of(senv, kid)] == [grand], "to its direct parent"
    assert [x["child"] for x in results_of(senv, sid)] == [kid], "nothing skips a level"
    tick(senv)
    assert msg(senv, kid, 2)["kind"] == "child" and "leaf answer" in kid_turns(senv, kid)[-1]
    assert hosted_results.HEADER.split("{count}")[0] in kid_turns(senv, kid)[-1]
    answer(senv, kid, "combined: leaf answer")
    answer(senv, sid, "noted")
    tick(senv)
    [first, second] = results_of(senv, sid)
    assert (second["child"], second["started_by"], second["outcome"]) == (kid, "child", "completed"), \
        "the child's turn on its helper's result is a result of its own for the root"
    assert msg(senv, sid, 3)["kind"] == "child" and "combined: leaf answer" in proc_of(senv, sid)[1].turns()[-1]


def test_order_the_humans_message_then_child_results_at_a_middle_level(senv):
    grant(senv)
    sid, kid, grand = chain(senv)
    answer(senv, kid, "first")                  # the child is free; its helper works
    send(senv, kid, "busy")                      # the human gives the child a turn
    tick(senv)
    answer(senv, grand, "leaf")                  # a result for the child while it is busy
    send(senv, kid, "the human again")          # queued before the result can be delivered
    answer(senv, kid, "done busy")
    tick(senv)
    kinds = [m["kind"] for m in hosted._messages(senv.board.conn, kid)]
    assert kinds == ["task", "user", "user"], "the human's message that waits goes first"
    answer(senv, kid, "done again")
    tick(senv)
    assert [m["kind"] for m in hosted._messages(senv.board.conn, kid)] == ["task", "user", "user", "child"]


def test_claims_are_per_parent_and_a_waiter_at_a_middle_level_is_told_when_another_call_took_it(senv, monkeypatch):
    grant(senv)
    sid, kid, grand = chain(senv)
    answer(senv, grand, "leaf")
    caller, among = hs.claim(senv.board, senv.p["claude"], bound(senv, sid))
    assert hosted_results.claim(senv.board, caller.id, "claude", child=among) is None, "the root sees none of it"
    caller, among = hs.claim(senv.board, senv.p["claude"], bound(senv, kid))
    took = hosted_results.claim(senv.board, caller.id, "claude", child=among)
    assert (took["child"], took["state"]) == (grand, "claimed")
    tick(senv)
    assert all(m["kind"] != "child" for m in hosted._messages(senv.board.conn, kid)), "claimed: never a turn"
    monkeypatch.setenv("AGENT_COMMS_TOKEN", senv.tokens["claude"])
    monkeypatch.setenv(hs.HOSTED_ENV, kid)
    monkeypatch.setenv(hs.KEY_ENV, bound(senv, kid).key)

    async def another_call_takes_it(board, parent, by, child=None, message=None, wait=0, gone=None):
        tick(senv)
        answer(senv, grand, "two")
        assert hosted_results.claim(board, parent, by, child=grand, message=message) is not None
        return {"result": None}
    monkeypatch.setattr(hosted_results, "claim_wait", another_call_takes_it)

    async def call():
        from mcp import Client
        from agent_comms.mcp_server import build_mcp
        async with Client(build_mcp(senv.board, "stdio")) as c:
            return await c.call_tool("board_session_send", {"child": grand, "message": "two", "wait": True})
    out = json.loads(asyncio.run(call()).content[0].text)
    assert out["result"] is None and "already taken by another call" in out["note"]


def test_turns_are_counted_across_the_tree_so_a_deep_chain_cannot_go_on_without_the_human(senv):
    with_spawn(senv, max_live_children=10, max_turns_without_human=4)
    grant(senv)
    sid, kid, grand = chain(senv)               # two starts: two turns
    answer(senv, grand, "x")
    hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, message="again")      # three
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="again")        # four
    for starter, kid_id in ((kid, grand), (sid, kid)):
        with pytest.raises(Conflict, match="were given 4 turns since the human"):
            hs.send(senv.board, senv.p["claude"], bound(senv, starter), kid_id, message="more")
    with pytest.raises(Conflict, match="limit"):
        spawn(senv, bound(senv, kid))
    send(senv, sid, "the human looks in")       # the human messages the root: the count starts over
    hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, message="more")


def test_a_childs_helpers_count_against_its_own_agents_grant(senv):
    """A child of another agent starts helpers under that agent's grant and budget: the frame says so, and without
    that grant it is told it cannot."""
    with_spawn(senv, max_live_children=10)
    grant(senv)
    sid = a_root(senv)
    kid = spawn(senv, bound(senv, sid), agent="codex")["child"]["id"]
    text = hosted._messages(senv.board.conn, kid)[0]["text"]
    assert text.split("\n")[1] == hs.NO_SPAWN_LINE.format(
        why="no spawn grant covers agent codex in this project (only the human can give one)")
    with pytest.raises(Forbidden, match="no live spawn grant covers codex"):
        hs.spawn(senv.board, senv.p["codex"], bound(senv, kid), task="x")
    grant(senv, agents=("codex",))
    hs.spawn(senv.board, senv.p["codex"], bound(senv, kid), task="x", agent="claude")
    assert (len(budget(senv)["tasks"]), len(budget(senv, "codex")["tasks"])) == (1, 1)
    assert hs._usage(senv.board.conn, "codex", PROJECT, senv.clock())["live"] == 1


# ------------------------------------------------------------ the cascade


def test_cascade_closing_the_root_marks_the_whole_subtree_in_one_transaction(senv):
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    sid, kid, grand, great = chain(senv, levels=3)
    hosted.close_session(senv.board, "human", sid)
    assert [state(senv, x)[1] for x in (sid, kid, grand, great)] == ["close", "parent", "parent", "parent"], \
        "every level, at once"
    with pytest.raises(Conflict, match="is being closed"):
        spawn(senv, bound(senv, grand))         # nothing starts under a closing subtree, at any level
    tick(senv, times=3)
    assert all(stored(senv, x)["state"] == "ended" for x in (sid, kid, grand, great))
    assert live_procs(senv) == 0, "no process is left running"
    assert results_of(senv, kid) == [] and results_of(senv, grand) == [], "a cascaded end tells nobody"
    assert hs._usage(senv.board.conn, "claude", PROJECT, senv.clock())["live"] == 0, "no slot is kept"
    hs.flush(senv.board)
    thread = tree_rec(senv, sid)["thread"]
    ends = [p["body"] for p in posts_in(senv, thread) if " ended: " in p["body"]]
    assert len(ends) == 3 and all("closed because the session it was started under ended" in b for b in ends)


def test_cascade_a_parent_that_ends_by_itself_and_one_its_starter_closes_take_their_subtrees(senv):
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    sid, kid, grand, great = chain(senv, levels=3)
    with hosted.db.write_tx(senv.board.conn) as c:      # the node says the child's session ended (its own end)
        r = hosted._session(c, kid)
        hosted._end(c, r, "the session ended on its node", "laptop", senv.clock(), results=True)
        hosted._put(c, hosted.SESSION_PREFIX + kid, r, "laptop", senv.clock())
    assert [state(senv, x)[1] for x in (sid, kid, grand, great)] == [None, None, "parent", "parent"]
    assert stored(senv, kid)["state"] == "ended" and stored(senv, sid)["state"] != "ended"
    assert [x["outcome"] for x in results_of(senv, sid)][-1] == "ended", "the root is told of its own child"
    tick(senv, times=3)
    assert [stored(senv, x)["state"] for x in (grand, great)] == ["ended", "ended"]
    kid2 = spawn(senv, bound(senv, sid))["child"]["id"]
    tick(senv)
    grand2 = spawn(senv, bound(senv, kid2))["child"]["id"]
    tick(senv)
    hs.cancel(senv.board, senv.p["claude"], bound(senv, sid), kid2, close=True)
    assert state(senv, grand2)[1] == "parent"
    tick(senv, times=3)
    assert [stored(senv, x)["state"] for x in (kid2, grand2)] == ["ended", "ended"]
    assert all(proc_of(senv, x)[1].code is not None for x in (kid, grand, great, kid2, grand2)), "none runs on"
    assert proc_of(senv, sid)[1].code is None, "the root is left alone"


def test_cascade_on_a_node_that_is_away_ends_every_level_at_once(senv):
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    ids = chain(senv, levels=3)
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 5)
    hosted.close_session(senv.board, "human", ids[0])
    assert [stored(senv, x)["state"] for x in ids] == ["ended"] * 4
    tick(senv, times=2)                         # the node is back: what the hub no longer lists is closed
    assert live_procs(senv) == 0


def test_a_revoked_grant_ends_nothing_and_each_level_still_sees_and_stops_its_own(senv):
    grant(senv)
    sid, kid, grand = chain(senv)
    revoke(senv, hs.list_grants(senv.board, senv.p["human"])["grants"][0])
    with pytest.raises(Forbidden, match="no live spawn grant"):
        spawn(senv, bound(senv, kid))
    with pytest.raises(Forbidden, match="no live spawn grant"):
        hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, message="x")
    assert [stored(senv, x)["state"] for x in (kid, grand)] != ["ended", "ended"]
    assert [c["id"] for c in hs.status(senv.board, senv.p["claude"], bound(senv, kid))["children"]] == [grand]
    hs.cancel(senv.board, senv.p["claude"], bound(senv, kid), grand, close=True)
    tick(senv, times=2)
    assert stored(senv, grand)["state"] == "ended" and stored(senv, kid)["state"] != "ended"


@pytest.mark.parametrize("order", ["as_stored", "reversed"])
def test_r1_d4_a_rolled_back_node_ends_each_session_of_a_deep_tree_once_whatever_the_order(senv, monkeypatch, order):
    """Review round 1, D4. The hub ends a rolled-back node's children from one reading of its sessions: a child's
    end cascades to its own children, and a stale copy of one of those must not end it again (two end audits, its
    reason overwritten). Ancestors first, each record read again; and `hosted._end` itself ends a session once."""
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    sid, kid, grand, great = chain(senv, levels=3)
    real = hosted._sessions
    if order == "reversed":
        monkeypatch.setattr(hosted, "_sessions", lambda conn: list(reversed(real(conn))))
    hello = {"projects": [PROJECT], "runners": ["claude-code", "codex-cli"], "sessions": {"max_live": 4}}
    assert senv.laptop.post("/api/nodes/hello", json=hello).status_code == 200      # rolled back: no session_host
    monkeypatch.undo()
    assert [stored(senv, x)["state"] for x in (kid, grand, great)] == ["ended"] * 3
    assert stored(senv, kid)["error"] == hosted_tree.ORPHANED[:300]
    assert [stored(senv, x)["error"] for x in (grand, great)] == [
        hosted_tree.PARENT_ENDED + " (its node was not connected)"] * 2, "the first reason, the cascade's, is kept"
    assert [x["outcome"] for x in results_of(senv, sid) if x["child"] == kid].count("ended") == 1
    hs.flush(senv.board)
    ends = [p["body"] for p in posts_in(senv, tree_rec(senv, sid)["thread"]) if " ended: " in p["body"]]
    assert sorted(sum(f"(id {x}," in b for b in ends) for x in (kid, grand, great)) == [1, 1, 1], "one end audit each"


def test_r1_d4_ending_a_session_that_has_ended_already_changes_nothing(senv):
    grant(senv)
    sid, kid, grand = chain(senv)
    stale = dict(stored(senv, grand))
    hosted.close_session(senv.board, "human", kid)
    tick(senv, times=3)
    first = dict(stored(senv, grand))
    assert first["state"] == "ended"
    audits = len(tree_rec(senv, sid).get("audit") or []) + len(posts_in(senv, tree_rec(senv, sid)["thread"]) or [])
    before = len(results_of(senv, kid))
    with hosted.db.write_tx(senv.board.conn) as c:
        hosted._end(c, stale, "a second reason", "hub", senv.clock(), results=True)
        hosted._put(c, hosted.SESSION_PREFIX + grand, stale, "hub", senv.clock())
    assert stored(senv, grand) == first, "one end, the first reason kept"
    assert len(tree_rec(senv, sid).get("audit") or []) + len(posts_in(senv, tree_rec(senv, sid)["thread"])) == audits
    assert len(results_of(senv, kid)) == before, "and no result"


# ------------------------------------------------------------ capacity


def test_capacity_one_slot_runs_a_whole_tree_idle_parents_are_stopped_and_resumed_for_their_results(senv):
    """max_live 1: every slot is held by a parent at some point. An idle parent's process is stopped to make room for
    its child and started again (resumed) for the turn that carries the child's results; a busy parent is never
    stopped, and its child waits until that turn is over. So the tree finishes and never more than one runs."""
    node = make_node(senv, config=tree_config(senv, sessions={"max_live": 1}), log_dir=senv.tmp / "one-logs")
    node.hello()
    grant(senv)
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    kid = spawn(senv, bound(senv, sid), task="plan")["child"]["id"]
    tick(senv, node, times=3)
    assert msg(senv, kid, 1)["state"] == "delivered" and live_procs(senv, node) == 1, "the idle root made room"
    grand = spawn(senv, bound(senv, kid), task="leaf")["child"]["id"]
    tick(senv, node, times=3)
    assert msg(senv, grand, 1)["state"] == "queued", "its parent's turn is in flight: it waits, nothing is stopped"
    seen = 0
    for _ in range(30):
        busy = [x for x in (sid, kid, grand) if any(m["state"] == "delivered" for m in hosted._messages(
            senv.board.conn, x))]
        assert live_procs(senv, node) <= 1
        if busy:
            answer(senv, busy[0], f"done {seen}", node=node)
            seen += 1
        tick(senv, node)
        if not busy and not any(m["state"] not in hosted.SETTLED for x in (sid, kid, grand)
                                for m in hosted._messages(senv.board.conn, x)):
            break
    assert [x["state"] for x in results_of(senv, kid)] == ["delivered"], "the grandchild's result was a turn"
    assert [x["state"] for x in results_of(senv, sid)] == ["delivered", "delivered"], \
        "the child's first reply and its reply on the helper's result, each a turn of the root"
    assert any("--resume" in c["argv"] for c in node.session_spawner.calls if sid in c["argv"]), "resumed"


def _drive(senv, node, sessions, rounds=40, limit=None) -> None:
    """Answer whichever of `sessions` has a turn in flight, one per pass, until nothing of theirs is unsettled."""
    for i in range(rounds):
        if limit is not None:
            assert live_procs(senv, node) <= limit
        busy = [x for x in sessions if any(m["state"] == "delivered" for m in hosted._messages(senv.board.conn, x))]
        if busy:
            answer(senv, busy[0], f"done {i}", node=node)
        tick(senv, node)
        if not busy and not any(m["state"] not in hosted.SETTLED for x in sessions
                                for m in hosted._messages(senv.board.conn, x)):
            return


def test_r1_d3_a_full_childs_parent_is_kept_while_what_is_under_that_child_can_still_bring_it_a_result(senv):
    """Review round 1, D3. max_live 2. A full child finished its first turn; the root, its result handled, was idle,
    and nothing was queued for the child, so the root's process could be stopped for another session (`_lends_full`
    looked at the child alone) while the child's own workspace child still worked. That one's result then came as a
    turn of the full child, which takes turns only under its parent's live process, and nothing would start the
    root again: the turn waited for good. Now the root is kept while a session under its full child is at work,
    spared while one has a result on its way or a message queued, and never stopped for a session under that child."""
    from test_hosted_sessions import P2
    from test_hosted_tree import child, full_node
    node = full_node(senv, "d3", max_live=2)
    r = root(senv, node=node)
    answer(senv, r, node=node)
    host = node.sessions.host
    assert host.sessions[r].ceiling == "full"
    kid = child(senv, r, "k", access="full")["session_id"]
    tick(senv, node, times=3)
    grand = child(senv, kid, "g", access="read-only")["session_id"]
    _drive(senv, node, [r, kid], rounds=12)         # the child's turn, the root's turn with its reply
    for _ in range(6):
        tick(senv, node)
    assert msg(senv, grand, 1)["state"] == "delivered", "the grandchild works (its parent is idle, or was parked)"
    other = root(senv, node=node, project=P2)       # another session wants a slot while the grandchild works
    tick(senv, node, times=3)
    assert host.sessions[r].child is not None, "the full child's parent is kept: what is under that child is at work"
    answer(senv, grand, "the leaf result", node=node)
    _drive(senv, node, [r, kid, grand, other], limit=2)
    kinds = [(m["kind"], m["state"]) for m in hosted._messages(senv.board.conn, kid)]
    assert ("child", "done") in kinds, f"the full child took its child's result as a turn: {kinds}"
    assert msg(senv, other, 1)["state"] == "done", "and the other session had its turn too"


def _full_tree(senv, name, **sessions):
    """A full root with a full child and a read-only grandchild (the human's), each turn settled; the grandchild's
    first turn in flight."""
    from test_hosted_tree import child, full_node
    node = full_node(senv, name, **sessions)
    r = root(senv, node=node)
    answer(senv, r, node=node)
    kid = child(senv, r, "k", access="full")["session_id"]
    tick(senv, node, times=3)
    _drive(senv, node, [r, kid], rounds=12)
    grand = child(senv, kid, "g", access="read-only")["session_id"]
    for _ in range(4):
        tick(senv, node)
    assert msg(senv, grand, 1)["state"] == "delivered"
    return node, r, kid, grand


def _idle_past(senv, node, seconds) -> None:
    for _ in range(3):
        senv.clock.advance(seconds)
        tick(senv, node)


class _Reports:
    """The node's hub client with its reports failing (`fail`: retried later) or refused for good (`refuse`: 404,
    which the node drops), for one session or all."""

    def __init__(self, real):
        self.real, self.fail, self.refuse = real, set(), set()

    def request(self, method, path, **kw):
        sid = path.rsplit("/", 1)[-1]
        if method == "POST" and ("*" in self.fail or sid in self.fail):
            raise httpx.ReadTimeout("the report did not get through")
        if method == "POST" and sid in self.refuse:
            return httpx.Response(404, json={"error": "not_found"}, request=httpx.Request(method, "http://hub" + path))
        return self.real.request(method, path, **kw)


def _deep_full_tree(senv, name, levels):
    """A full root, a full child of it, and `levels` read-only sessions under that child, one under the other (the
    human's), every turn settled. Returns (node, root, [the full child, the one under it, ..., the deepest])."""
    from test_hosted_tree import child, full_node
    with_spawn(senv, max_depth=levels + 1)
    node = full_node(senv, f"{name}{levels}", max_live=6, idle_hours=0.001)
    r = root(senv, node=node)
    answer(senv, r, node=node)
    chain = [child(senv, r, "k", access="full")["session_id"]]
    tick(senv, node, times=3)
    _drive(senv, node, [r] + chain, rounds=12)
    for i in range(levels):
        chain.append(child(senv, chain[-1], f"d{i}", access="read-only")["session_id"])
        tick(senv, node, times=2)
        _drive(senv, node, [r] + chain, rounds=30)
    for _ in range(3):
        tick(senv, node)
    assert all(m["state"] in hosted.SETTLED for x in [r] + chain for m in hosted._messages(senv.board.conn, x))
    return node, r, chain


def _open(senv, kid) -> dict:
    return hosted_tree.subtree_open(senv.board, senv.board.conn, stored(senv, kid))


HOLD_SHAPES = ["turn_in_flight", "queued_message", "pending_result_held_by_a_busy_checkout", "queued_result_turn",
               "end_report_not_yet_taken", "result_turn_written_start_not_confirmed",
               "result_turn_written_start_failed_report_retrying"]
RESULT_SHAPES = ("pending_result_held_by_a_busy_checkout", "queued_result_turn", "end_report_not_yet_taken",
                 "result_turn_written_start_not_confirmed", "result_turn_written_start_failed_report_retrying")


@pytest.mark.parametrize("what", HOLD_SHAPES)
@pytest.mark.parametrize("owner", ["the_full_child", "directly_under_it", "deepest"])
@pytest.mark.parametrize("levels", [2, 3])          # depth 3, and depth 4 (max_depth 4)
def test_r4_n4_the_full_childs_parent_is_held_while_its_subtree_is_open_whatever_its_shape(senv, levels, owner, what):
    """Review round 4, N4: the hold as one rule, pinned by shape. Wherever under a full child something is
    outstanding (the child itself, a session directly under it, the deepest one), and whatever it is (a turn in
    flight, a message queued, a result held by a busy checkout, a result turn queued, an end report the hub has not
    taken), the hub's listing says the subtree is open (`subtree_open`, with the reason that holds it), and the
    full child's parent is held: kept while something there is at work, spared otherwise, the idle timer leaving it."""
    from test_hosted_results import occupy
    node, r, chain = _deep_full_tree(senv, "n4", levels)
    kid, runner, host = chain[0], node.sessions, node.sessions.host
    x = {"the_full_child": chain[0], "directly_under_it": chain[1], "deepest": chain[-1]}[owner]
    below = chain[chain.index(x) + 1] if x != chain[-1] else None
    if what in RESULT_SHAPES and below is None:
        pytest.skip("the deepest session has nobody under it to produce a result or an end for it")
    if what == "turn_in_flight":
        send(senv, x, "work")
        tick(senv, node)
        assert runner._lends_full(r, waiting=False), "something under the full child is at work: kept"
        want = "live" if x != kid else None
    elif what == "queued_message":
        send(senv, x, "one")
        send(senv, x, "two")                        # (one of them queued at the hub)
        want = "queued"
    elif what == "pending_result_held_by_a_busy_checkout":
        send(senv, below, "x")
        tick(senv, node)
        occupy(senv)
        answer(senv, below, "its result", node=node)
        assert [y["state"] for y in results_of(senv, x)][-1] == "pending"
        want = "results"
    elif what == "queued_result_turn":
        send(senv, below, "x")
        tick(senv, node)
        answer(senv, below, "its result", node=node)
        listing(senv)                               # the listing makes it a turn of `x`, queued at the hub
        assert hosted._messages(senv.board.conn, x)[-1]["kind"] == "child"
        want = "queued"
    elif what.startswith("result_turn_written"):
        # A result turn of `x`'s is written to its process; its provider has not said it started (Claude says so
        # with the turn's first line). Until it does, the result can still go back to pending.
        send(senv, below, "x")
        tick(senv, node)
        answer(senv, below, "its result", node=node)
        tick(senv, node, times=2)
        [m] = [m for m in hosted._messages(senv.board.conn, x) if m["kind"] == "child"][-1:]
        assert m["state"] == "delivered" and m.get("started_at") is None
        assert [y["state"] for y in results_of(senv, x)][-1] == "queued", "written, its start not confirmed"
        if what.endswith("report_retrying"):
            reports = _Reports(runner.client)
            runner.client = reports
            reports.fail.add(x)                     # the failure's report does not get through: sent again later
            proc_of(senv, x, node)[1].code = 1      # the process goes before the turn starts
            tick(senv, node, times=2)
            assert not host.sessions[x].busy and stored(senv, x)["in_flight"] is not None, \
                "the turn is over on the node; the hub has not heard"
        want = "results"
    else:                                           # end_report_not_yet_taken
        reports = _Reports(runner.client)
        runner.client = reports
        reports.fail.add(below)
        hosted.close_session(senv.board, "human", below)
        tick(senv, node, times=2)
        gone = node.sessions.host.sessions.get(below)
        assert (gone is None or gone.ended) and stored(senv, below)["state"] != "ended", \
            "ended on the node; its end report not taken by the hub"
        assert any(sid == below and e["kind"] == "ended" for sid, e in runner.pending), "(it waits to be sent again)"
        want = "live"
    reasons = _open(senv, kid)
    if want is not None:
        assert x in reasons[want] or (want == "live" and below in reasons["live"]), (want, reasons)
    assert listing(senv)[kid].get("subtree_open") is True
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is not None, "the full child's parent is held, with slots to spare"


def test_r5_n4_a_result_turn_written_but_not_started_holds_the_parent_until_it_is_settled(senv):
    """Review round 5, the N4 remainder (Sol's case). Nothing under the full child is alive any more (its child has
    ended); the full child's turn with those results is written, its process goes before the turn starts, and the
    report of that failure does not get through (it is sent again later). On the node nothing is at work; at the hub
    the turn is still in flight and its results queued, not settled: they go back to pending when the report comes.
    The subtree stays open on that alone, the idle timer leaves the root, and once the report gets through the
    results are the full child's turn again, under its parent's live process."""
    node, r, kid, grand = _full_tree(senv, "r5w", max_live=3, idle_hours=0.001)
    runner, host = node.sessions, node.sessions.host
    answer(senv, grand, "the leaf result", node=node)
    hosted.close_session(senv.board, "human", grand)
    tick(senv, node, times=2)
    assert stored(senv, grand)["state"] == "ended"
    m = [m for m in hosted._messages(senv.board.conn, kid) if m["kind"] == "child"][-1]
    assert m["state"] == "delivered" and m.get("started_at") is None, "written; its start not confirmed"
    reports = _Reports(runner.client)
    runner.client = reports
    reports.fail.add(kid)
    proc_of(senv, kid, node)[1].code = 1            # the process goes before the turn starts
    tick(senv, node, times=2)
    reasons = _open(senv, kid)
    assert reasons["live"] == [] and reasons["results"] == [kid] and reasons["queued"] == [kid], reasons
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is not None, "held on the hub's record alone"
    reports.fail.clear()
    senv.clock.advance(3)
    tick(senv, node, times=3)
    _drive(senv, node, [r, kid], rounds=10)
    states = [x["state"] for x in results_of(senv, kid) if x["child"] == grand]
    assert states and all(s_ == "delivered" for s_ in states), f"sent once more, and delivered: {states}"


def test_r5_n6_a_hello_between_the_check_and_the_write_refuses_the_start(senv, monkeypatch):
    """Review round 5, N6. A start checks its node's protocol before it takes the write lock; a hello landing in
    between (a node rolled back) used to be missed: the hello's orphan sweep ran before the session existed, and the
    old node's next fetch got its message. The protocol is now asked again inside the creation transaction."""
    from agent_comms import nodes as nodes_mod
    from test_hosted_tree import child
    node, r, kid, grand = _full_tree(senv, "r5h", max_live=3)
    info = nodes_mod._get(senv.board.conn, nodes_mod.NODE_PREFIX + "laptop")
    rolled = {"projects": info["projects"], "runners": ["claude-code", "codex-cli"], "sessions": info["sessions"],
              "session_host": {"tree": 2, "runners": info["session_host"]["runners"]}}
    real_target = hosted_tree._target
    hello = {"body": None}

    def target_then_hello(*a, **kw):                # the check before the write passes; then the hello lands
        real_target(*a, **kw)
        if hello["body"] is not None:
            assert senv.laptop.post("/api/nodes/hello", json=hello["body"]).status_code == 200
            hello["body"] = None
    monkeypatch.setattr(hosted_tree, "_target", target_then_hello)
    before = {x["session_id"] for x in hosted._sessions(senv.board.conn)}
    hello["body"] = rolled
    out = child(senv, kid, "under the full child", access="read-only", expect=409)
    assert "does not keep a full-access session's parent up" in out["message"]
    assert {x["session_id"] for x in hosted._sessions(senv.board.conn)} == before, "nothing made"
    assert stored(senv, grand)["state"] == "ended", "(the hello's sweep ended the one already there)"
    node.hello()                                    # protocol 3 again
    plain = {"projects": info["projects"], "runners": ["claude-code", "codex-cli"], "sessions": info["sessions"]}
    hello["body"] = plain
    out = child(senv, r, "under the root", access="read-only", expect=409)
    assert "no longer speaks the child-session protocol" in out["message"], "the same for a node that hosts none"
    assert {x["session_id"] for x in hosted._sessions(senv.board.conn)} == before, "nothing made, nothing enqueued"


def test_r4_n4_a_closed_subtree_holds_nothing_and_the_idle_timer_runs_again(senv):
    """The other side, and the cost accepted: while any session under the full child lives, its parent is not idle-
    parked (even if all of them are idle); once the subtree is closed and nothing of it waits, the flag goes, the
    listing's version changes, and the idle timer stops the parent as any other."""
    node, r, chain = _deep_full_tree(senv, "n4c", 2)
    kid, host = chain[0], node.sessions.host
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is not None and _open(senv, kid)["live"] == chain[1:], "idle, but alive: held"
    before = senv.laptop.get("/api/nodes/sessions", params={"tree": 3}).json()["version"]
    hosted.close_session(senv.board, "human", chain[1])
    tick(senv, node, times=3)
    _drive(senv, node, [r, kid], rounds=10)         # the full child takes the ends as its turn, the root its reply
    for _ in range(3):
        tick(senv, node)
    assert _open(senv, kid) == {"live": [], "results": [], "queued": []}
    assert "subtree_open" not in listing(senv)[kid]
    assert senv.laptop.get("/api/nodes/sessions", params={"tree": 3}).json()["version"] != before
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is None, "nothing holds it: the idle timer stopped it"


def test_r2_d3_a_result_held_by_a_dispatched_run_keeps_the_full_childs_parent_until_it_is_delivered(senv):
    """Review round 2, D3 (Sol's sequence), under the round-4 rule. The grandchild's result is held at the hub (a
    dispatched run uses the checkout); the human gives the full child a turn meanwhile; the idle timer comes round.
    The root is held all along (the subtree is open), and once the run is over the result is the full child's
    turn, with no other message. While it waits, `show` says why."""
    from test_hosted_results import occupy
    node, r, kid, grand = _full_tree(senv, "r2d3", max_live=3, idle_hours=0.001)
    host = node.sessions.host
    occupy(senv)
    answer(senv, grand, "the leaf result", node=node)
    tick(senv, node, times=2)
    [x] = [x for x in results_of(senv, kid) if x["child"] == grand]
    assert x["state"] == "pending" and listing(senv)[kid].get("subtree_open") is True
    [shown] = [v for v in senv.human.get(f"/api/hosted-sessions/{kid}").json()["results"] if v["child"] == grand]
    assert "a dispatched run is using the parent's checkout" in shown["waiting_because"]
    send(senv, kid, "the human, meanwhile")          # an unrelated turn of the full child
    tick(senv, node, times=2)
    answer(senv, kid, "hello", node=node)
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is not None, "the root is held while the subtree is open"
    senv.board.conn.execute("DELETE FROM board_state WHERE key = 'dispatch.run.busy-run'")
    senv.board.conn.commit()
    tick(senv, node, times=3)
    _drive(senv, node, [r, kid], rounds=10)
    assert ("child", "done") in [(m["kind"], m["state"]) for m in hosted._messages(senv.board.conn, kid)], \
        "delivered, and the full child took it under its parent, with no other message"


def test_r2_n1_a_result_claimed_in_turn_holds_nothing_once_the_subtree_is_closed(senv):
    """Review round 2, N1, under the round-4 rule. The full child takes its child's result in its own turn: claimed,
    nothing of it waits. Its parent is held only while the subtree has something open (the child it waited for is
    still alive); once that child ends and its end is taken, the idle timer runs again: nothing resets it for good."""
    node, r, kid, grand = _full_tree(senv, "r2n1", max_live=3, idle_hours=0.001)
    host = node.sessions.host
    send(senv, kid, "wait for your helper")
    tick(senv, node, times=2)
    answer(senv, grand, "the leaf result", node=node)
    assert hosted_results.claim(senv.board, kid, "claude", child=grand) is not None
    answer(senv, kid, "took it", node=node)
    _drive(senv, node, [r, kid], rounds=10)
    assert _open(senv, kid) == {"live": [grand], "results": [], "queued": []}, "only the helper's life holds it"
    hosted.close_session(senv.board, "human", grand)
    tick(senv, node, times=3)
    _drive(senv, node, [r, kid], rounds=10)
    for _ in range(3):
        tick(senv, node)
    assert not any(_open(senv, kid).values())
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is None, "nothing holds it: the idle timer stopped it"


def test_r2_d3_an_abandoned_result_releases_the_hold(senv):
    """A result the hub keeps undelivered (it will never be a turn) releases the hold as delivery or a claim does:
    here the last thing open in the subtree (the helper has ended; its results are held by a busy checkout)."""
    from test_hosted_results import occupy
    node, r, kid, grand = _full_tree(senv, "r2ab", max_live=3, idle_hours=0.001)
    host = node.sessions.host
    occupy(senv)
    answer(senv, grand, "the leaf result", node=node)
    hosted.close_session(senv.board, "human", grand)
    tick(senv, node, times=3)
    assert stored(senv, grand)["state"] == "ended" and _open(senv, kid)["live"] == []
    assert _open(senv, kid)["results"] == [kid], "only the held results keep it open"
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is not None
    before = senv.laptop.get("/api/nodes/sessions", params={"tree": 3}).json()["version"]
    with hosted.db.write_tx(senv.board.conn) as c:
        for x in hosted_results._results(c, kid):
            if x["state"] == "pending":
                hosted_results._put(c, hosted_results._key(kid, x["n"]), x | hosted_results._undelivered(
                    "kept for the human", senv.clock()), "test", senv.clock())
    assert "subtree_open" not in listing(senv)[kid]
    assert senv.laptop.get("/api/nodes/sessions", params={"tree": 3}).json()["version"] != before, \
        "news for the node's long-poll"
    _idle_past(senv, node, 10)
    assert host.sessions[r].child is None, "nothing waits any more: the idle timer stopped the root"


def test_r1_d3_a_session_under_a_full_child_never_has_that_childs_parent_stopped_for_its_slot(senv):
    """max_live 2: the root idle, another session busy, the full child stopped (it had nothing to do). Its own child
    asks for a slot: the only process that could make room is the root, whose live process the full child's next turn
    (this one's result) needs. It is not stopped for that: the grandchild waits until the other session's turn is
    over, and then its result is the full child's turn under its parent's live process."""
    from test_hosted_sessions import P2
    from test_hosted_tree import child, full_node
    node = full_node(senv, "d3b", max_live=2)
    r = root(senv, node=node)
    answer(senv, r, node=node)
    host = node.sessions.host
    kid = child(senv, r, "k", access="full")["session_id"]
    tick(senv, node, times=3)
    _drive(senv, node, [r, kid], rounds=12)         # the full child's turn, the root's turn with its reply
    senv.clock.advance(5)
    send(senv, r, "the human again")                # the root was busy last: the full child is the longest idle
    tick(senv, node)
    answer(senv, r, node=node)
    other = root(senv, node=node, project=P2)
    for _ in range(5):
        tick(senv, node)
    assert msg(senv, other, 1)["state"] == "delivered" and host.sessions[kid].child is None, \
        "the other session runs; the full child, idle, made room"
    grand = child(senv, kid, "g", access="read-only")["session_id"]
    for _ in range(5):
        tick(senv, node)
    assert msg(senv, grand, 1)["state"] == "queued" and host.sessions[r].child is not None, \
        "the grandchild waits: the full child's parent is not stopped for it"
    _drive(senv, node, [r, kid, grand, other], limit=2)
    kinds = [(m["kind"], m["state"]) for m in hosted._messages(senv.board.conn, kid)]
    assert ("child", "done") in kinds, f"the full child took its child's result as a turn: {kinds}"


@pytest.mark.parametrize("max_live", [2, 3])
def test_r1_d3_capacity_with_a_full_access_middle_level_the_tree_finishes(senv, max_live):
    """A full child (it needs its parent's live process, so max_live is at least 2: with 1 the hub refuses it,
    test_hosted_tree) that starts a helper of its own: every turn of the three finishes, the full child's always
    under a parent whose live process confirmed full access, and never more than max_live processes up."""
    from test_hosted_tree import full_node
    node = full_node(senv, f"cap{max_live}", max_live=max_live)
    grant(senv, access="full")
    r = root(senv, node=node)
    answer(senv, r, node=node)
    host, runner = node.sessions.host, node.sessions
    under, real_spawn = [], host._spawn

    def spawn_(s, now, out):
        parent = runner.parent_of.get(s.session_id)
        if parent is not None and s.access == "full":
            under.append(host.sessions[parent].ceiling)
        return real_spawn(s, now, out)
    host._spawn = spawn_
    kid = spawn(senv, bound(senv, r), task="plan", access="full")["child"]["id"]
    tick(senv, node, times=3)
    grand = spawn(senv, bound(senv, kid), task="leaf")["child"]["id"]
    assert (stored(senv, kid)["access"], stored(senv, grand)["access"]) == ("full", "read-only")
    _drive(senv, node, [r, kid, grand], rounds=60, limit=max_live)
    assert [x["state"] for x in results_of(senv, kid)] == ["delivered"], "the helper's result was the child's turn"
    assert [x["state"] for x in results_of(senv, r)] == ["delivered", "delivered"]
    assert under and all(c == "full" for c in under), "the full child only ever ran under a confirmed parent"


# ------------------------------------------------------------ placement and isolation


def test_placement_a_grandchild_of_an_interactive_root_runs_on_its_parents_node(senv):
    senv.board.s.dispatch = json.loads(json.dumps(senv.board.s.dispatch))
    senv.board.s.dispatch["nodes"]["desktop"] = dict(senv.board.s.dispatch["nodes"]["laptop"])
    grant(senv)
    kid = spawn(senv, interactive(senv))["child"]["id"]
    assert stored(senv, kid)["node"] == "laptop"
    tick(senv)
    desktop = make_node(senv, name="desktop", config=tree_config(senv), log_dir=senv.tmp / "desktop-logs")
    senv.clock.advance(1)
    desktop.hello()                             # now the most recently seen
    assert spawn(senv, interactive(senv))["child"]["id"] and \
        hosted_tree.place_interactive(senv.board, PROJECT, "claude", "claude-code", hs._nodes(senv.board)) == "desktop"
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    assert (stored(senv, grand)["node"], stored(senv, grand)["depth"]) == ("laptop", 2), "its parent's node"
    assert hosted_tree.shared_checkout(senv.board.conn, stored(senv, grand)) is None, "an interactive tree's"


def test_r2_d2_a_result_for_a_parent_in_its_own_worktree_is_not_held_by_a_run_in_the_project_checkout(wtenv):
    """Review round 2, D2. A child with a worktree of its own takes its turns there: a dispatched run in the
    project's checkout holds neither its tasks nor, now, its children's results (the hub's path check made the
    exception the task path already had)."""
    from test_hosted_results import occupy
    grant(wtenv)
    sid = a_root(wtenv)
    kid = spawn(wtenv, bound(wtenv, sid), isolation="worktree")["child"]["id"]
    tick(wtenv)
    answer(wtenv, kid, "planned")
    _drive(wtenv, wtenv.node, [sid], rounds=6)
    grand = spawn(wtenv, bound(wtenv, kid), isolation="worktree")["child"]["id"]
    tick(wtenv)
    occupy(wtenv)                                 # a run in the project's checkout, not in either worktree
    answer(wtenv, grand, "leaf")
    tick(wtenv, times=2)
    assert hosted._messages(wtenv.board.conn, kid)[-1]["kind"] == "child", "delivered beside the run"
    assert "leaf" in kid_turns(wtenv, kid)[-1]


def test_isolation_a_grandchild_may_have_a_worktree_and_shared_is_the_projects_checkout_at_every_level(wtenv):
    grant(wtenv)
    sid = a_root(wtenv)
    kid = spawn(wtenv, bound(wtenv, sid), isolation="worktree")["child"]["id"]
    tick(wtenv)
    own = spawn(wtenv, bound(wtenv, kid), isolation="worktree")["child"]["id"]
    shared = spawn(wtenv, bound(wtenv, kid))["child"]["id"]
    tick(wtenv)
    from test_hosted_tree import call_for
    kid_dir, own_dir = call_for(wtenv, kid)["cwd"], call_for(wtenv, own)["cwd"]
    assert own_dir.endswith("session-" + own) and own_dir != kid_dir, "a worktree of its own"
    assert call_for(wtenv, shared)["cwd"] == str(wtenv.work), "shared: the project's checkout, not its parent's tree"
    assert hosted_tree.shared_checkout(wtenv.board.conn, stored(wtenv, shared)) == stored(wtenv, sid)["checkout"]


def test_one_run_per_checkout_holds_a_grandchilds_task_and_a_result_turn_of_its_parent(senv):
    from test_hosted_results import occupy
    grant(senv)
    sid, kid = chain(senv, levels=1)
    answer(senv, kid, "first")
    occupy(senv)
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    tick(senv, times=2)
    assert listing(senv)[grand]["messages"][0]["held"] == "checkout" and kid_turns(senv, grand) == []
    senv.board.conn.execute("DELETE FROM board_state WHERE key = 'dispatch.run.busy-run'")
    tick(senv, times=2)
    answer(senv, grand, "leaf")
    occupy(senv)
    tick(senv, times=2)
    assert all(m["kind"] != "child" for m in hosted._messages(senv.board.conn, kid)), "the result waits"
    senv.board.conn.execute("DELETE FROM board_state WHERE key = 'dispatch.run.busy-run'")
    tick(senv, times=2)
    assert hosted._messages(senv.board.conn, kid)[-1]["kind"] == "child"


@pytest.mark.parametrize("depth", [1, 2])
def test_r1_d2_a_result_turn_is_held_by_a_run_on_the_parents_node_and_reserved_once_shown(senv, depth):
    """Review round 1, D2 (already so on main, for a child's result to a hosted root: the same path). The result check
    used only the hub's path for the project, which may be another directory than the node's; and a result turn,
    once listed, did not keep the checkout from the dispatcher until its node reported it. Now it is held as an
    agent's task is (the node-and-checkout identity, and the dispatcher's claims) and reserved in the listing's own
    transaction. Both orders: a run there first holds the result; a result listed first holds the checkout."""
    from agent_comms import hosted_wake
    senv.board.s.dispatch = json.loads(json.dumps(senv.board.s.dispatch))
    senv.board.s.dispatch["worktrees"] = {PROJECT: "/elsewhere/on/the/hub"}
    grant(senv)
    ids = chain(senv, levels=depth)
    parent, kid = ids[-2], ids[-1]
    if depth == 2:
        answer(senv, parent, "planned")
        tick(senv)
    n = len(hosted._messages(senv.board.conn, parent))
    run = {"run_id": "node-run", "agent": "codex", "status": "running", "started_at": senv.clock(), "pid": None,
           "node": "laptop", "cwd": PROJECT}
    with hosted.db.write_tx(senv.board.conn) as c:          # a run first, on the parent's node, in its checkout
        hosted.nodes._put(c, "dispatch.run.node-run", run, "test", senv.clock())
    answer(senv, kid, "the result")
    tick(senv, times=2)
    assert not hosted_wake._occupied(senv.board, stored(senv, parent)), "(the hub's own path says free)"
    assert len(hosted._messages(senv.board.conn, parent)) == n, "held: no result turn beside the run"
    senv.board.conn.execute("DELETE FROM board_state WHERE key = 'dispatch.run.node-run'")
    senv.board.conn.commit()
    listing(senv)                             # the node's fetch: the result becomes the parent's turn, reserved
    [m] = hosted._messages(senv.board.conn, parent)[n:]
    assert (m["kind"], m["state"]) == ("child", "queued") and m.get("shown_at") is not None
    assert parent in [r["session_id"] for r in hosted_wake.turn_sessions(senv.board, senv.clock())], \
        "the dispatcher counts the checkout busy from the listing on, before the node reports anything"
    tick(senv)
    answer(senv, parent, "took them")
    assert parent not in [r["session_id"] for r in hosted_wake.turn_sessions(senv.board, senv.clock())], \
        "until its node reports the turn"
    from agent_comms import dispatch
    hs.send(senv.board, senv.p["claude"], bound(senv, parent), kid, message="once more")
    tick(senv)
    key = dispatch._claim_key(PROJECT)          # a launch the dispatcher claimed, its run not on record yet
    with hosted.db.write_tx(senv.board.conn) as c:
        hosted.nodes._put(c, key, {"at": senv.clock(), "cwd": PROJECT, "node": "laptop", "project": PROJECT}, "test",
                          senv.clock())
    n = len(hosted._messages(senv.board.conn, parent))
    answer(senv, kid, "third")
    assert [x["state"] for x in results_of(senv, parent)][-1] == "pending"
    tick(senv, times=2)
    assert len(hosted._messages(senv.board.conn, parent)) == n, "a claimed launch holds the result too"
    senv.board.conn.execute("DELETE FROM board_state WHERE key = ?", (key,))
    senv.board.conn.commit()
    tick(senv)
    assert [m["kind"] for m in hosted._messages(senv.board.conn, parent)][n:] == ["child"]


# ------------------------------------------------------------ the frame and the audit


def test_the_frame_tells_each_level_what_remains_below_it(senv):
    with_spawn(senv, max_depth=3, max_live_children=10)
    grant(senv)
    sid, kid, grand, great = chain(senv, levels=3)
    lines = {x: kid_turns(senv, x)[0].split("\n")[1] for x in (kid, grand, great)}
    assert "you are at depth 1 of at most 3, so 2 more level(s) can be started below you" in lines[kid]
    assert "you are at depth 2 of at most 3, so the sessions you start cannot start any of their own" in lines[grand]
    assert ("agents claude, codex on your node, your own among them (any model they offer; prefer the cheapest that "
            "can do the work)") in lines[grand]
    assert lines[great] == hs.NO_SPAWN_LINE.format(why=hs.LAST_LEVEL.format(depth=3, max_depth=3))
    assert "at most 9 more session(s) live at once" in lines[kid], "the budget as it stood at each start"
    assert "at most 8 more session(s) live at once" in lines[grand]


def test_r1_d5_the_frame_is_true_when_written_the_budget_after_this_start_and_targets_on_the_childs_node(senv):
    """Review round 1, D5. The frame said "You may start helper sessions" when the start that made the child had
    spent the budget's last unit, and listed targets its node does not offer. Now it counts the budget after this
    start's charge, lists only what the child could start on its own node (within what it could give), and says
    plainly when nothing is possible and why."""
    with_spawn(senv, max_depth=3, max_live_children=10, max_turns_without_human=2,
               targets=["claude/haiku", "grok", "claude"])
    grant(senv)
    sid, kid, grand = chain(senv)
    lines = {x: kid_turns(senv, x)[0].split("\n")[1] for x in (kid, grand)}
    assert lines[kid].startswith("You may start helper sessions") and "1 more child turn(s)" in lines[kid]
    assert "targets claude/haiku, claude (cheapest first" in lines[kid], "grok: no runner on its node, left out"
    assert lines[grand] == hs.NO_SPAWN_LINE.format(why=(
        "the budget for agent claude in this project has no room left now (child turns before the human looks in); "
        "board_session_capabilities says when it has")), "this start took the last turn: it says so"
    with pytest.raises(Conflict, match="limit"):
        spawn(senv, bound(senv, grand))          # (and the tools agree)
    with_spawn(senv, max_turns_without_human=40, targets=["grok", "claude/haiku"])
    cheap = spawn(senv, bound(senv, sid), model="haiku")["child"]["id"]
    tick(senv)
    assert "targets claude/haiku (cheapest first" in kid_turns(senv, cheap)[0].split("\n")[1], \
        "grok is cheaper and listed, but its node has no runner for it: not offered"


def test_audit_one_post_per_spawn_and_end_at_every_level_naming_the_direct_parent_and_the_root(senv):
    grant(senv)
    sid, kid, grand = chain(senv)
    hs.cancel(senv.board, senv.p["claude"], bound(senv, kid), grand, close=True)
    tick(senv, times=2)
    hs.flush(senv.board)
    bodies = [p["body"] for p in posts_in(senv, tree_rec(senv, sid)["thread"])]
    assert len(bodies) == 3, "two starts and one end, in the one thread of the tree"
    assert f"session {sid} (claude) started child session general-1 (id {kid}" in bodies[0]
    assert f"depth 1 under parent {sid} in tree {sid}" in bodies[0]
    assert f"session {kid} (claude, child general-1) started child session general-1 (id {grand}" in bodies[1]
    assert f"depth 2 under parent {kid} in tree {sid}" in bodies[1]
    assert f"(id {grand}, agent claude, depth 2 under parent {kid} in tree {sid}) of session {kid}" in bodies[2]
    assert "ended: closed by the session that started it" in bodies[2]


# ------------------------------------------------------------ self-stop and the start gate


def test_self_stop_and_the_start_gate_hold_for_a_grandchild_as_for_any_session(senv):
    """The node's deadline and its start gate (review rounds 10 to 12) look at no depth: a grandchild's turn in
    flight is stopped at the self-stop deadline, and nothing of it starts again until contact is back and a listing
    fetched since has been applied."""
    from test_hosted_spawn import _cut_off, _past_the_self_stop
    grant(senv)
    sid, kid, grand = chain(senv)               # the grandchild's first turn is in flight
    proc = proc_of(senv, grand)[1]
    hub = _cut_off(senv)
    hub.silent = True
    _past_the_self_stop(senv)
    tick(senv)
    senv.node.sessions.enforce_deadlines()
    assert proc.terminated, "stopped at the deadline, like any turn in flight"
    hs.send(senv.board, senv.p["claude"], bound(senv, kid), grand, message="next")      # queued at the hub meanwhile
    calls = len(senv.node.session_spawner.calls)
    for _ in range(3):
        tick(senv)
        senv.clock.advance(3)
    assert len(senv.node.session_spawner.calls) == calls, "nothing starts while the hub is silent"
    hub.silent = False
    for _ in range(4):
        senv.clock.advance(3)
        tick(senv)
    assert any("next" in t for t in kid_turns(senv, grand)), "contact is back: its next task goes"


def test_a_runner_that_names_its_own_directory_is_held_against_the_roots_checkout_at_every_level(senv, tmp_path):
    """A shared grandchild runs in the project's checkout on the node, the one its root reported: a runner that names
    its own directory is held against that (a child reports no checkout of its own), and an interactive root's tree,
    whose checkout is not known, gets no such grandchild."""
    from test_hosted_sessions import P2, start
    from test_hosted_tree import child
    here, other = senv.work, tmp_path / "elsewhere"
    other.mkdir()
    pinned = ["codex", "exec", "--cd", str(here), "--sandbox", "workspace-write", "{prompt}"]
    node = make_node(senv, config=tree_config(senv, runners={"codex-cli": pinned},
                                              worktrees={PROJECT: str(here), P2: str(other)}),
                     log_dir=senv.tmp / "b-logs")
    node.hello()
    sid = start(senv)["session_id"]
    tick(senv, node)
    kid = child(senv, sid, "plan")["session_id"]
    tick(senv, node)
    assert "checkout" not in stored(senv, kid), "(only a root's node says where it runs)"
    grand = child(senv, kid, "count", agent="codex")
    assert (grand["depth"], grand["agent"]) == (2, "codex"), "held against the root's checkout, which is the directory"
    assert "a worktree of its own" in child(senv, kid, agent="codex", isolation="worktree", expect=409)["message"]
    elsewhere = root(senv, node=node, project=P2)
    under = child(senv, elsewhere, "plan")["session_id"]
    tick(senv, node)
    assert f"session {under} runs in {other}" in child(senv, under, agent="codex", expect=409)["message"]
    grant(senv)
    ikid = spawn(senv, interactive(senv))["child"]["id"]
    tick(senv, node)
    out = child(senv, ikid, agent="codex", expect=409)["message"]
    assert "cannot be started in an interactive session's tree" in out


def test_r2_d5_the_frame_and_capabilities_list_only_what_the_start_would_place(senv, tmp_path):
    """Review round 2, D5. A runner that names its own directory could be listed (even as the only target) under a
    session in another checkout, where the start refuses it. The frame and `capabilities` now pass every target
    through the start's own placement checks (hosted_tree.placement_problem: `_target` and `_fits`)."""
    from test_hosted_sessions import P2, start
    here, other = senv.work, tmp_path / "elsewhere"
    other.mkdir()
    pinned = ["codex", "exec", "--cd", str(here), "--sandbox", "workspace-write", "{prompt}"]
    node = make_node(senv, config=tree_config(senv, runners={"codex-cli": pinned},
                                              worktrees={PROJECT: str(here), P2: str(other)}),
                     log_dir=senv.tmp / "d5-logs")
    node.hello()
    grant(senv, project=P2)
    grant(senv, project=PROJECT)
    for project, codex_listed in ((P2, False), (PROJECT, True)):
        sid = start(senv, project=project)["session_id"]
        tick(senv, node)
        answer(senv, sid, node=node)
        caps = hs.capabilities(senv.board, senv.p["claude"], bound(senv, sid))
        assert ("codex" in [t["agent"] for t in caps["targets"]]) is codex_listed, (project, caps["targets"])
        kid = spawn(senv, bound(senv, sid))["child"]["id"]
        tick(senv, node)
        from test_hosted_sessions import written
        spawned = zip(node.session_spawner.calls, node.session_spawner.children)
        [first] = [x["message"]["content"] for call, proc in spawned
                   if kid in call["argv"] for x in written(proc) if x.get("type") == "user"][:1]
        line = first.split("\n")[1]
        assert ("codex" in line) is codex_listed, (project, line)
        if not codex_listed:
            assert "agents claude on your node" in line
            with pytest.raises(Conflict, match=f"runs in {other}"):
                spawn(senv, bound(senv, kid), agent="codex")        # what the frame left out, the start refuses


def test_r3_n3_nothing_is_started_under_a_full_child_on_a_node_that_does_not_keep_its_parent(senv):
    """Review round 3, N3. Hub first, then nodes: a node of the previous version says `tree: 2` and takes
    grandchildren, but does not read `results_waiting`, so it could stop a full child's parent while that child's
    own child works. Only a node whose hello says `tree: 3` (it keeps the parent) gets a session under a full-access
    child; for any other the start is refused with the reason, and the frame and capabilities say it cannot, through
    the same placement check."""
    from agent_comms import nodes as nodes_mod
    from test_hosted_tree import full_node

    def node_version(tree: int) -> None:
        with hosted.db.write_tx(senv.board.conn) as c:
            info = nodes_mod._get(c, nodes_mod.NODE_PREFIX + "laptop")
            info["session_host"]["tree"] = tree
            nodes_mod._put(c, nodes_mod.NODE_PREFIX + "laptop", info, "laptop", senv.clock())

    node = full_node(senv, "n3", max_live=3)
    assert nodes_mod._get(senv.board.conn, nodes_mod.NODE_PREFIX + "laptop")["session_host"]["tree"] == 4
    grant(senv, access="full")
    r = root(senv, node=node)
    answer(senv, r, node=node)
    node_version(2)                                 # the node as the previous version's hello says it
    kid = spawn(senv, bound(senv, r), access="full")["child"]["id"]
    tick(senv, node, times=2)
    reason = "does not keep a full-access session's parent up while that session's children work"
    with pytest.raises(Conflict, match=reason):
        spawn(senv, bound(senv, kid))
    caps = hs.capabilities(senv.board, senv.p["claude"], bound(senv, kid))
    assert not caps["may_spawn"] and caps["targets"] == [] and any(reason in w for w in caps["why_not"])
    from test_hosted_sessions import written
    spawned = zip(node.session_spawner.calls, node.session_spawner.children)
    first = [x["message"]["content"] for call, proc in spawned if kid in call["argv"]
             for x in written(proc) if x.get("type") == "user"][0]
    line = first.split("\n")[1]
    assert line.startswith("You cannot start helper sessions of your own: node laptop runs a version") and \
        reason in line, line
    from test_hosted_tree import child
    assert reason in child(senv, kid, "g", access="read-only", expect=409)["message"], "the human's route too"
    node_version(3)                                 # the node updated
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    assert stored(senv, grand)["depth"] == 2, "placed"


def test_r3_n2_a_result_in_a_held_turn_says_why_in_show_the_cli_text_and_status(senv):
    """Review round 3, N2. A result already in a turn of a full child that its node holds (the child's parent has no
    live process that confirmed full access: after a node restart, say) says why on every surface: `show`, the
    CLI's text of it, and the starter's `board_session_status`."""
    from agent_comms import cli
    from test_hosted_tree import full_node
    node = full_node(senv, "n2", max_live=3)
    grant(senv, access="full")
    r = root(senv, node=node)
    answer(senv, r, node=node)
    kid = spawn(senv, bound(senv, r), access="full")["child"]["id"]
    tick(senv, node, times=2)
    answer(senv, kid, "planned", node=node)
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    tick(senv, node, times=2)
    assert msg(senv, grand, 1)["state"] == "delivered"
    again = full_node(senv, "n2", max_live=3)       # the node restarts: the grandchild's turn is lost (a result)
    for _ in range(4):
        tick(senv, again)
    [x] = [x for x in results_of(senv, kid) if x["child"] == grand]
    assert x["state"] == "queued", "in the full child's next turn, which its node holds"
    view = senv.human.get(f"/api/hosted-sessions/{kid}").json()
    [shown] = [v for v in view["results"] if v["child"] == grand]
    assert "confirmed full access" in shown["waiting_because"]
    assert f"waits: {shown['waiting_because']}" in cli._fmt_hosted_detail(view)
    [c] = hs.status(senv.board, senv.p["claude"], bound(senv, kid))["children"]
    assert "confirmed full access" in c["results_waiting_because"][str(x["n"])] and c["results_waiting"] == [], \
        "queued results get their reason too (results_waiting lists the pending ones, as before)"


def test_r4_n5_a_node_rolled_back_below_the_hold_ends_the_sessions_under_a_full_child(senv):
    """Review round 4, N5. A node that comes back with a lower session protocol than its sessions need: on main, the
    hub ends the children such a node can no longer run, at its hello (hosted_tree.end_orphans) or on a read
    (hosted._settle). The same path now covers a session under a full-access child on a node below protocol 3:
    ended at that hello, with the reason (show says it), its parent told as for any end; the full child and the root
    go on; nothing of it is listed to the node any more."""
    from agent_comms import nodes as nodes_mod
    from test_hosted_tree import child
    node, r, kid, grand = _full_tree(senv, "n5", max_live=3)
    info = nodes_mod._get(senv.board.conn, nodes_mod.NODE_PREFIX + "laptop")
    hello = {"projects": info["projects"], "runners": ["claude-code", "codex-cli"], "sessions": info["sessions"],
             "session_host": {"tree": 2, "runners": info["session_host"]["runners"]}}
    assert senv.laptop.post("/api/nodes/hello", json=hello).status_code == 200      # rolled back to protocol 2
    assert stored(senv, grand)["state"] == "ended", "at that hello, before anybody reads it (no read settles it)"
    g = senv.human.get(f"/api/hosted-sessions/{grand}").json()
    assert g["state"] == "ended" and g["error"] == hosted_tree.ORPHANED_HOLD[:300]
    assert msg(senv, grand, 1)["state"] == "failed", "its turn in flight, lost"
    assert stored(senv, r)["state"] != "ended" and stored(senv, kid)["state"] != "ended"
    assert [y["outcome"] for y in results_of(senv, kid) if y["child"] == grand][-1] == "ended"
    listed = senv.laptop.get("/api/nodes/sessions", params={"tree": 2}).json()["sessions"]
    assert grand not in [x["session_id"] for x in listed] and kid in [x["session_id"] for x in listed]
    assert "does not keep a full-access session's parent" in child(senv, kid, "x", access="read-only",
                                                                   expect=409)["message"]
    node.hello()                                                                   # updated again (protocol 3)
    assert child(senv, kid, "again", access="read-only")["depth"] == 2
