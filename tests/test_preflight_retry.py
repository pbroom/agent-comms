"""A request the dispatcher blocked at preflight is queued again by the dispatcher once the same checks pass, so the
human never has to unblock it by hand after fixing the configuration. Never spawns a real agent CLI or browser."""

from agent_comms import access, requests
from agent_comms.dispatch import Dispatcher

from test_access_routing import ORIGIN, hub_checkout, laptop, signed_in_request
from test_dispatch import allow, human_post, runs
from test_nodes import nenv  # noqa: F401  (fixture)


def state(env, post):
    [row] = env.board.get_post(env.p["human"], post["id"])["requests"]
    return row


def later(env, seconds=Dispatcher.PREFLIGHT_RETRY_SECONDS + 1):
    env.clock.advance(seconds)
    env.d.tick()


def test_a_preflight_block_is_requeued_and_launched_once_the_access_is_configured(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    allow(nenv, agents=["codex"])
    post = signed_in_request(nenv)
    nenv.d.tick()
    assert state(nenv, post)["state"] == "blocked" and state(nenv, post)["reason"].startswith("Preflight failed:")
    later(nenv)
    assert state(nenv, post)["state"] == "blocked" and nenv.spawner.calls == [], "still no access: stays blocked"
    nenv.d.config.runner_access = {"codex": [access.browser(ORIGIN)]}   # the human fixes the configuration
    later(nenv)
    assert len(nenv.spawner.calls) == 1
    assert [r for r in runs(nenv) if r["status"] == "running"][0]["browser_access"] == [ORIGIN]
    events = nenv.board.conn.execute("SELECT state, event_source, reason FROM request_events WHERE post_id=? "
                                     "ORDER BY id", (post["id"],)).fetchall()
    assert [(e["state"], e["event_source"]) for e in events][-2:] == [("blocked", "dispatcher"),
                                                                       ("queued", "dispatcher")]
    assert "now passes" in events[-1]["reason"] and "signed-in desktop browser" in events[-1]["reason"]


def test_a_preflight_block_goes_to_a_node_that_came_online_with_the_access(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    allow(nenv, agents=["codex"])
    post = signed_in_request(nenv)
    nenv.d.tick()
    assert state(nenv, post)["state"] == "blocked"
    nenv.clock.advance(Dispatcher.PREFLIGHT_RETRY_SECONDS + 1)
    laptop(nenv, agents=("codex",))    # the node comes online holding the sign-in
    nenv.d.tick()
    [run] = [r for r in runs(nenv) if r.get("node") == "laptop"]
    assert run["browser_access"] == [ORIGIN]


def test_a_block_the_agent_or_human_wrote_is_never_touched(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    nenv.d.config.runner_access = {"codex": [access.browser(ORIGIN)]}
    post = human_post(nenv, ["codex"], body="Plain work")
    row = state(nenv, post)
    requests.progress(nenv.board, nenv.p["codex"], nenv.sid["codex"], post["id"], "codex", "blocked",
                      "Preflight failed: I decided to stop", expected_version=row["version"])
    allow(nenv, agents=["codex"])
    later(nenv)
    assert state(nenv, post)["state"] == "blocked" and nenv.spawner.calls == []


def test_a_flipping_check_is_requeued_at_most_three_times_a_day(nenv, tmp_path, monkeypatch):
    hub_checkout(nenv, tmp_path)
    allow(nenv, agents=["codex"], max_launches=10)
    post = signed_in_request(nenv)
    nenv.d.tick()
    # The retry pass sees the check pass, the launch pass sees it fail again (a check that flickers).
    nenv.d.config.runner_access = {"codex": [access.browser(ORIGIN)]}
    real = Dispatcher._preflight
    flip = {"n": 0}

    def preflight(self, agent, rule, item=None):
        flip["n"] += 1
        return "flaky" if flip["n"] % 2 == 0 else real(self, agent, rule, item)
    monkeypatch.setattr(Dispatcher, "_preflight", preflight)
    for _ in range(8):
        later(nenv)
    queued = nenv.board.conn.execute("SELECT COUNT(*) FROM request_events WHERE post_id=? AND state='queued' "
                                     "AND event_source='dispatcher'", (post["id"],)).fetchone()[0]
    assert queued == Dispatcher.PREFLIGHT_RETRY_MAX and state(nenv, post)["state"] == "blocked"


def test_nothing_is_requeued_with_automatic_recovery_off(nenv, tmp_path):
    nenv.board.s.auto_recover_stalled_work = False
    hub_checkout(nenv, tmp_path)
    allow(nenv, agents=["codex"])
    post = signed_in_request(nenv)
    nenv.d.tick()
    nenv.d.config.runner_access = {"codex": [access.browser(ORIGIN)]}
    later(nenv)
    assert state(nenv, post)["state"] == "blocked" and nenv.spawner.calls == []
