import pytest

from agent_comms.core import Forbidden, Invalid, LimitExceeded, Paused
from conftest import make_env


def test_thread_cap_without_human(tmp_path):
    env = make_env(tmp_path, max_agent_posts_per_thread_without_human=4)
    tid = env.thread()
    for i in range(4):
        env.post(("claude", "codex")[i % 2], tid, f"msg {i}")
    with pytest.raises(LimitExceeded, match="needs the human"):
        env.post("grok", tid)
    # sealed posts count too
    with pytest.raises(LimitExceeded):
        env.post("claude", tid, sealed=True)
    env.post("human", tid, "carry on")
    for i in range(4):
        env.post("claude", tid, f"again {i}")
    with pytest.raises(LimitExceeded):
        env.post("claude", tid)
    # other threads are unaffected
    env.post("claude", env.thread("other"))


def test_daily_cap_rolling_24h(tmp_path):
    env = make_env(tmp_path, daily_post_cap_per_agent=3, max_agent_posts_per_thread_without_human=100)
    tid = env.thread()
    for _ in range(3):
        env.post("claude", tid)
        env.clock.advance(3600)
    with pytest.raises(LimitExceeded, match="daily post cap"):
        env.post("claude", tid)
    env.post("codex", tid)  # per agent
    env.clock.advance(21 * 3600 + 1)  # first post is now > 24h old
    env.post("claude", tid)
    with pytest.raises(LimitExceeded):
        env.post("claude", tid)


def test_pause_rejects_all_agent_writes(env):
    tid = env.thread()
    task = env.accepted_task(tid)
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    task2 = env.accepted_task(tid, title="t2")
    with pytest.raises(Forbidden):
        env.board.set_paused(env.p["claude"], True)
    env.board.set_paused(env.p["human"], True)

    c, s = env.p["codex"], env.sid["codex"]
    for attempt in (
        lambda: env.post("codex", tid),
        lambda: env.board.create_post(c, s, body="x", type="status", new_thread_title="new"),
        lambda: env.board.create_thread(c, s, "new"),
        lambda: env.board.create_task(c, s, tid, title="t"),
        lambda: env.board.claim_task(c, s, task2),
        lambda: env.board.claim_task(env.p["claude"], env.sid["claude"], task),  # renew
        lambda: env.board.transition_task(env.p["claude"], env.sid["claude"], task, "blocked"),
        lambda: env.board.release_task(env.p["claude"], env.sid["claude"], task),
        lambda: env.board.set_summary(c, s, tid, "summary"),
    ):
        with pytest.raises(Paused):
            attempt()
    # reads and cursor acks still work; the human can still write
    r = env.board.read_updates(c, s)
    assert r["paused"] is True
    env.board.ack(c, s, 0)
    env.post("human", tid, "paused while I look")
    env.board.set_paused(env.p["human"], False)
    env.post("codex", tid)


def test_body_limit_and_refs(env):
    tid = env.thread()
    env.post("claude", tid, "x" * 4096)
    with pytest.raises(Invalid, match="Point, don't paste"):
        env.post("claude", tid, "é" * 2049)  # 4098 bytes
    with pytest.raises(Invalid):
        env.post("claude", tid, refs=[{"kind": "blob", "path": "x"}])
    with pytest.raises(Invalid):
        env.post("claude", tid, refs=[{"kind": "commit", "path": "/repo"}])  # commit needs rev
    with pytest.raises(Invalid, match="finding must reference"):
        env.post("codex", tid, "looks fine", "finding")
    p = env.post("codex", tid, "looks fine", "finding", refs=[{"kind": "file", "path": "a.py", "rev": "abc123"}])
    assert p["refs"] == [{"kind": "file", "path": "a.py", "rev": "abc123"}]
