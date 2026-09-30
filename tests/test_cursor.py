from conftest import PROJECT


def ids(r):
    return [p["body"] for p in r["posts"]]


def test_read_is_idempotent_until_ack(env):
    tid = env.thread()
    env.post("codex", tid, "a")
    env.post("codex", tid, "b")
    c, s = env.p["claude"], env.sid["claude"]
    r1 = env.board.read_updates(c, s)
    r2 = env.board.read_updates(c, s)
    assert ids(r1) == ids(r2) == ["a", "b"]  # "crash" before ack: nothing lost
    env.post("codex", tid, "c")
    r3 = env.board.read_updates(c, s, ack_through=r1["ack_through"])
    assert r3["acked_through"] == r1["ack_through"]
    assert ids(r3) == ["c"]
    assert ids(env.board.read_updates(c, s)) == ["c"]  # still unacked
    assert ids(env.board.read_updates(c, s, ack_through=r3["ack_through"])) == []


def test_ack_is_monotonic_and_clamped(env):
    tid = env.thread()
    env.post("codex", tid, "a")
    c, s = env.p["claude"], env.sid["claude"]
    r = env.board.read_updates(c, s)
    env.board.ack(c, s, r["ack_through"])
    env.board.ack(c, s, 0)  # going backwards is a no-op
    assert ids(env.board.read_updates(c, s)) == []
    env.board.ack(c, s, 10_000)  # clamped to the current max; future posts are not skipped
    env.post("codex", tid, "later")
    assert ids(env.board.read_updates(c, s)) == ["later"]


def test_limit_and_more(env):
    tid = env.thread()
    for i in range(5):
        env.post("codex", tid, str(i))
    c, s = env.p["claude"], env.sid["claude"]
    r = env.board.read_updates(c, s, limit=2)
    assert ids(r) == ["0", "1"] and r["more"]
    r = env.board.read_updates(c, s, ack_through=r["ack_through"], limit=2)
    assert ids(r) == ["2", "3"]


def test_scope_project_and_addressed(env):
    mine = env.thread("mine")
    other = env.board.create_thread(env.p["human"], env.sid["human"], "elsewhere", "/other/repo")["id"]
    env.post("codex", mine, "in my project")
    env.post("codex", other, "not for me")
    env.post("codex", other, "for claude", to=["claude"], needs_response=True)
    c, s = env.p["claude"], env.sid["claude"]
    assert ids(env.board.read_updates(c, s)) == ["in my project", "for claude"]
    assert ids(env.board.read_updates(c, s, only="addressed")) == ["for claude"]
    assert ids(env.board.read_updates(c, s, only="needs_response")) == ["for claude"]
    # acking one thread leaves the others unread
    last = env.board.read_updates(c, s)["ack_through"]
    env.board.ack(c, s, last, thread_id=mine)
    assert ids(env.board.read_updates(c, s)) == ["for claude"]


def test_cursor_is_per_session_and_excludes_own_session(env):
    tid = env.thread()
    env.post("claude", tid, "my own")
    env.post("codex", tid, "theirs")
    c, s1 = env.p["claude"], env.sid["claude"]
    r = env.board.read_updates(c, s1)
    assert ids(r) == ["theirs"]
    s2 = env.session("claude", PROJECT, "/wt/2")
    assert ids(env.board.read_updates(c, s2)) == ["my own", "theirs"]
    env.board.ack(c, s1, r["ack_through"])
    # a parallel claude session keeps its own read position: s1 acking does not hide posts from s2
    assert ids(env.board.read_updates(c, s1)) == []
    assert ids(env.board.read_updates(c, s2)) == ["my own", "theirs"]
    assert ids(env.board.read_updates(env.p["codex"], env.sid["codex"])) == ["my own"]


def test_new_session_starts_at_agents_read_position(env):
    tid = env.thread()
    env.post("codex", tid, "old")
    c, s1 = env.p["claude"], env.sid["claude"]
    env.board.ack(c, s1, env.board.read_updates(c, s1)["ack_through"])
    env.post("codex", tid, "new")
    s2 = env.session("claude", PROJECT, "/wt/restarted")
    assert ids(env.board.read_updates(c, s2)) == ["new"]  # no history replay
    s3 = env.board.register_session(c, PROJECT, resume_session_id=s1)["session_id"]
    assert s3 == s1 and ids(env.board.read_updates(c, s1)) == ["new"]
    # an agent that has never acked anything starts from the beginning
    assert ids(env.board.read_updates(env.p["grok"], env.session("grok"))) == ["old", "new"]


def test_finalized_decision_resurfaces(env):
    tid = env.thread()
    d = env.post("claude", tid, "use sqlite", "decision")
    assert d["decision_status"].startswith("proposal")
    c, s = env.p["codex"], env.sid["codex"]
    r = env.board.read_updates(c, s)
    env.board.ack(c, s, r["ack_through"])
    env.board.finalize(env.p["human"], d["id"])
    r = env.board.read_updates(c, s)
    assert [p["id"] for p in r["posts"]] == [d["id"]] and r["posts"][0]["decision_status"] == "final"
    # and it resurfaces for the author too, even though it is their own post
    r = env.board.read_updates(env.p["claude"], env.sid["claude"])
    assert [p["id"] for p in r["posts"]] == [d["id"]]
