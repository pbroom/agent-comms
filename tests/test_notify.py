"""Human notifications: what notifies, filters, sealed text, failure isolation, argv form, dedupe."""

import json
import subprocess

import pytest

from agent_comms import notify
from agent_comms.core import Board, Forbidden, Invalid
from agent_comms.notify import HumanNotifier, MacOSDeliverer, Notification, osascript_argv

from conftest import ASK, make_env

REF = [{"kind": "commit", "path": "/work/repo", "rev": "abc123"}]
SECRET = "SECRET-SEALED-TEXT"


class FakeDeliverer:
    def __init__(self):
        self.sent: list[Notification] = []

    def available(self) -> bool:
        return True

    def __call__(self, n: Notification) -> None:
        self.sent.append(n)

    def text(self) -> str:
        return "\n".join(" | ".join(n.argv_fields()) for n in self.sent)


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def __call__(self, delay, fn):
        self.calls.append((delay, fn))

    def run(self):
        calls, self.calls = self.calls, []
        for _, fn in calls:
            fn()


@pytest.fixture
def nenv(tmp_path):
    env = make_env(tmp_path)
    env.fake = FakeDeliverer()
    env.sched = FakeScheduler()
    env.board.notifier = HumanNotifier(env.board, deliverer=env.fake, min_interval=0, schedule=env.sched)
    return env


def on(env, **kw):
    return env.board.subscribe_notifications(env.p["human"], **kw)


# ---------------------------------------------------------------- which events notify


def test_notifies_on_question_for_human_addressed_and_decision(nenv):
    on(nenv)
    tid = nenv.thread()
    nenv.post("claude", tid, "Which DB?", "question", needs_response=True, decision_question=ASK)
    nenv.post("codex", tid, "ping human", "status", to=["human"])
    nenv.post("claude", tid, "Use SQLite", "decision", decision_question=ASK)
    kinds = [n.kind for n in nenv.fake.sent]
    assert kinds == ["needs-response", "to-human", "decision"]
    first = nenv.fake.sent[0]
    assert first.argv_fields() == ["agent-comms", f"claude · question · thread {tid}", "Needs your response: Which DB?"]


def test_does_not_notify_for_routine_posts_or_human_posts(nenv):
    on(nenv)
    tid = nenv.thread()
    nenv.post("claude", tid, "working on it")                                   # plain status
    nenv.post("claude", tid, "codex?", "question", to=["codex"], needs_response=True)  # needs codex, not human
    nenv.post("human", tid, "decided", "decision", final=True)                  # human's own post
    nenv.post("human", tid, "q", "question", needs_response=True)
    nenv.board.create_thread(nenv.p["claude"], nenv.sid["claude"], "new thread")
    tsk = nenv.accepted_task(tid)
    nenv.board.claim_task(nenv.p["claude"], nenv.sid["claude"], tsk)
    assert nenv.fake.sent == []


def test_needs_response_addressed_to_human_and_agent_still_notifies(nenv):
    tid = nenv.thread()
    # One stored before asking posts had to be the human's alone (made here with notifications still off) ...
    r = nenv.post("claude", tid, "both of you", "question", to=["codex"], needs_response=True)
    nenv.board.conn.execute("UPDATE posts SET to_agents = ? WHERE id = ?", ('["codex", "human"]', r["id"]))
    on(nenv, events=["needs-response"])
    # ... while a new post cannot ask the human and an agent at once.
    with pytest.raises(Invalid):
        nenv.post("claude", tid, "both of you", "question", to=["codex", "human"], needs_response=True)
    assert nenv.fake.sent == []
    nenv.board.notifier("post.created", {"post_id": r["id"]})
    assert [n.kind for n in nenv.fake.sent] == ["needs-response"]


def test_event_selection_limits_what_notifies(nenv):
    on(nenv, events=["decision"])
    tid = nenv.thread()
    nenv.post("claude", tid, "q", "question", needs_response=True, decision_question=ASK)
    nenv.post("claude", tid, "d", "decision", decision_question=ASK)
    assert [n.kind for n in nenv.fake.sent] == ["decision"]


def test_idle_agent_nudge_counts_only(nenv):
    on(nenv, events=["idle-agent"], idle_minutes=10)
    tid = nenv.thread()
    nenv.post("claude", tid, "codex please look", "request", to=["codex"])   # codex active: nothing
    assert nenv.fake.sent == []
    nenv.clock.advance(11 * 60)
    nenv.post("claude", tid, "codex, still there? SECRET-ish text", "request", to=["codex"])
    assert len(nenv.fake.sent) == 1
    n = nenv.fake.sent[0]
    assert n.kind == "idle-agent" and n.message == "codex has 2 unread post(s) addressed to it"
    assert "SECRET" not in nenv.fake.text() and "please" not in nenv.fake.text()
    # once per idle stretch
    nenv.post("claude", tid, "again", "request", to=["codex"])
    assert len(nenv.fake.sent) == 1
    # codex comes back, goes idle again: nudged again
    nenv.board.heartbeat(nenv.p["codex"], nenv.sid["codex"])
    nenv.clock.advance(11 * 60)
    nenv.post("claude", tid, "and again", "request", to=["codex"])
    assert len(nenv.fake.sent) == 2


def test_sealed_first_post_does_not_burn_idle_nudge(nenv):
    on(nenv, events=["idle-agent"], idle_minutes=10)
    tid = nenv.thread()
    nenv.clock.advance(11 * 60)
    # codex cannot see a sealed post, so there is nothing to report and the marker must stay unclaimed
    nenv.post("claude", tid, SECRET, "request", to=["codex"], sealed=True)
    assert nenv.fake.sent == []
    nenv.post("claude", tid, "visible ask", "request", to=["codex"])
    assert [n.message for n in nenv.fake.sent] == ["codex has 1 unread post(s) addressed to it"]
    assert SECRET not in nenv.fake.text()


# ---------------------------------------------------------------- subscriptions and filters


def test_nothing_without_a_subscription(nenv):
    tid = nenv.thread()
    nenv.post("claude", tid, "q", "question", needs_response=True, decision_question=ASK)
    nenv.post("claude", tid, "d", "decision", to=["human"], decision_question=ASK)
    assert nenv.fake.sent == []


def test_off_stops_notifications(nenv):
    on(nenv)
    assert len(nenv.board.unsubscribe_notifications(nenv.p["human"])) == 1
    tid = nenv.thread()
    nenv.post("claude", tid, "q", "question", needs_response=True, decision_question=ASK)
    assert nenv.fake.sent == [] and nenv.board.list_notification_subscriptions(nenv.p["human"]) == []


def test_project_and_thread_filters(nenv):
    b, h = nenv.board, nenv.p["human"]
    t1 = nenv.thread("one")
    t2 = nenv.thread("two")
    other = b.create_thread(h, nenv.sid["human"], "elsewhere", "/work/other")["id"]
    on(nenv, project="/work/repo/", thread_id=t1)          # path normalized
    for t in (t1, t2, other):
        nenv.post("claude", t, f"q{t}", "question", needs_response=True, decision_question=ASK)
    assert [n.thread_ids for n in nenv.fake.sent] == [(t1,)]
    b.unsubscribe_notifications(h)
    nenv.fake.sent.clear()
    on(nenv, project="/work/other")
    for t in (t1, other):
        nenv.post("claude", t, f"again {t}", "question", needs_response=True, decision_question=ASK)
    assert [n.thread_ids for n in nenv.fake.sent] == [(other,)]


def test_on_twice_replaces_same_scope(nenv):
    on(nenv)
    on(nenv, events=["decision"])
    subs = nenv.board.list_notification_subscriptions(nenv.p["human"])
    assert [s["events"] for s in subs] == [["decision"]]


def test_agents_cannot_manage_human_subscriptions(nenv):
    for name in ("claude", "codex"):
        with pytest.raises(Forbidden):
            nenv.board.subscribe_notifications(nenv.p[name])
        with pytest.raises(Forbidden):
            nenv.board.unsubscribe_notifications(nenv.p[name])
        with pytest.raises(Forbidden):
            nenv.board.list_notification_subscriptions(nenv.p[name])


def test_rows_not_owned_by_the_human_are_ignored(nenv):
    # Even a row written straight into the table for an agent never notifies anyone.
    nenv.board.conn.execute(
        "INSERT INTO subscriptions(agent, events, channel, active, created_at) VALUES ('claude', ?, 'macos', 1, 0)",
        (json.dumps(list(notify.NOTIFY_EVENTS)),))
    tid = nenv.thread()
    nenv.post("codex", tid, "q", "question", needs_response=True, decision_question=ASK)
    assert nenv.fake.sent == []


def test_invalid_subscriptions_rejected(nenv):
    with pytest.raises(Invalid):
        on(nenv, events=["post.created"])
    with pytest.raises(Invalid):
        on(nenv, events=[])
    with pytest.raises(Invalid):
        on(nenv, events=["idle-agent"], idle_minutes=0)
    with pytest.raises(Invalid):
        on(nenv, idle_minutes=5)  # without idle-agent


# ---------------------------------------------------------------- content


def test_sealed_text_never_included(nenv):
    on(nenv)
    tid = nenv.thread()
    task = nenv.accepted_task(tid)
    nenv.post("codex", tid, SECRET, "finding", task_id=task, refs=REF, sealed=True, to=["codex", "human"])
    nenv.post("claude", tid, SECRET, "decision", sealed=True, decision_question=ASK)
    assert len(nenv.fake.sent) == 2
    assert SECRET not in nenv.fake.text()
    assert all(n.message.endswith(": sealed post") for n in nenv.fake.sent)


def test_body_is_cleaned_and_truncated(nenv):
    on(nenv)
    tid = nenv.thread()
    body = "line1\nline2\t\x1b[31mred‮" + "x" * 300
    nenv.post("claude", tid, body, "question", needs_response=True, decision_question=ASK)
    msg = nenv.fake.sent[0].message
    assert "\n" not in msg and "\x1b" not in msg and "‮" not in msg
    assert msg.startswith("Needs your response: line1 line2 [31mred")
    assert len(msg) <= len("Needs your response: ") + notify.SNIPPET_CHARS
    assert nenv.tokens["claude"] not in nenv.fake.text()


# ---------------------------------------------------------------- failure isolation


def test_deliverer_exception_does_not_break_create_post(nenv):
    def boom(n):
        raise RuntimeError("osascript exploded")

    nenv.board.notifier = HumanNotifier(nenv.board, deliverer=boom, min_interval=0)
    on(nenv)
    tid = nenv.thread()
    r = nenv.post("claude", tid, "still posted", "question", needs_response=True, decision_question=ASK)
    assert nenv.board.get_post(nenv.p["human"], r["id"])["body"] == "still posted"


def test_raising_notifier_does_not_break_writes(nenv):
    def boom(event, payload):
        raise RuntimeError("bad notifier")

    nenv.board.notifier = boom
    tid = nenv.thread()
    r = nenv.post("claude", tid, "ok", "question", needs_response=True, decision_question=ASK)
    assert r["body"] == "ok"
    nenv.board.finalize(nenv.p["human"], nenv.post("human", tid, "d", "decision")["id"])


def test_broken_subscription_row_is_skipped(nenv):
    nenv.board.conn.execute("INSERT INTO subscriptions(agent, project, events, channel, active, created_at) "
                            "VALUES ('human', '/work/repo', 'not json', 'macos', 1, 0)")
    tid = nenv.thread()
    nenv.post("claude", tid, "q", "question", needs_response=True, decision_question=ASK)
    assert nenv.fake.sent == []                       # the broken row alone notifies nothing, and doesn't crash
    on(nenv)                                          # a different scope, so the broken row stays active
    nenv.post("claude", tid, "q2", "question", needs_response=True, decision_question=ASK)
    assert len(nenv.fake.sent) == 1
    assert len(nenv.board.list_notification_subscriptions(nenv.p["human"])) == 2


def test_unavailable_deliverer_does_nothing(nenv):
    class Off(FakeDeliverer):
        def available(self):
            return False

    off = Off()
    nenv.board.notifier = HumanNotifier(nenv.board, deliverer=off, min_interval=0)
    on(nenv)
    nenv.post("claude", nenv.thread(), "q", "question", needs_response=True, decision_question=ASK)
    assert off.sent == []


def test_default_board_notifier_is_human_notifier(tmp_path):
    env = make_env(tmp_path)
    assert isinstance(env.board.notifier, HumanNotifier)
    assert isinstance(env.board.notifier.deliverer, MacOSDeliverer)


# ---------------------------------------------------------------- osascript argv


def test_osascript_argv_exact_and_no_shell():
    calls = []

    class Proc:
        def wait(self, timeout=None):
            return 0

    def popen(argv, **kw):
        calls.append((argv, kw))
        return Proc()

    d = MacOSDeliverer(popen=popen, platform="darwin")
    evil = 'x" & (do shell script "touch /tmp/pwned") & "'
    d(Notification("agent-comms", "claude · question · thread 3", f"Needs your response: {evil}\n$(id)"))
    argv, kw = calls[0]
    assert argv == [
        "/usr/bin/osascript",
        "-e", "on run argv",
        "-e", "display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)",
        "-e", "end run",
        "agent-comms",
        "claude · question · thread 3",
        f"Needs your response: {evil} $(id)",
    ]
    assert kw["shell"] is False
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL
    assert kw["start_new_session"] is True
    assert not any("TOKEN" in k for k in kw["env"])


def test_argv_helper_matches_deliverer():
    n = Notification("agent-comms", "s", "m")
    assert osascript_argv(n)[-3:] == ["agent-comms", "s", "m"]
    assert all("m" != a for a in osascript_argv(n)[:-3])  # text only after the fixed script


def test_platform_gating(tmp_path):
    assert not MacOSDeliverer(platform="linux").available()
    assert not MacOSDeliverer(platform="darwin", osascript=str(tmp_path / "missing")).available()


# ---------------------------------------------------------------- dedupe and coalescing


def test_at_most_one_notification_per_post(nenv):
    on(nenv)
    tid = nenv.thread()
    # needs-response + to-human + decision all match: still one notification
    r = nenv.post("claude", tid, "d", "decision", to=["human"], needs_response=True, decision_question=ASK)
    assert len(nenv.fake.sent) == 1 and nenv.fake.sent[0].kind == "needs-response"
    # the same event replayed (e.g. a second notifier call) is deduped
    nenv.board.notifier("post.created", {"post_id": r["id"]})
    assert len(nenv.fake.sent) == 1


def test_burst_is_coalesced(nenv):
    nenv.board.notifier = n = HumanNotifier(nenv.board, deliverer=nenv.fake, min_interval=30, schedule=nenv.sched)
    on(nenv)
    tid = nenv.thread()
    for i in range(5):
        nenv.post("claude" if i % 2 else "codex", tid, f"q{i}", "question", needs_response=True, decision_question=ASK)
    assert len(nenv.fake.sent) == 1               # first goes out immediately
    assert len(nenv.sched.calls) == 1             # one flush scheduled for the rest
    nenv.clock.advance(31)
    nenv.sched.run()
    assert len(nenv.fake.sent) == 2
    c = nenv.fake.sent[1]
    assert c.kind == "coalesced" and c.subtitle == "4 board items need you"
    assert c.message == f"from claude, codex in thread(s) {tid}"
    n.flush()                                     # nothing left
    assert len(nenv.fake.sent) == 2


def test_flush_waits_only_for_the_remaining_window(nenv):
    nenv.board.notifier = HumanNotifier(nenv.board, deliverer=nenv.fake, min_interval=30, schedule=nenv.sched)
    on(nenv)
    tid = nenv.thread()
    nenv.post("claude", tid, "q1", "question", needs_response=True, decision_question=ASK)        # sent at t0
    nenv.clock.advance(28)
    nenv.post("codex", tid, "q2", "question", needs_response=True, decision_question=ASK)         # rejected 28 s into the window
    assert len(nenv.fake.sent) == 1
    assert [d for d, _ in nenv.sched.calls] == [pytest.approx(2.0)]        # not a full 30 s
    nenv.clock.advance(2)
    nenv.sched.run()
    assert [n.message for n in nenv.fake.sent][1:] == ["Needs your response: q2"]
    # rejected at the very end of a window: the floor keeps the timer from spinning
    nenv.clock.advance(29.99)
    nenv.post("claude", tid, "q3", "question", needs_response=True, decision_question=ASK)
    assert [d for d, _ in nenv.sched.calls] == [notify.MIN_FLUSH_DELAY]


def test_rate_gate_is_shared_across_processes(nenv):
    """Two Boards on one database stand in for two processes (e.g. two stdio MCP servers)."""
    other = Board(nenv.settings, clock=nenv.clock)
    fake2, sched2 = FakeDeliverer(), FakeScheduler()
    other.notifier = HumanNotifier(other, deliverer=fake2, min_interval=30, schedule=sched2)
    nenv.board.notifier = HumanNotifier(nenv.board, deliverer=nenv.fake, min_interval=30, schedule=nenv.sched)
    on(nenv)
    tid = nenv.thread()
    nenv.post("claude", tid, "q1", "question", needs_response=True, decision_question=ASK)
    other.create_post(nenv.p["codex"], nenv.sid["codex"], body="q2", type="question", thread_id=tid,
                      needs_response=True, decision_question=ASK)
    assert len(nenv.fake.sent) == 1 and fake2.sent == [] and len(sched2.calls) == 1
    nenv.clock.advance(31)
    sched2.run()
    assert [n.message for n in fake2.sent] == ["Needs your response: q2"]
