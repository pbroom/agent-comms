"""Settings page: human-only API, validation, writes to board.local.toml only, hot reload in running processes,
audit, and the notification / dispatcher endpoints it uses."""

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from agent_comms import board_settings, core, dispatch
from agent_comms.api import create_app
from agent_comms.config import Settings, create_agent
from agent_comms.core import Board, Conflict, Forbidden, LimitExceeded
from agent_comms.dispatch import DispatchConfig, Dispatcher
from agent_comms.notify import HumanNotifier

from conftest import PROJECT, FakeClock

BOARD_TOML = """# shipped settings (committed)
[server]
host = "127.0.0.1"
port = 8787
db_path = "data/board.db"
agents_path = "agents.toml"

[limits]
lease_ttl_minutes = 30
daily_post_cap_per_agent = 200   # rolling 24 hours

[tasks]
require_human_accept = false

[dispatch]
live_minutes = 2
max_concurrent = 2

[dispatch.runners]
"claude-code" = ["claude", "-p", "{prompt}"]
"""

LOCAL_TOML = """# per-machine overrides; keep my comments
[limits]
body_max_bytes = 8192   # longer posts here

[dispatch.runners]
"codex-cli" = [
  "codex", "exec", "--sandbox", "workspace-write",
  "{prompt}",   # [limits] inside a value is not a header
]

[dispatch.env]
"codex-cli" = ["CODEX_HOME"]

[dispatch.worktrees]
"/work/repo" = "/work/repo-dispatch"
"""


class SEnv:
    def __init__(self, home, board, clock, tokens):
        self.home, self.board, self.clock, self.tokens = home, board, clock, tokens
        self.client = TestClient(create_app(board))
        self.p = {n: board.authenticate(t) for n, t in tokens.items()}
        self.sid = {n: board.register_session(self.p[n], PROJECT)["session_id"] for n in tokens}

    @property
    def local(self):
        return self.home / "board.local.toml"

    def h(self, who="human"):
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def put(self, changes, who="human"):
        return self.client.put("/api/settings", json=changes, headers=self.h(who))

    def get(self, who="human"):
        return self.client.get("/api/settings", headers=self.h(who))


@pytest.fixture
def senv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    (home / "board.toml").write_text(BOARD_TOML)
    (home / "board.local.toml").write_text(LOCAL_TOML)
    s = Settings.load()
    assert s.config_path == home / "board.toml" and s.body_max_bytes == 8192
    tokens = {"human": create_agent(s.agents_path, "human", "human", is_human=True),
              "codex": create_agent(s.agents_path, "codex", "codex-cli"),
              "claude": create_agent(s.agents_path, "claude", "claude-code")}
    clock = FakeClock()
    return SEnv(home, Board(s, clock=clock), clock, tokens)


def by_key(payload):
    return {x["key"]: x for x in payload["settings"]}


def touch_later(path):
    """Make sure a rewrite is seen as a change even on coarse-mtime filesystems."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


# ---------------------------------------------------------------- access


def test_settings_and_admin_routes_are_human_only(senv):
    routes = [("GET", "/api/settings", None), ("PUT", "/api/settings", {"limits.max_refs": 5}),
              ("GET", "/api/admin/notifications", None), ("POST", "/api/admin/notifications", {}),
              ("POST", "/api/admin/notifications/1/remove", None), ("POST", "/api/admin/notifications/test", None),
              ("GET", "/api/admin/dispatch", None), ("POST", "/api/admin/dispatch/stop", None),
              ("POST", "/api/admin/dispatch/rules", {"thread_id": 1, "agents": ["codex"], "purpose": "p",
                                                      "max_launches": 1}),
              ("POST", "/api/admin/dispatch/rules/1/revoke", None)]
    before = senv.local.read_bytes()
    for method, path, body in routes:
        r = senv.client.request(method, path, json=body, headers=senv.h("codex"))
        assert r.status_code == 403, (method, path, r.text)
        assert senv.client.request(method, path, json=body).status_code == 401
    assert senv.local.read_bytes() == before
    # core enforces it too, for any caller that bypasses the HTTP dependency
    with pytest.raises(Forbidden):
        board_settings.get_settings(senv.board, senv.p["codex"])
    with pytest.raises(Forbidden):
        board_settings.update_settings(senv.board, senv.p["claude"], {"limits.max_refs": 5})


def test_get_reports_values_sources_bounds_and_agents_without_secrets(senv):
    r = senv.get()
    assert r.status_code == 200
    d = r.json()
    s = by_key(d)
    assert set(s) == set(board_settings.EDITABLE)
    assert (s["limits.body_max_bytes"]["value"], s["limits.body_max_bytes"]["source"]) == (8192, "board.local.toml")
    assert (s["limits.daily_post_cap_per_agent"]["value"], s["limits.daily_post_cap_per_agent"]["source"]) == \
        (200, "board.toml")
    assert (s["limits.max_refs"]["value"], s["limits.max_refs"]["source"]) == (20, "default")
    assert s["dispatch.max_concurrent"]["source"] == "board.toml" and s["dispatch.timeout_minutes"]["value"] == 30.0
    assert s["limits.lease_ttl_minutes"]["min"] == 1 and s["limits.lease_ttl_minutes"]["max"] == 1440
    assert {(a["name"], a["runtime"], a["is_human"]) for a in d["agents"]} == \
        {("human", "human", True), ("codex", "codex-cli", False), ("claude", "claude-code", False)}
    text = r.text
    for tok in senv.tokens.values():
        assert tok not in text
    for line in (senv.home / "agents.toml").read_text().splitlines():
        if "token_sha256" in line:
            assert line.split('"')[1] not in text
    assert "token" not in json.dumps(d["agents"])


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("changes", [
    {"limits.lease_ttl_minutes": 0}, {"limits.lease_ttl_minutes": 1441}, {"limits.daily_post_cap_per_agent": -1},
    {"limits.body_max_bytes": 100}, {"limits.body_max_bytes": 1_000_000}, {"limits.max_refs": 0},
    {"limits.max_agent_posts_per_thread_without_human": 5000}, {"dispatch.max_concurrent": 21},
    {"dispatch.live_minutes": 0.5}, {"dispatch.poll_seconds": 0}, {"dispatch.timeout_minutes": 2000},
    {"dispatch.kill_grace_seconds": 301},
    {"limits.max_refs": 2.5}, {"limits.max_refs": "5"}, {"limits.max_refs": True}, {"dispatch.max_concurrent": 1.5},
    {"tasks.require_human_accept": 1}, {"tasks.require_human_accept": "true"}, {"dispatch.poll_seconds": "5"},
    {"dispatch.poll_seconds": [5]}, {}, {"limits.max_refs": {"x": 1}},
])
def test_out_of_bounds_and_wrong_types_are_rejected(senv, changes):
    before = senv.local.read_bytes()
    r = senv.put(changes)
    assert r.status_code in (400, 422), (changes, r.text)
    assert senv.local.read_bytes() == before


@pytest.mark.parametrize("key", [
    "server.host", "server.port", "server.db_path", "server.agents_path", "host", "port", "db_path", "agents_path",
    "dispatch.runners", "dispatch.env", "dispatch.worktrees", "runners", "env", "worktrees",
    'dispatch.runners.claude-code', "dispatch.worktrees./work/repo", "limits.unknown_key", "lease_ttl_minutes",
    "tasks.require_human_accept.x", "nonsense",
])
def test_unknown_and_file_only_keys_are_rejected(senv, key):
    before = senv.local.read_bytes()
    r = senv.put({key: ["evil", "{prompt}"] if "runners" in key else 1})
    assert r.status_code == 400, (key, r.text)
    assert senv.local.read_bytes() == before
    if any(x in key for x in ("host", "port", "path", "runners", "env", "worktrees")):
        assert "not editable from the dashboard" in r.json()["message"]
    # mixing a valid key with a refused one writes nothing
    assert senv.put({"limits.max_refs": 7, key: 1}).status_code == 400
    assert senv.local.read_bytes() == before


def test_accepts_bounds_and_whole_numbers_for_float_settings(senv):
    r = senv.put({"limits.lease_ttl_minutes": 1440, "dispatch.live_minutes": 1, "dispatch.poll_seconds": 2.5,
                  "dispatch.max_concurrent": 20})
    assert r.status_code == 200, r.text
    s = by_key(r.json())
    assert s["limits.lease_ttl_minutes"]["value"] == 1440 and s["dispatch.poll_seconds"]["value"] == 2.5


# ---------------------------------------------------------------- persistence


def test_writes_only_board_local_toml_mode_600_preserving_unrelated_content(senv):
    board_toml = senv.home / "board.toml"
    shipped, shipped_mtime = board_toml.read_bytes(), board_toml.stat().st_mtime_ns
    os.chmod(senv.local, 0o644)
    top_before = sorted(p.name for p in senv.home.iterdir())
    r = senv.put({"limits.daily_post_cap_per_agent": 150, "limits.lease_ttl_minutes": 45,
                  "tasks.require_human_accept": True, "dispatch.max_concurrent": 3})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["changed"]) == sorted(["limits.daily_post_cap_per_agent", "limits.lease_ttl_minutes",
                                                  "tasks.require_human_accept", "dispatch.max_concurrent"])
    assert board_toml.read_bytes() == shipped and board_toml.stat().st_mtime_ns == shipped_mtime
    assert sorted(p.name for p in senv.home.iterdir()) == top_before          # no temp or backup files left
    assert stat.S_IMODE(senv.local.stat().st_mode) == 0o600
    text = senv.local.read_text()
    # every original line is still there, byte for byte and in order
    it = iter(text.splitlines())
    assert all(any(line == x for x in it) for line in LOCAL_TOML.splitlines())
    assert "body_max_bytes = 8192   # longer posts here" in text
    import tomllib
    data = tomllib.loads(text)
    assert data["limits"] == {"body_max_bytes": 8192, "daily_post_cap_per_agent": 150, "lease_ttl_minutes": 45}
    assert data["tasks"] == {"require_human_accept": True}
    assert data["dispatch"]["max_concurrent"] == 3
    assert data["dispatch"]["runners"]["codex-cli"][-1] == "{prompt}"
    assert data["dispatch"]["env"] == {"codex-cli": ["CODEX_HOME"]}
    assert data["dispatch"]["worktrees"] == {"/work/repo": "/work/repo-dispatch"}
    # the shipped [dispatch] keys still come from board.toml, merged under the local table
    merged = Settings.load()
    assert merged.dispatch["live_minutes"] == 2 and merged.dispatch["runners"]["claude-code"][0] == "claude"


def test_editing_an_existing_key_keeps_its_comment_and_null_removes_the_override(senv):
    r = senv.put({"limits.body_max_bytes": 16384})
    assert r.status_code == 200
    assert "body_max_bytes = 16384   # longer posts here\n" in senv.local.read_text()
    assert senv.board.s.body_max_bytes == 16384
    r = senv.put({"limits.body_max_bytes": None})
    assert r.status_code == 200
    assert "body_max_bytes" not in senv.local.read_text()
    s = by_key(r.json())["limits.body_max_bytes"]
    assert (s["value"], s["source"]) == (4096, "default")
    assert senv.board.s.body_max_bytes == 4096


def test_creates_board_local_toml_when_absent(senv):
    senv.local.unlink()
    r = senv.put({"limits.max_refs": 5})
    assert r.status_code == 200
    assert senv.local.read_text() == "[limits]\nmax_refs = 5\n"
    assert stat.S_IMODE(senv.local.stat().st_mode) == 0o600


def test_malformed_local_file_is_never_overwritten(senv):
    senv.local.write_text("[limits\nmax_refs = \n")
    before = senv.local.read_bytes()
    r = senv.put({"limits.max_refs": 5})
    assert r.status_code == 409 and "Edit it by hand" in r.json()["message"]
    assert senv.local.read_bytes() == before
    assert senv.get().json()["files"]["local_error"]


def test_layout_the_editor_cannot_handle_is_refused(senv):
    senv.local.write_text('limits = { max_refs = 3 }\n')
    before = senv.local.read_bytes()
    assert senv.put({"limits.max_refs": 5}).status_code == 409
    assert senv.local.read_bytes() == before


def test_audit_records_who_when_key_old_and_new(senv):
    senv.clock.t = 1_800_000_000.0
    senv.put({"limits.daily_post_cap_per_agent": 150})
    senv.put({"limits.daily_post_cap_per_agent": 150})          # no change: nothing recorded
    senv.put({"tasks.require_human_accept": True, "limits.body_max_bytes": None})
    audit = senv.get().json()["audit"]
    assert [(e["key"], e["old"], e["new"]) for e in audit] == [
        ("limits.body_max_bytes", 8192, 4096), ("tasks.require_human_accept", False, True),
        ("limits.daily_post_cap_per_agent", 200, 150)] or \
        [(e["key"], e["old"], e["new"]) for e in audit] == [
        ("tasks.require_human_accept", False, True), ("limits.body_max_bytes", 8192, 4096),
        ("limits.daily_post_cap_per_agent", 200, 150)]
    assert all(e["by"] == "human" and e["file"] == "board.local.toml" and e["at"].startswith("2027-01-15")
               for e in audit)
    path = senv.board.s.db_path.parent / board_settings.AUDIT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(path.read_text().splitlines()) == 3


def test_board_built_in_code_cannot_save(env):
    client = TestClient(create_app(env.board))
    r = client.put("/api/settings", json={"limits.max_refs": 5}, headers={"Authorization": f"Bearer {env.tokens['human']}"})
    assert r.status_code == 409
    g = client.get("/api/settings", headers={"Authorization": f"Bearer {env.tokens['human']}"}).json()
    assert g["files"]["writable"] is False and {x["source"] for x in g["settings"]} == {"process"}


# ---------------------------------------------------------------- hot reload


def test_running_board_sees_changed_daily_cap_and_require_human_accept(senv):
    """A second Board (another process: a stdio MCP server, say) picks up the edit on its next authentication."""
    other = Board(Settings.load(), clock=senv.clock)
    codex = other.authenticate(senv.tokens["codex"])
    sid = other.register_session(codex, PROJECT)["session_id"]
    tid = other.create_thread(other.authenticate(senv.tokens["human"]), senv.sid["human"], "t", PROJECT)["id"]
    other.create_post(codex, sid, body="one", type="status", thread_id=tid)
    other.create_post(codex, sid, body="two", type="status", thread_id=tid)

    assert senv.put({"limits.daily_post_cap_per_agent": 2, "tasks.require_human_accept": True}).status_code == 200
    touch_later(senv.local)
    codex = other.authenticate(senv.tokens["codex"])            # every request re-checks the files
    assert other.s.daily_post_cap_per_agent == 2 and other.s.require_human_accept is True
    with pytest.raises(LimitExceeded, match="daily post cap"):
        other.create_post(codex, sid, body="three", type="status", thread_id=tid)
    claude = other.authenticate(senv.tokens["claude"])
    csid = other.register_session(claude, PROJECT)["session_id"]
    task = other.create_post(claude, csid, body="p", type="proposal", thread_id=tid,
                             propose_task={"title": "x"})["task_id"]
    with pytest.raises(Conflict, match="needs the human"):
        other.claim_task(codex, sid, task)
    assert other.limits()["require_human_accept"] is True


def test_auto_recover_stalled_work_is_on_by_default_and_the_human_can_turn_it_off(senv):
    item = by_key(senv.get().json())["tasks.auto_recover_stalled_work"]
    assert item["value"] is True and item["type"] == "bool" and item["source"] == "default"
    assert senv.put({"tasks.auto_recover_stalled_work": False}, who="codex").status_code == 403
    assert senv.put({"tasks.auto_recover_stalled_work": "off"}).status_code == 400
    assert senv.put({"tasks.auto_recover_stalled_work": False}).status_code == 200
    assert senv.board.s.auto_recover_stalled_work is False
    assert "auto_recover_stalled_work = false" in senv.local.read_text()
    other = Board(Settings.load(), clock=senv.clock)          # another process loads it too
    assert other.s.auto_recover_stalled_work is False


def test_hand_edit_is_reloaded_like_agents_toml(senv):
    senv.local.write_text(LOCAL_TOML.replace("8192", "1024"))
    touch_later(senv.local)
    senv.board.authenticate(senv.tokens["codex"])
    assert senv.board.s.body_max_bytes == 1024
    (senv.home / "board.toml").write_text(BOARD_TOML.replace("lease_ttl_minutes = 30", "lease_ttl_minutes = 5"))
    touch_later(senv.home / "board.toml")
    senv.board.authenticate(senv.tokens["codex"])
    assert senv.board.s.lease_ttl_minutes == 5


@pytest.mark.parametrize("bad", ["[limits\nmax_refs = ", '[limits]\nmax_refs = "many"\n',
                                 "[limits]\nnot_a_setting = 1\n", '[dispatch]\nmax_concurrent = 0\n',
                                 '[dispatch.runners]\n"codex-cli" = ["bash", "-c", "{prompt}"]\n'])
def test_malformed_local_file_keeps_the_last_good_settings(senv, bad, caplog):
    assert senv.put({"limits.daily_post_cap_per_agent": 50}).status_code == 200
    gen = senv.board.settings_generation
    senv.local.write_text(bad)
    touch_later(senv.local)
    p = senv.board.authenticate(senv.tokens["human"])            # does not raise
    assert senv.board.s.daily_post_cap_per_agent == 50 and senv.board.s.body_max_bytes == 8192
    assert senv.board.settings_generation == gen and senv.board.settings_error
    assert "keeping the last good settings" in caplog.text
    assert senv.get().json()["files"]["reload_error"]
    senv.board.create_thread(p, senv.sid["human"], "still works", PROJECT)
    senv.local.write_text("[limits]\ndaily_post_cap_per_agent = 75\n")   # fixed: applied on the next request
    touch_later(senv.local)
    senv.board.authenticate(senv.tokens["human"])
    assert senv.board.s.daily_post_cap_per_agent == 75 and senv.board.settings_error is None


def test_restart_only_keys_are_not_applied_live(senv, caplog):
    senv.local.write_text(LOCAL_TOML + '\n[server]\nport = 9999\n')
    touch_later(senv.local)
    senv.board.authenticate(senv.tokens["human"])
    assert senv.board.s.port == 8787 and "restart" in caplog.text


def test_dispatcher_applies_new_scalars_without_restart(senv):
    config = DispatchConfig.load()
    d = Dispatcher(senv.board, senv.p["human"], config, spawner=lambda *a, **k: None, log_dir=senv.home / "logs")
    d.acquire_loop()
    runners = dict(config.runners)
    other = Board(Settings.load(), clock=senv.clock)                 # the HTTP server, say
    TestClient(create_app(other)).put("/api/settings", headers=senv.h(), json={
        "dispatch.max_concurrent": 5, "dispatch.timeout_minutes": 12, "dispatch.poll_seconds": 3,
        "dispatch.live_minutes": 4, "dispatch.kill_grace_seconds": 7})
    touch_later(senv.local)
    d.tick()
    assert (config.max_concurrent, config.timeout_minutes, config.poll_seconds, config.live_minutes,
            config.kill_grace_seconds) == (5, 12, 3, 4, 7)
    assert config.runners == runners                                  # runners are not reloaded
    senv.local.write_text("[dispatch]\nmax_concurrent = -1\n")        # invalid: the running values stay
    touch_later(senv.local)
    d.tick()
    assert config.max_concurrent == 5
    d.release_loop()


# ---------------------------------------------------------------- notifications and dispatcher endpoints


class FakeDeliverer:
    def __init__(self):
        self.sent = []

    def available(self):
        return True

    def __call__(self, n):
        self.sent.append(n)


def test_notification_rules_add_list_remove_and_test(senv):
    h = senv.h()
    tid = senv.board.create_thread(senv.p["human"], senv.sid["human"], "t", PROJECT)["id"]
    r = senv.client.post("/api/admin/notifications", headers=h, json={
        "events": ["needs-response", "idle-agent"], "project": PROJECT, "thread_id": tid, "idle_minutes": 15})
    assert r.status_code == 200, r.text
    rule = r.json()
    assert rule["events"] == ["needs-response", "idle-agent"] and rule["idle_minutes"] == 15
    assert senv.client.post("/api/admin/notifications", headers=h, json={"events": ["bogus"]}).status_code == 400
    assert senv.client.post("/api/admin/notifications", headers=h,
                            json={"events": ["to-human"], "idle_minutes": 5}).status_code == 400
    listed = senv.client.get("/api/admin/notifications", headers=h).json()
    assert [x["id"] for x in listed["rules"]] == [rule["id"]] and listed["deliverable"] is False
    assert senv.client.post("/api/admin/notifications/test", headers=h).status_code == 409   # not on this machine
    fake = FakeDeliverer()
    senv.board.notifier = HumanNotifier(senv.board, deliverer=fake)
    assert senv.client.post("/api/admin/notifications/test", headers=h).json() == {"sent": True}
    assert len(fake.sent) == 1 and fake.sent[0].kind == "test"
    r = senv.client.post(f"/api/admin/notifications/{rule['id']}/remove", headers=h)
    assert r.status_code == 200 and r.json()["removed"][0]["id"] == rule["id"]
    assert senv.client.get("/api/admin/notifications", headers=h).json()["rules"] == []
    assert senv.client.post(f"/api/admin/notifications/{rule['id']}/remove", headers=h).status_code == 404


def test_dispatch_overview_approve_revoke_and_stop(senv):
    h = senv.h()
    tid = senv.board.create_thread(senv.p["human"], senv.sid["human"], "parser", PROJECT)["id"]
    o = senv.client.get("/api/admin/dispatch", headers=h).json()
    assert o["status"] == {"running": False} and o["rules"] == [] and o["runs"] == []
    assert set(o["runners"]) == {"claude-code", "codex-cli"} and o["env"] == {"codex-cli": ["CODEX_HOME"]}
    assert o["worktrees"] == {"/work/repo": "/work/repo-dispatch"}
    assert [t["id"] for t in o["threads"]] == [tid] and [a["name"] for a in o["agents"]] == ["claude", "codex"]

    body = {"thread_id": tid, "agents": ["codex"], "purpose": "Review the parser rewrite only", "max_launches": 5,
            "expires_in_hours": 24}
    r = senv.client.post("/api/admin/dispatch/rules", headers=h, json=body)
    assert r.status_code == 200, r.text
    rule = r.json()
    assert (rule["agents"], rule["launches_left"], rule["max_launches"], rule["state"]) == (["codex"], 5, 5, "active")
    assert rule["expires_at"].startswith("2027-01-16")
    for bad in ({**body, "purpose": " "}, {**body, "max_launches": 0}, {**body, "agents": ["human"]},
                {**body, "agents": []}, {**body, "expires_in_hours": -1}, {**body, "thread_id": 999}):
        assert senv.client.post("/api/admin/dispatch/rules", headers=h, json=bad).status_code in (400, 404, 422), bad
    no_expiry = senv.client.post("/api/admin/dispatch/rules", headers=h, json={**body, "expires_in_hours": None})
    assert no_expiry.json()["expires_at"] is None
    assert [x["id"] for x in senv.client.get("/api/admin/dispatch", headers=h).json()["rules"]] == \
        [rule["id"], no_expiry.json()["id"]]
    r = senv.client.post(f"/api/admin/dispatch/rules/{rule['id']}/revoke", headers=h)
    assert r.json()["state"] == "revoked"

    assert senv.client.post("/api/admin/dispatch/stop", headers=h).json()["was_running"] is False
    assert senv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?", (Dispatcher.STOP_KEY,)).fetchone() is None
    d = Dispatcher(senv.board, senv.p["human"], DispatchConfig.load(), spawner=lambda *a, **k: None)
    d.acquire_loop()
    o = senv.client.get("/api/admin/dispatch", headers=h).json()
    assert o["status"]["running"] is True and o["status"]["heartbeat_seconds_ago"] == 0
    assert senv.client.post("/api/admin/dispatch/stop", headers=h).json() == {"requested": True, "was_running": True}
    assert d.stop_requested()                                       # the same flag `board dispatch stop` sets
    d.release_loop()


def test_settings_routes_are_not_reachable_through_the_chatgpt_gateway():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "gateway", Path(__file__).resolve().parents[1] / "integrations/chatgpt/gateway.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    for mode in ("mcp", "actions"):
        for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"), ("GET", "/api/admin/dispatch"),
                             ("POST", "/api/admin/notifications")):
            assert not gateway.allowed(mode, method, path)


def test_configuration_rejected_reload_is_visible_and_refresh_preserves_bytes(senv):
    senv.local.write_text('[limits]\ndaily_post_cap_per_agent = 1000\n[dispatch]\nunknown_future_option = true\n')
    before = senv.local.read_bytes()
    status = senv.client.get('/api/configuration', headers=senv.h('codex')).json()
    assert status['state'] == 'stale'
    assert status['error'] == core.CONFIG_ERROR_FOR_AGENTS             # agents get the generic message
    assert status['effective_limits']['daily_post_cap_per_agent'] == 200
    human = senv.client.get('/api/configuration', headers=senv.h()).json()
    assert 'unknown_future_option' in human['error']                 # the human gets the details
    result = senv.client.post('/api/configuration/refresh', headers=senv.h()).json()
    assert result['applied'] is False and result['state'] == 'stale'
    assert senv.local.read_bytes() == before
    registered = senv.board.register_session(senv.p['codex'], PROJECT)
    assert registered['configuration']['state'] == 'stale'
    assert senv.board.snapshot(senv.p['codex'])['configuration']['state'] == 'stale'
    assert senv.board.snapshot(senv.p['human'])['configuration']['state'] == 'stale'
    updates = senv.board.read_updates(senv.p['codex'], registered['session_id'])
    assert updates['configuration']['state'] == 'stale'
    senv.local.write_text('[limits]\ndaily_post_cap_per_agent = 1000\n')
    result = senv.client.post('/api/configuration/refresh', headers=senv.h()).json()
    assert result['applied'] is True and result['state'] == 'current'
    assert result['effective_limits']['daily_post_cap_per_agent'] == 1000


def test_refresh_retries_unchanged_signature(senv, monkeypatch):
    original = senv.board._settings_files_sig()
    monkeypatch.setattr(senv.board, '_settings_files_sig', lambda: original)
    senv.local.write_text('[limits]\ndaily_post_cap_per_agent = 1000\n')
    assert senv.board.reload_settings() is False
    assert senv.board.refresh_configuration(senv.p['human'])['effective_limits']['daily_post_cap_per_agent'] == 1000


def test_configuration_restart_only_and_authenticated_routes(senv):
    senv.local.write_text('[server]\nport = 9898\n')
    result = senv.client.post('/api/configuration/refresh', headers=senv.h()).json()
    assert result['state'] == 'restart_required'
    assert result['restart_required'] == ['port']
    assert senv.board.s.port == 8787
    for method, path in [('GET', '/api/configuration'), ('POST', '/api/configuration/refresh')]:
        assert senv.client.request(method, path).status_code == 401
    assert senv.client.get('/api/whoami', headers=senv.h('codex')).json()['configuration']['state'] == 'restart_required'


def test_configuration_refresh_is_human_only(senv, monkeypatch):
    import asyncio
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    assert senv.client.post('/api/configuration/refresh', headers=senv.h('codex')).status_code == 403
    with pytest.raises(Forbidden):
        senv.board.refresh_configuration(senv.p['codex'])
    monkeypatch.setenv('AGENT_COMMS_TOKEN', senv.tokens['codex'])

    async def run():
        async with Client(build_mcp(senv.board, 'stdio')) as client:
            result = await client.call_tool('board_configuration_status', {})
            assert not result.is_error
            status = json.loads(result.content[0].text)
            assert status['state'] == 'current'
            assert status['effective_limits']['daily_post_cap_per_agent'] == 200
            refused = await client.call_tool('board_refresh_configuration', {})
            assert refused.is_error and 'forbidden' in refused.content[0].text
    asyncio.run(run())


def test_configuration_refresh_retries_rejected_unchanged_file(senv, monkeypatch):
    validator = core.check_reloadable
    senv.local.write_text('[limits]\ndaily_post_cap_per_agent = 1000\n')
    def incompatible(settings):
        raise ValueError('unsupported runtime configuration')
    monkeypatch.setattr(core, 'check_reloadable', incompatible)
    assert senv.board.reload_settings() is False
    assert senv.board.configuration_status()['state'] == 'stale'
    monkeypatch.setattr(core, 'check_reloadable', validator)
    assert senv.board.reload_settings() is False
    result = senv.board.refresh_configuration(senv.p['human'])
    assert result['applied'] and result['error'] is None
    assert result['effective_limits']['daily_post_cap_per_agent'] == 1000


def test_configuration_changed_during_validation_is_not_applied(senv, monkeypatch):
    validator = core.check_reloadable
    senv.local.write_text('[limits]\ndaily_post_cap_per_agent = 1000\n')
    def changing(settings):
        validator(settings)
        senv.local.write_text('[limits]\ndaily_post_cap_per_agent = 300\n')
        touch_later(senv.local)
    monkeypatch.setattr(core, 'check_reloadable', changing)
    assert senv.board.reload_settings() is False
    assert senv.board.s.daily_post_cap_per_agent == 200
    assert 'changed while being read' in senv.board.settings_error


# ---------------------------------------------------------------- a changed installed source never blocks settings


@pytest.mark.parametrize("source_unavailable", [False, True])
def test_settings_still_apply_when_the_installed_source_changed(senv, monkeypatch, source_unavailable):
    """The board runs from an editable install, so any pull or edit changes the source. Settings must still apply
    (via PUT, file edit + authentication, and refresh); the stale code is reported separately."""
    def fingerprint():
        if source_unavailable:
            raise OSError('source unavailable')
        return 'new-runtime-source'
    monkeypatch.setattr(core, '_runtime_source_fingerprint', fingerprint)
    monkeypatch.setattr(core, '_SOURCE_CHECK', {'stat': ('force a recheck',), 'changed': False})
    r = senv.put({'tasks.require_human_accept': True})
    assert r.status_code == 200, r.text
    assert senv.board.s.require_human_accept is True and r.json()['changed'] == ['tasks.require_human_accept']
    senv.local.write_text(senv.local.read_text().replace('[limits]', '[limits]\ndaily_post_cap_per_agent = 1000', 1))
    touch_later(senv.local)
    status = senv.client.get('/api/configuration', headers=senv.h('codex')).json()   # authentication reloads
    assert status['effective_limits']['daily_post_cap_per_agent'] == 1000
    assert status['runtime_source_changed'] is True and status['refresh_supported'] is True
    assert 'restart' in status['recovery']
    result = senv.client.post('/api/configuration/refresh', headers=senv.h()).json()
    assert result['applied'] is True and result['runtime_source_changed'] is True


def test_dispatcher_applies_dispatch_changes_after_a_source_change(senv, monkeypatch):
    d = Dispatcher(senv.board, senv.p['human'], DispatchConfig.from_dict(senv.board.s.dispatch))
    monkeypatch.setattr(core, '_runtime_source_fingerprint', lambda: 'new-runtime-source')
    monkeypatch.setattr(core, '_SOURCE_CHECK', {'stat': ('force a recheck',), 'changed': False})
    assert senv.put({'dispatch.max_concurrent': 3}).status_code == 200
    d.refresh_config()
    assert d.config.max_concurrent == 3


def test_settings_put_reports_a_setting_it_could_not_apply(senv, monkeypatch):
    monkeypatch.setattr(senv.board, 'reload_settings', lambda force=False: False)
    senv.board.settings_error = 'ValueError: simulated'
    r = senv.put({'tasks.require_human_accept': True})
    assert r.status_code == 409 and 'could not apply' in r.json()['message']


def test_source_change_check_rehashes_only_when_a_file_changes(monkeypatch):
    calls = []
    real = core._runtime_source_fingerprint
    monkeypatch.setattr(core, '_runtime_source_fingerprint', lambda: calls.append(1) or real())
    for _ in range(5):
        assert core._runtime_source_changed() is False
    assert calls == []


# ---------------------------------------------------------------- configuration errors never leak to agents


def test_configuration_error_text_is_generic_for_agents_and_redacted_for_the_human(senv):
    senv.local.write_text('[dispatch.runners]\ncodex = ["codex", "--api-key=sk-SECRET-{x}", "{prompt}"]\n')
    touch_later(senv.local)
    texts = [senv.client.get('/api/configuration', headers=senv.h('codex')).text,
             senv.client.get('/api/whoami', headers=senv.h('codex')).text,
             senv.client.get('/api/state', headers=senv.h('codex')).text,
             json.dumps(senv.board.register_session(senv.p['codex'], PROJECT)),
             json.dumps(senv.board.read_updates(senv.p['codex'], senv.sid['codex']))]
    for text in texts:
        assert 'SECRET' not in text and 'api-key' not in text
    assert core.CONFIG_ERROR_FOR_AGENTS in texts[0]
    human = senv.client.get('/api/configuration', headers=senv.h()).json()['error']
    assert human and 'dispatch.runners' in human and 'SECRET' not in human
    assert 'SECRET' not in json.dumps(senv.get().json())


@pytest.mark.parametrize('text,leak', [('--api-key=sk-SECRET-1', 'SECRET'), ('run --token hunter2', 'hunter2'),
                                       ('Authorization: Bearer abc.def', 'abc.def'), ('password=hunter2', 'hunter2'),
                                       ('ghp_1234567890abcdef', '1234567890'), ('token = "hunter2"', 'hunter2'),
                                       ("{'KEY': 'hunter2'}", 'hunter2'), ('["codex", "--api-key","x"]', '"x"'),
                                       ("['codex', '--token', 'hunter2']", 'hunter2'), ('api_key: "a b c"', 'a b c'),
                                       ('"password": "p\\"q"', 'q')])
def test_redact_secrets(text, leak):
    assert leak not in core.redact_secrets(text)
    assert core.redact_secrets('unknown setting [dispatch] unknown_future_option') == \
        'unknown setting [dispatch] unknown_future_option'
