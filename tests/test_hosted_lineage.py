"""The dashboard's lineage view of session trees (hosted_lineage.py, `GET /api/hosted-trees`; DESIGN_NOTES "Session
lineage view"): one entry per tree, children nested under their parent at every depth, read-only, human only. Real
hub routes and the node's SessionRunner with fake processes, as in test_hosted_spawn; never a real agent CLI."""

import pytest

from agent_comms import gateway, hosted_lineage, hosted_spawn as hs

from conftest import PROJECT
from test_hosted_results import occupy
from test_hosted_sessions import send, tick
from test_hosted_spawn import a_root, bound, grant, interactive, spawn, with_spawn
from test_hosted_tree import _reports_follow_the_command_line, answer, child, senv  # noqa: F401

HOSTILE = ('<script>alert(1)</script><img src=x onerror="alert(2)"> **bold** [click me](javascript:alert(3)) '
           '<a href="https://evil.example/">evil</a> & &amp; </pre></div>')


@pytest.fixture(autouse=True)
def _no_codex_home_of_the_users(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def trees(env, all=False) -> dict:
    r = env.human.get("/api/hosted-trees", params={"all": "true"} if all else None)
    assert r.status_code == 200, r.text
    return r.json()


def only(env, all=False) -> dict:
    [t] = trees(env, all)["trees"]
    return t


def walk(node) -> list[dict]:
    return [node] + [x for k in node["children"] for x in walk(k)]


def by_id(tree) -> dict[str, dict]:
    return {n["session_id"]: n for n in walk(tree["session"])}


def test_a_hosted_root_and_its_children_nested_at_depth_1_2_and_3(senv):
    with_spawn(senv, max_depth=3)
    grant(senv)
    sid = a_root(senv)
    kid = spawn(senv, bound(senv, sid), title="look around", model="haiku")["child"]["id"]
    tick(senv)
    grand = spawn(senv, bound(senv, kid), access="read-only")["child"]["id"]
    tick(senv)
    great = spawn(senv, bound(senv, grand))["child"]["id"]
    tick(senv)
    mine = child(senv, sid, "the human's own child", role="review", title="human review")["session_id"]
    hs.flush(senv.board)
    t = only(senv)
    assert (t["root"], t["kind"], t["agent"], t["project"], t["node"]) == (sid, "hosted", "claude", PROJECT, "laptop")
    assert (t["sessions"], t["live"]) == (5, 5)
    assert type(t["audit_thread"]) is int, "agent-started children are audited in one thread per tree"
    assert senv.board.conn.execute("SELECT title FROM threads WHERE id = ?", (t["audit_thread"],)).fetchone()[0] \
        .startswith("Session tree of claude's hosted session")
    root = t["session"]
    assert root["session_id"] == sid and root["kind"] == "hosted" and root["parent"] is None and root["depth"] == 0
    assert root["started_by"] == {"kind": "human", "name": "human"}
    assert [k["session_id"] for k in root["children"]] == [kid, mine], "oldest first"
    n = by_id(t)
    assert [n[x]["depth"] for x in (kid, grand, great)] == [1, 2, 3]
    assert [n[x]["parent"] for x in (kid, grand, great)] == [sid, kid, grand]
    assert {n[x]["root"] for x in (kid, grand, great, mine)} == {sid}
    assert [k["session_id"] for k in n[kid]["children"]] == [grand]
    assert [k["session_id"] for k in n[grand]["children"]] == [great]
    assert n[kid]["started_by"] == {"kind": "agent", "name": "claude", "via": "hosted", "session": sid}
    assert n[grand]["started_by"]["session"] == kid
    assert n[mine]["started_by"] == {"kind": "human", "name": "human"}
    # What `board session list` prints for a child, from the same view (hosted._human_view).
    k = n[kid]
    assert (k["name"], k["title"], k["role"], k["agent"], k["model"], k["access"], k["isolation"], k["node"]) == (
        "general-1", "look around", "general", "claude", "haiku", "workspace", "shared", "laptop")
    assert n[grand]["access"] == "read-only" and n[mine]["role"] == "review" and n[mine]["title"] == "human review"
    for x in walk(root):
        assert {"state", "turns", "queued", "in_flight", "created_at", "ended_at", "node_live", "closing"} <= set(x)
        assert "reply" not in x and "reply_for" not in x, "the reply comes only as capped `output`"
    assert trees(senv)["live_sessions"] == 5


def test_an_interactive_root_anchors_the_children_it_started(senv):
    grant(senv)
    kid = spawn(senv, interactive(senv), title="from my terminal")["child"]["id"]
    tick(senv)
    grand = spawn(senv, bound(senv, kid))["child"]["id"]
    tick(senv)
    t = only(senv)
    iroot = hs._find_iroot(senv.board.conn, "claude", PROJECT)
    assert (t["root"], t["kind"], t["agent"], t["project"]) == (iroot["session_id"], "interactive", "claude", PROJECT)
    assert t["sessions"] == 2, "the interactive root itself is no hosted session"
    a = t["session"]
    assert (a["kind"], a["state"], a["depth"]) == ("interactive", "open", 0)
    assert a["started_by"] == {"kind": "interactive", "name": "claude"}, "a conversation the human opened"
    [k] = a["children"]
    assert (k["session_id"], k["parent"], k["depth"]) == (kid, iroot["session_id"], 1)
    assert k["started_by"] == {"kind": "agent", "name": "claude", "via": "interactive", "session": iroot["session_id"]}
    assert [g["session_id"] for g in k["children"]] == [grand]
    assert k["children"][0]["started_by"]["via"] == "hosted"


def test_ended_members_stay_in_a_live_tree_and_ended_trees_need_all(senv):
    grant(senv)
    sid = a_root(senv)
    kid = spawn(senv, bound(senv, sid))["child"]["id"]
    other = spawn(senv, bound(senv, sid))["child"]["id"]
    tick(senv)
    assert senv.human.post(f"/api/hosted-sessions/{kid}/close").status_code == 200
    tick(senv)
    t = only(senv)
    n = by_id(t)
    assert n[kid]["state"] == "ended" and n[kid]["error"], "greyed in the view, with why it ended"
    assert n[other]["state"] != "ended"
    assert (t["sessions"], t["live"]) == (3, 2)
    assert trees(senv)["live_sessions"] == 2
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    tick(senv)
    assert trees(senv)["trees"] == [], "an ended tree is hidden by default"
    t = only(senv, all=True)
    assert t["live"] == 0 and all(x["state"] == "ended" for x in walk(t["session"]))
    assert trees(senv)["live_sessions"] == 0


def test_a_waiting_result_says_why_and_child_output_is_returned_as_capped_data(senv):
    sid = a_root(senv)
    kid = child(senv, sid, "do it")["session_id"]
    tick(senv)
    occupy(senv)        # a dispatched run uses the parent's checkout: the result waits
    long = HOSTILE + "x" * (hosted_lineage.OUTPUT_MAX + 500)
    answer(senv, kid, long)
    tick(senv, times=2)
    n = by_id(only(senv))
    res = n[kid]["result"]
    assert (res["outcome"], res["state"], res["message"], res["started_by"]) == ("completed", "pending", 1, "human")
    assert "a dispatched run is using the parent's checkout" in res["waiting_because"]
    assert "text" not in res, "the result's own copy of the reply is not repeated"
    out = n[kid]["output"]
    assert out["text"] == long[:hosted_lineage.OUTPUT_MAX] and out["text"].startswith(HOSTILE), "data, unchanged"
    assert (out["truncated"], out["length"], out["for_message"]) == (True, len(long), 1)
    assert n[sid]["result"] is None, "a root has no parent to send results to"


def test_short_hostile_output_comes_back_whole_and_unescaped(senv):
    sid = a_root(senv)
    send(senv, sid, "again")
    tick(senv)
    answer(senv, sid, HOSTILE)
    r = senv.human.get("/api/hosted-trees")
    assert r.headers["content-type"].startswith("application/json")
    out = r.json()["trees"][0]["session"]["output"]
    assert out == {"text": HOSTILE, "truncated": False, "length": len(HOSTILE), "for_message": 2}


def test_agents_and_nodes_get_nothing_new(senv):
    grant(senv)
    sid = a_root(senv)
    spawn(senv, bound(senv, sid))
    for who in (senv.agent, senv.laptop):
        r = who.get("/api/hosted-trees", params={"all": "true"})
        assert r.status_code == 403, r.text
        assert "trees" not in r.json() and sid not in r.text
    assert not gateway.allowed("GET", "/api/hosted-trees"), "the agent gateway never carries it"
    with pytest.raises(Exception, match="human"):
        hosted_lineage.trees(senv.board, senv.p["claude"])
    assert senv.human.post("/api/hosted-trees").status_code in (404, 405), "read-only: no write route"
    # The tab's count rides on the dashboard snapshot, for the human only.
    assert senv.human.get("/api/state").json()["hosted_live"] == 2
    assert "hosted_live" not in senv.agent.get("/api/state").json()


def test_the_dashboard_has_the_sessions_tab_and_sets_child_output_as_text_only():
    """The view's escaping, read off the page's source (the jsdom tests in tests/dashboard-sessions.test.cjs drive it
    with hostile output). `el` turns every string child into a text node and every attribute into setAttribute; the
    page never parses a string as HTML anywhere, and the output block is a <pre> whose only child is the text."""
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent / "agent_comms" / "dashboard.html").read_text()
    assert 'tab("sessions", "Sessions"' in html and '"/api/hosted-trees"' in html
    for parser in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                   "createContextualFragment"):
        assert parser not in html, parser
    assert "document.createTextNode(String(k))" in html, "el() adds string children as text nodes"
    assert 'el("pre", { class: "lin-output" + (long && !open ? " clamped" : ""), id }, text)' in html, \
        "output: one text child, nothing else"
    block = html[html.index("function outputBlock"):html.index("function linOutput")]
    assert "href" not in block and "marked" not in block.lower(), "no link and no markdown in an output block"
    assert "untrusted · plain text" in block


def dump(env) -> list[str]:
    return list(env.board.conn.iterdump())


def test_the_view_writes_nothing_and_says_what_a_settling_read_would_change(senv):
    """Sol round 1, S2. The lineage view and its details are a projection of the records AS STORED: neither request
    writes anything, also where `hosted._settle` would (a turn in flight on a node gone silent). The view says so
    beside the session (`unsettled`); the CLI's `show` still settles, as before."""
    from agent_comms import nodes
    sid = a_root(senv)
    kid = child(senv, sid, "go")["session_id"]
    tick(senv)
    send(senv, sid, "more")
    tick(senv)                                                   # the root's message 2 is now in flight
    senv.clock.advance(nodes.NODE_LOST_SECONDS + 5)              # ... and its node has gone silent
    before = dump(senv)
    t = only(senv)
    for x in (sid, kid):
        assert senv.human.get(f"/api/hosted-trees/{x}").status_code == 200
    trees(senv, all=True)
    assert dump(senv) == before, "no row changed"
    root = t["session"]
    assert root["in_flight"] == 2 and root["unsettled"] == hosted_lineage.LOST, "as stored, and why it is not settled"
    assert root["node_live"] is False
    shown = senv.human.get(f"/api/hosted-sessions/{sid}").json()     # `board session show`: settles, unchanged
    assert shown["in_flight"] is None and dump(senv) != before


def test_the_details_route_caps_the_reply_and_a_kept_results_text(senv):
    """Sol round 1, S1. The dashboard's details come from GET /api/hosted-trees/{id}, every agent text in it capped
    at OUTPUT_MAX with `truncated` and `length`; `show` keeps its whole reply."""
    big = HOSTILE + "y" * (hosted_lineage.OUTPUT_MAX + 100)
    sid = a_root(senv)
    send(senv, sid, "more")
    tick(senv)
    answer(senv, sid, big)
    kid = child(senv, sid, "do it")["session_id"]
    tick(senv)
    occupy(senv)
    answer(senv, kid, big)
    tick(senv, times=2)
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200   # its waiting result: undelivered
    tick(senv)
    d = senv.human.get(f"/api/hosted-trees/{sid}").json()
    assert d["output"] == {"text": big[:hosted_lineage.OUTPUT_MAX], "truncated": True, "length": len(big),
                           "for_message": 2}
    [x] = [x for x in d["results"] if x["child"] == kid and x["outcome"] == "completed"]
    assert x["state"] == "undelivered" and x["output"] == {"text": big[:hosted_lineage.OUTPUT_MAX], "truncated": True,
                                                           "length": len(big)}
    assert "text" not in x and "reply" not in d
    assert [m["n"] for m in d["messages"]] == [1, 2] and all("text" not in m for m in d["messages"])
    assert type(d["version"]) is str and d["version"] == by_id(only(senv, all=True))[sid]["version"]
    assert len(senv.human.get(f"/api/hosted-sessions/{sid}").json()["reply"]) == len(big), "show: unchanged"
    assert senv.human.get(f"/api/hosted-trees/{'0' * 8}-0000-4000-8000-{'0' * 12}").status_code == 404
    for who in (senv.agent, senv.laptop):
        assert who.get(f"/api/hosted-trees/{sid}").status_code == 403
    assert not gateway.allowed("GET", f"/api/hosted-trees/{sid}")


def test_a_version_changes_with_what_the_details_show(senv):
    sid = a_root(senv)
    v1 = only(senv)["session"]["version"]
    assert only(senv)["session"]["version"] == v1, "stable while nothing changes"
    send(senv, sid, "more")
    assert only(senv)["session"]["version"] != v1


def test_the_dispatchers_runs_are_read_once_per_request_however_many_results_wait(senv):
    """Sol round 1, S3. Why a result waits needs the dispatcher's runs: one read of them per request, not two per
    waiting result; and nothing is written."""
    with_spawn(senv, max_children=6)
    sid = a_root(senv)
    kids = [child(senv, sid, f"task {i}")["session_id"] for i in range(3)]   # root + 3: the node's max_live
    tick(senv)
    occupy(senv)
    for k in kids:
        answer(senv, k, "done")
    tick(senv, times=2)
    seen: list[str] = []
    senv.board.conn.set_trace_callback(seen.append)
    try:
        t = hosted_lineage.trees(senv.board, senv.p["human"])
    finally:
        senv.board.conn.set_trace_callback(None)
    waits = [n["result"]["waiting_because"] for n in walk(t["trees"][0]["session"]) if n.get("result")]
    assert len(waits) == 3 and all("dispatched run" in w for w in waits)
    assert sum("LIKE 'dispatch.run.%'" in q for q in seen) == 1, "the run records: one scan"
    assert sum("dispatch.checkout_claim" in q for q in seen) == 1, "the checkout claims: one read"
    assert not any(w in q for q in seen for w in ("INSERT", "UPDATE", "DELETE")), "and nothing written"
    assert hosted_lineage.live_count(senv.board) == 4


def test_the_tab_count_reads_only_what_the_session_reader_reads(senv):
    """Sol round 2, S3: `live_count` counts in SQL exactly the records `hosted._sessions` takes (a JSON object with a
    string session_id), recorded as not ended."""
    from agent_comms import hosted
    a_root(senv)
    with hosted.db.write_tx(senv.board.conn) as c:
        for key, value in (("odd-array", "[1, 2]"), ("odd-number-id", '{"session_id": 7, "state": "idle"}'),
                           ("odd-no-id", '{"state": "idle"}'), ("odd-not-json", "{nope"),
                           ("odd-string", '"idle"')):
            c.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'test', 0)",
                      (hosted.SESSION_PREFIX + key, value))
    stored = [r for r in hosted._sessions(senv.board.conn) if r.get("state") != "ended"]
    assert hosted_lineage.live_count(senv.board) == len(stored) == 1


def test_the_default_checkout_check_stops_at_an_unreadable_claim_as_before(senv, monkeypatch):
    """Sol round 2, S3: a caller that passes no runs reads the claims first and, at an unreadable one, answers
    without reading the run records (main's order)."""
    from agent_comms import dispatch, hosted
    with hosted.db.write_tx(senv.board.conn) as c:
        c.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, 'not json', 'test', 0)",
                  (dispatch.CLAIM_PREFIX + "x",))

    read: list[int] = []
    monkeypatch.setattr(dispatch, "_active_records", lambda board: read.append(1) or [])
    assert hosted._checkout_busy(senv.board, {"project": PROJECT, "node": "laptop"}) is True
    assert read == [], "the run records were not read"


def test_an_orphan_says_what_the_next_settling_read_does_and_stays_as_stored(senv):
    """Sol round 2, N2: a child its node can no longer run is still live as stored; the view says that the next
    settling read ends it, never that the hub already did."""
    from agent_comms import nodes
    sid = a_root(senv)
    kid = child(senv, sid, "go")["session_id"]
    tick(senv)
    info = nodes._get(senv.board.conn, nodes.NODE_PREFIX + "laptop")
    with hosted_lineage.hosted.db.write_tx(senv.board.conn) as c:       # its node now says it hosts no children
        nodes._put(c, nodes.NODE_PREFIX + "laptop", {k: v for k, v in info.items() if k != "session_host"}, "laptop",
                   senv.clock())
    before = dump(senv)
    t = only(senv)
    n = by_id(t)[kid]
    assert n["state"] != "ended" and t["live"] == 2
    assert n["unsettled"].startswith("its node can no longer run it (its latest hello says it hosts no child sessions)")
    assert "the next settling read" in n["unsettled"] and "ended it" not in n["unsettled"]
    assert dump(senv) == before


def test_the_version_is_the_details_themselves(senv):
    """Sol round 2, N1: the version is a hash of exactly what the details route returns, so it changes with any of
    it, the node's liveness included, and the route's own version is the tree's."""
    from agent_comms import nodes
    sid = a_root(senv)
    v1 = only(senv)["session"]["version"]
    d1 = senv.human.get(f"/api/hosted-trees/{sid}").json()
    assert d1["version"] == v1 and d1["node_live"] is True
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 1)             # nothing on record changed but the node's liveness
    v2 = only(senv)["session"]["version"]
    d2 = senv.human.get(f"/api/hosted-trees/{sid}").json()
    assert d2["node_live"] is False and v2 != v1 and d2["version"] == v2
    assert hosted_lineage._version({k: v for k, v in d2.items() if k != "version"}) == v2


def test_each_outbox_and_message_list_is_read_once_per_request(senv):
    """Sol round 3, N7: per request, each session's outbox is read once (its children's latest results come from
    that one read, indexed), and of its messages only the last SHOW_MESSAGES are read, so the reads grow with the
    number of sessions, not with sessions times children."""
    from agent_comms import hosted
    sid = a_root(senv)
    kids = [child(senv, sid, f"task {i}")["session_id"] for i in range(3)]
    tick(senv)
    for k in kids:
        answer(senv, k, "done")
    tick(senv, times=2)
    for _ in range(hosted.SHOW_MESSAGES + 3):                   # more messages than are shown
        send(senv, kids[0], "again")
    for path, call in (("trees", lambda: hosted_lineage.trees(senv.board, senv.p["human"])),
                       ("detail", lambda: hosted_lineage.detail(senv.board, senv.p["human"], sid))):
        seen: list[str] = []
        senv.board.conn.set_trace_callback(seen.append)
        try:
            got = call()
        finally:
            senv.board.conn.set_trace_callback(None)
        outboxes = [q for q in seen if "host.result." in q]
        messages = [q for q in seen if "host.msg." in q]
        if path == "trees":
            n = by_id({"session": got["trees"][0]["session"]})
            assert len(n) == 4 and all(n[k]["result"] for k in kids)
            assert len(outboxes) == 4, "each session's outbox once: 4 sessions, not 1 + 3 per child"
            assert sum(f"host.result.{sid}." in q for q in outboxes) == 1
            assert len(messages) == 4 and all("LIMIT" in q for q in messages)
            shown = [m["n"] for m in hosted_lineage._messages(senv.board.conn, kids[0])]
            assert shown == [m["n"] for m in hosted._messages(senv.board.conn, kids[0])[-hosted.SHOW_MESSAGES:]]
            assert len(shown) == hosted.SHOW_MESSAGES and shown[-1] == hosted.SHOW_MESSAGES + 4, "the last ones"
        else:
            assert len(outboxes) == 1 and len(messages) == 1 and "LIMIT" in messages[0]
