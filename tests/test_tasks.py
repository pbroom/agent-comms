import threading

import pytest

from agent_comms.core import Conflict, Forbidden
from conftest import make_env


def test_claim_race_exactly_one_winner(tmp_path):
    env = make_env(tmp_path)
    tid = env.thread()
    racers = []
    for i in range(8):
        name = ("claude", "codex", "grok")[i % 3]
        racers.append((name, env.session(name, worktree=f"/wt/{i}")))
    for round_ in range(10):
        task = env.accepted_task(tid, title=f"race {round_}")
        barrier = threading.Barrier(len(racers))
        wins, losses, errors = [], [], []

        def go(name, sid):
            barrier.wait()
            try:
                env.board.claim_task(env.p[name], sid, task)
                wins.append((name, sid))
            except Conflict:
                losses.append(sid)
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [threading.Thread(target=go, args=r) for r in racers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(wins) == 1 and len(losses) == len(racers) - 1
        row = env.board.get_task(env.p["human"], task)
        assert (row["owner_agent"], row["owner_session"]) == wins[0]
        assert [e["event"] for e in row["events"]] == ["create", "claim"]


def test_lease_expiry_and_reclaim(env):
    tid = env.thread()
    task = env.accepted_task(tid)
    t = env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    assert t["status"] == "working" and t["lease_state"] == "active" and t["lease_seconds_left"] == 30 * 60

    with pytest.raises(Conflict, match="leased by claude"):
        env.board.claim_task(env.p["codex"], env.sid["codex"], task)

    env.clock.advance(29 * 60)
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)  # renew
    env.clock.advance(29 * 60)
    with pytest.raises(Conflict):
        env.board.claim_task(env.p["codex"], env.sid["codex"], task)

    env.clock.advance(2 * 60)  # renewed lease has now expired
    assert env.board.get_task(env.p["codex"], task)["lease_state"] == "expired"
    t = env.board.claim_task(env.p["codex"], env.sid["codex"], task)
    assert t["owner_agent"] == "codex"
    events = env.board.get_task(env.p["human"], task)["events"]
    assert [e["event"] for e in events] == ["create", "claim", "renew", "reclaim"]
    assert "claude" in events[-1]["note"]

    # the old holder can no longer finish or renew
    with pytest.raises(Forbidden):
        env.board.transition_task(env.p["claude"], env.sid["claude"], task, "done")
    with pytest.raises(Conflict):
        env.board.renew_task(env.p["claude"], env.sid["claude"], task)


def test_same_agent_other_session_cannot_steal(env):
    tid = env.thread()
    task = env.accepted_task(tid)
    other = env.session("claude", worktree="/wt/b")
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    with pytest.raises(Conflict):
        env.board.claim_task(env.p["claude"], other, task)


def test_release_and_lifecycle(env):
    tid = env.thread()
    task = env.accepted_task(tid)
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    with pytest.raises(Forbidden):
        env.board.release_task(env.p["codex"], env.sid["codex"], task)
    env.board.transition_task(env.p["claude"], env.sid["claude"], task, "blocked", "waiting on review")
    t = env.board.release_task(env.p["claude"], env.sid["claude"], task, "handing off")
    assert t["status"] == "accepted" and t["owner_agent"] is None
    env.board.claim_task(env.p["codex"], env.sid["codex"], task)
    with pytest.raises(Conflict, match="cannot move"):
        env.board.transition_task(env.p["codex"], env.sid["codex"], task, "proposed")
    t = env.board.transition_task(env.p["codex"], env.sid["codex"], task, "done")
    assert t["status"] == "done" and t["owner_agent"] is None
    with pytest.raises(Conflict):
        env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    events = env.board.get_task(env.p["human"], task)["events"]
    assert [(e["event"], e["from"], e["to"], e["agent"]) for e in events] == [
        ("create", None, "accepted", "human"),
        ("claim", "accepted", "working", "claude"),
        ("transition", "working", "blocked", "claude"),
        ("release", "blocked", "accepted", "claude"),
        ("claim", "accepted", "working", "codex"),
        ("transition", "working", "done", "codex"),
    ]


def test_proposed_tasks_are_open_to_agents_by_default(env):
    assert env.settings.require_human_accept is False
    tid = env.thread()
    post = env.post("claude", tid, "let's add a rate limiter", "proposal",
                    propose_task={"title": "rate limiter", "intends_files": ["app/limits.py"]})
    task = post["task_id"]
    claimed = env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    assert (claimed["status"], claimed["owner_agent"]) == ("working", "claude")
    other = env.post("claude", tid, "and tests", "proposal", propose_task={"title": "tests"})["task_id"]
    accepted = env.board.transition_task(env.p["codex"], env.sid["codex"], other, "accepted")
    assert accepted["status"] == "accepted"


def test_turning_the_gate_back_on_reblocks_agent_accepted_work(env):
    tid = env.thread()
    task = env.post("claude", tid, "x", "proposal", propose_task={"title": "x"})["task_id"]
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    env.settings.require_human_accept = True
    with pytest.raises(Forbidden):
        env.board.transition_task(env.p["claude"], env.sid["claude"], task, "done")


def test_proposed_tasks_need_human_accept(env):
    env.settings.require_human_accept = True  # the gate is off by default; this covers it switched on
    tid = env.thread()
    post = env.post("claude", tid, "let's add a rate limiter", "proposal",
                    propose_task={"title": "rate limiter", "intends_files": ["app/limits.py"]})
    task = post["task_id"]
    assert env.board.get_task(env.p["claude"], task)["status"] == "proposed"
    with pytest.raises(Conflict, match="human to accept"):
        env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    with pytest.raises(Forbidden):
        env.board.transition_task(env.p["codex"], env.sid["codex"], task, "accepted")
    env.board.transition_task(env.p["human"], env.sid["human"], task, "accepted")
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)


def test_dependencies_and_file_conflicts(env):
    tid = env.thread()
    a = env.accepted_task(tid, title="a", intends_files=["x.py", "y.py"])
    b = env.accepted_task(tid, title="b", depends_on=[a])
    c = env.accepted_task(tid, title="c", intends_files=["y.py"])
    with pytest.raises(Conflict, match="unfinished"):
        env.board.claim_task(env.p["codex"], env.sid["codex"], b)
    env.board.claim_task(env.p["claude"], env.sid["claude"], a)
    out = env.board.claim_task(env.p["codex"], env.sid["codex"], c)
    assert "y.py" in out["file_conflict_warnings"][0]
    env.board.transition_task(env.p["claude"], env.sid["claude"], a, "done")
    env.board.claim_task(env.p["grok"], env.sid["grok"], b)
