"""Claude steering switched OFF (`[dispatch.sessions] claude_steer = false`; DESIGN_NOTES "Mid-turn steering, step
3", review rounds 9 and 10, rulings F, G1 and G2; "Enabling Claude steering", ruling S4).

Claude steering is on by default since the enabling PR; this module sets the switch explicitly to false on every
configuration made here (before any node or adapter starts: each `SessionsConfig` is made with `claude_steer = false`,
as a file with that line would make it), and every steering-only entry point is a tripwire: called at all, it raises.
Run under them:
- every Codex steering scenario of tests/test_codex_steering.py (imported below, not edited: they run here a second
  time, under the tripwires);
- the Codex turn timeout with a live steer and bytes still in the output buffer (Sol's round-10 sequence), with the
  group registry busy: main e6b421f's `_fail` then `_stop_from`, as on main;
- a Claude turn with a steer request (main's `REFUSED["cli"]`);
- shutdown with a Codex steer live and a Claude turn running.
None fires: with the switch off, every stop is main's code."""
import threading

import pytest

from agent_comms import dispatch, hosted_steer, node_groups, node_sessions, session_host
from agent_comms.providers import claude

TRIPWIRES = [
    (node_groups.Groups, "try_stopping"),       # the reservation without waiting
    (session_host.SessionHost, "_reserved_stop"),       # the reserved stop (enabling PR, S2)
    (session_host.SessionHost, "_hold"),        # a hold's creation
    (session_host.SessionHost, "_publish_hold"),    # the fence's publication (and every other of a held end)
    (session_host.SessionHost, "_hold_cut"),    # the hold deadline enforcers' cut
    (session_host.SessionHost, "_steer_adjudicate"),    # the adjudicator of a Claude steer's evidence (S4)
    (session_host.SessionHost, "_retire_hold"),     # a held end's retirement (S4)
    (claude.ClaudeSession, "steer_named"),      # a Claude steer frame's construction
    (claude.ClaudeSession, "steer_facts"),      # the adapter's attempt facts (S4)
    (claude.ClaudeSession, "steer_done"),       # ... and the attempt's retirement in the adapter (S4)
    (node_sessions.SessionRunner, "_checkpoint"),       # a checkpoint's enqueue
    (node_sessions.SessionRunner, "_post_checkpoints"),     # ... and its send
]


@pytest.fixture(autouse=True)
def tripwires(monkeypatch):
    """The switch set explicitly to false on every configuration made from here on (before any node or adapter
    starts), and every steering-only entry point raises if called and is recorded: a host error path that swallowed
    the raise cannot hide it (the test fails at its end)."""
    assert dispatch.SessionsConfig().claude_steer is True, "on by default (S4)"
    real_init = dispatch.SessionsConfig.__init__

    def off(self, *a, **k):
        real_init(self, *a, **k)
        self.claude_steer = False       # (as `claude_steer = false` in the node's file)
    monkeypatch.setattr(dispatch.SessionsConfig, "__init__", off)
    assert dispatch.SessionsConfig.from_dict({}).claude_steer is False
    assert node_groups and claude.ClaudeSession.steering_on is False, "the adapter's own word, until the host's"
    fired = []
    for cls, name in TRIPWIRES:
        def trip(*a, _n=f"{cls.__name__}.{name}", **k):
            fired.append(_n)
            raise AssertionError(f"tripwire: {_n} ran with Claude steering off")
        monkeypatch.setattr(cls, name, trip)
    yield
    assert not fired, f"tripwires fired: {fired}"


from test_codex_steering import *  # noqa: F401,F403,E402  (every Codex steering scenario, under the tripwires)
from test_codex_steering import host_n  # noqa: E402
from test_session_host import SID, henv  # noqa: F401,E402


def _codex_timeout_with_a_live_steer_and_bytes_pending(henv):
    """A Codex turn whose steer frame the pipe took in part just before the turn's own timeout: a live attempt, its
    bytes still in the output buffer (so the interrupt is skipped), the timeout reached."""
    host, s, child, _ = host_n(henv)
    henv.clock.advance(host.timeout_minutes * 60 - 1)
    child.accept = 10
    host.steer_offer(SID, "a", "u1", "steer text", authorized=True)
    host.tick()
    assert s.steer is not None and 0 < s.steer.sent < s.steer.length and s.outbuf, "S taken in part, bytes pending"
    henv.clock.advance(2)
    assert s.outbuf, "the output buffer is NON-empty at the timeout: the stop path, not the interrupt"
    return host, s, child


@pytest.mark.parametrize("switch", ["off", "on"])
def test_r10_g1_a_codex_timeout_with_a_live_steer_is_mains(henv, monkeypatch, switch):
    """Sol's round-10 blocker (ruling G1, H1): the Codex timeout with a live steer, bytes pending, the registry busy:
    the turn is failed and stopped by main e6b421f's path (`_fail`, then `_stop_from`, which waits for the registry
    as main's does), never a reservation that commits nothing. The same with Claude steering switched on: Codex is
    still main's."""
    if switch == "on":
        monkeypatch.setattr(session_host.SessionHost, "claude_steer", lambda self: True)
    host, s, child = _codex_timeout_with_a_live_steer_and_bytes_pending(henv)
    calls = []
    real_fail, real_stop_from = host._fail, host._stop_from
    monkeypatch.setattr(host, "_fail", lambda *a, **k: (calls.append("_fail"), real_fail(*a, **k))[1])
    monkeypatch.setattr(host, "_stop_from", lambda *a, **k: (calls.append("_stop_from"), real_stop_from(*a, **k))[1])
    release, held = threading.Event(), threading.Event()

    def spawn_holding_the_registry():
        with host.groups.lock:
            held.set()
            release.wait(10)
    threading.Thread(target=spawn_holding_the_registry, daemon=True).start()
    assert held.wait(3)
    out, done = [], threading.Event()

    def loop():
        out.extend(host.tick())
        done.set()
    threading.Thread(target=loop, daemon=True).start()
    done.wait(0.5)          # (main's stop waits for the registry here)
    release.set()
    assert done.wait(5)
    assert calls[:2] == ["_fail", "_stop_from"], calls
    ended = [e for e in out if e.kind == "turn_ended" and e.message_id == "a"]
    assert len(ended) == 1 and not ended[0].ok, "the turn failed, once"
    assert child.terminated == 1, "and its process got main's SIGTERM"


def test_r10_g2_a_steer_for_a_claude_turn_is_refused_as_on_main(env):
    """Ruling G2: a Claude turn with a steer request, the switch off: main's `cli` refusal; no tripwire fires."""
    from test_hosted_sessions import start, tick
    from test_hosted_tree import proc_of
    from test_hosted_sessions import written
    from test_claude_steering import cinit, TEXT
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    child.emit(cinit(sid, written(child)[-1]["uuid"]))
    tick(env, times=2)
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert hosted_steer.REFUSED["cli"] in r.text, r.text
    tick(env, times=2)
    assert not [x for x in written(child) if x.get("message", {}).get("content") == TEXT]


def test_r10_g2_shutdown_with_a_codex_steer_live_and_a_claude_turn(env):
    """Ruling G2: shutdown with a Codex steer live and a Claude turn running: everything stops, no tripwire fires."""
    from test_codex_steering import codex_turn_in_flight, post_steer
    from test_hosted_sessions import P3, start, tick
    from test_hosted_tree import proc_of
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    cl = proc_of(env, sid)[1]
    b, cb = codex_turn_in_flight(env, project=P3)
    post_steer(env, b)
    tick(env)
    runner = env.node.sessions
    runner.signal()
    runner.kill()
    runner.shutdown()
    assert cl.terminated and cb.terminated
