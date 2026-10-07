"""Dashboard sign-in: login links, cookie sessions, CSRF, lifetimes, revocation, and `board dashboard`."""

import asyncio
import importlib.util
import json
import re
import urllib.error
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_comms import board_settings, cli, weblogin
from agent_comms.api import create_app
from agent_comms.config import Settings, create_agent, hash_token
from agent_comms.core import Board, Unauthorized

from conftest import FakeClock, make_env

DAY = 86400
COOKIE = "agent_comms_session_8787"   # TestClient's Host has no port, so the board's configured port names it
URL_SHAPE = re.compile(r"http://127\.0\.0\.1:(\d+)/login/([A-Za-z0-9_-]{8,256})")
SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.0 Safari/605.1.15")
DASH = {"X-Board-Request": "1"}       # what the dashboard sends with every request


class W:
    def __init__(self, env):
        self.env, self.board, self.clock = env, env.board, env.clock
        self.app = create_app(env.board)

    def client(self, **kw) -> TestClient:
        return TestClient(self.app, **kw)

    def h(self, who="human"):
        return {"Authorization": f"Bearer {self.env.tokens[who]}"}

    def link(self, next_path="/", who="human", client=None):
        r = (client or self.client()).post("/api/login-links", json={"next": next_path}, headers=self.h(who))
        return r

    def code(self, next_path="/"):
        r = self.link(next_path)
        assert r.status_code == 200, r.text
        return URL_SHAPE.fullmatch(r.json()["url"]).group(2)

    def signed_in(self, next_path="/", user_agent=SAFARI) -> TestClient:
        c = self.client(headers={"User-Agent": user_agent})
        r = c.get(f"/login/{self.code(next_path)}", follow_redirects=False)
        assert r.status_code == 303, r.text
        assert c.cookies.get(COOKIE)
        return c


@pytest.fixture
def w(tmp_path):
    return W(make_env(tmp_path))


def state_rows(board, prefix):
    return {r["key"]: r["value"] for r in board.conn.execute(
        "SELECT key, value FROM board_state WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))}


# ---------------------------------------------------------------- login links


def test_login_link_is_human_only_and_has_the_contract_shape(w):
    assert w.link(who="codex").status_code == 403
    assert w.link(who="claude").status_code == 403
    assert w.client().post("/api/login-links", json={"next": "/"}).status_code == 401
    r = w.link("/#post-41")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"url", "expires_in_seconds"} and body["expires_in_seconds"] == 60
    m = URL_SHAPE.fullmatch(body["url"])
    assert m and m.group(1) == "8787", body["url"]
    assert "?" not in body["url"] and "#" not in body["url"] and w.env.tokens["human"] not in body["url"]
    assert len(m.group(2)) >= 43   # token_urlsafe(32): 32 random bytes
    # no body at all means next = "/"; the port follows the Host the client used
    r = w.client().post("/api/login-links", headers=w.h() | {"Host": "127.0.0.1:9999"})
    assert URL_SHAPE.fullmatch(r.json()["url"]).group(1) == "9999"
    # unknown fields are refused like everywhere else in the API
    assert w.client().post("/api/login-links", json={"next": "/", "agent": "x"}, headers=w.h()).status_code == 422


def test_a_cookie_session_cannot_mint_login_links(w):
    c = w.signed_in()
    r = c.post("/api/login-links", json={"next": "/"}, headers=DASH)
    assert r.status_code == 403
    assert "bearer" in r.json()["message"]


@pytest.mark.parametrize("bad", [
    "//evil.example", "//evil.example/#post-1", "/\\evil.example", "\\\\evil.example", "https://evil.example/",
    "http:/evil.example", "javascript:alert(1)", "evil.example", "", " /", "/\t/evil.example", "/\n/evil.example",
    "/\r\nSet-Cookie: x=y", "/ spaced", "/café", "/\x00", "/" + "a" * 3000, "#post-1", "?x=1",
])
def test_next_must_be_a_same_origin_path(w, bad):
    r = w.link(bad)
    assert r.status_code == 400, (bad, r.text)
    assert not state_rows(w.board, weblogin.LINK_PREFIX)


@pytest.mark.parametrize("bad", [5, None, ["/"], {"a": 1}])
def test_next_must_be_a_string(w, bad):
    assert w.client().post("/api/login-links", json={"next": bad}, headers=w.h()).status_code == 422


@pytest.mark.parametrize("good", ["/", "/#post-41", "/#thread-3", "/#settings", "/?closed=true#post-2", "/api/docs",
                                  "/%2F%2Fevil.example"])
def test_redirect_goes_to_the_stored_next_with_its_fragment(w, good):
    code = w.code(good)
    # a next in the GET is ignored: only the value stored with the code counts
    r = w.client().get(f"/login/{code}?next=//evil.example", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == good
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


def test_code_is_single_use_expires_and_is_stored_hashed(w):
    code = w.code()
    rows = state_rows(w.board, weblogin.LINK_PREFIX)
    assert list(rows) == [weblogin.LINK_PREFIX + hash_token(code)]
    dump = json.dumps([dict(r) for r in w.board.conn.execute("SELECT * FROM board_state")])
    assert code not in dump and w.env.tokens["human"] not in dump
    c = w.client()
    assert c.get(f"/login/{code}", follow_redirects=False).status_code == 303
    again = w.client().get(f"/login/{code}", follow_redirects=False)
    assert again.status_code == 410
    assert "Link expired" in again.text and "board dashboard" in again.text
    assert "set-cookie" not in again.headers
    assert not state_rows(w.board, weblogin.LINK_PREFIX)

    code = w.code()
    w.clock.advance(59)
    assert w.client().get(f"/login/{code}", follow_redirects=False).status_code == 303
    code = w.code()
    w.clock.advance(60)
    assert w.client().get(f"/login/{code}", follow_redirects=False).status_code == 410
    assert not state_rows(w.board, weblogin.LINK_PREFIX)   # consumed even though it had expired

    for junk in ["x", "A" * 43, "A.B", "A" * 300]:
        r = w.client().get(f"/login/{junk}", follow_redirects=False)
        assert r.status_code in (404, 410) and "set-cookie" not in r.headers


def test_rotating_the_human_token_invalidates_pending_links(w):
    code = w.code()
    create_agent(w.env.settings.agents_path, "human", "human", is_human=True, rotate=True)
    assert w.client().get(f"/login/{code}", follow_redirects=False).status_code == 410


def test_pending_links_are_capped(w):
    codes = [w.code() for _ in range(weblogin.MAX_PENDING_LINKS + 5)]
    assert len(state_rows(w.board, weblogin.LINK_PREFIX)) == weblogin.MAX_PENDING_LINKS
    assert w.client().get(f"/login/{codes[-1]}", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- the cookie


def test_cookie_attributes_and_session_storage(w):
    c = w.client(headers={"User-Agent": SAFARI})
    r = c.get(f"/login/{w.code('/#post-41')}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#post-41"
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 1
    parts = [p.strip() for p in cookies[0].split(";")]
    name, _, secret = parts[0].partition("=")
    attrs = {p.split("=")[0].lower(): (p.split("=", 1)[1] if "=" in p else True) for p in parts[1:]}
    assert name == COOKIE and len(secret) >= 43
    assert attrs["httponly"] is True
    assert attrs["samesite"].lower() == "strict"
    assert attrs["path"] == "/"
    assert int(attrs["max-age"]) == 30 * DAY
    assert "secure" not in attrs and "domain" not in attrs   # see DESIGN_NOTES: Safari drops Secure on http
    rows = state_rows(w.board, weblogin.SESSION_PREFIX)
    assert list(rows) == [weblogin.SESSION_PREFIX + hash_token(secret)]
    rec = json.loads(next(iter(rows.values())))
    assert rec["label"] == "Safari on macOS" and rec["agent"] == "human"
    dump = json.dumps([dict(r) for r in w.board.conn.execute("SELECT * FROM board_state")])
    assert secret not in dump
    me = c.get("/api/whoami", headers=DASH)
    assert me.status_code == 200 and me.json()["name"] == "human" and me.json()["is_human"] is True


@pytest.mark.parametrize("ua,label", [
    (SAFARI, "Safari on macOS"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 "
     "Safari/537.36", "Chrome on macOS"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Claude/2.1 "
     "Chrome/152.0 Safari/537.36", "Claude app browser on macOS"),
    ("Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/130.0 Electron/33.0 Safari/537.36",
     "Embedded browser on macOS"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", "Firefox on Linux"),
    ("<img src=x onerror=alert(1)>", "Browser"), (None, "Browser"),
])
def test_browser_label_is_from_a_fixed_vocabulary(ua, label):
    assert weblogin.browser_label(ua) == label


# ---------------------------------------------------------------- lifetime


def test_session_slides_at_most_hourly_and_ends_after_idle_or_absolute_limit(w):
    c = w.signed_in()
    key = next(iter(state_rows(w.board, weblogin.SESSION_PREFIX)))
    written = lambda: w.board.conn.execute("SELECT updated_at FROM board_state WHERE key = ?", (key,)).fetchone()[0]
    t0 = written()
    w.clock.advance(1800)
    r = c.get("/api/whoami", headers=DASH)
    assert r.status_code == 200 and "set-cookie" not in r.headers and written() == t0   # no write within the hour
    # every 20 days of use renews it, until the 90-day absolute limit
    for day in (20, 40, 60, 80):
        w.clock.t = t0 + day * DAY
        r = c.get("/api/whoami", headers=DASH)
        assert r.status_code == 200, day
        max_age = int(re.search(r"Max-Age=(\d+)", r.headers["set-cookie"]).group(1))
        assert max_age == min(30 * DAY, (90 - day) * DAY)
        assert written() == w.clock.t
    w.clock.t = t0 + 90 * DAY - 1
    assert c.get("/api/whoami", headers=DASH).status_code == 200
    w.clock.t = t0 + 90 * DAY
    r = c.get("/api/whoami", headers=DASH)
    assert r.status_code == 401
    assert not state_rows(w.board, weblogin.SESSION_PREFIX)   # deleted when found expired

    c = w.signed_in()
    w.clock.advance(30 * DAY - 1)
    assert c.get("/api/whoami", headers=DASH).status_code == 200
    w.clock.advance(30 * DAY)                                  # 30 days unused since that renewal
    assert c.get("/api/whoami", headers=DASH).status_code == 401


def test_lifetimes_come_from_web_settings(tmp_path):
    w = W(make_env(tmp_path, session_days=1, session_max_days=2))
    c = w.signed_in()
    assert c.get("/api/whoami", headers=DASH).status_code == 200
    w.clock.advance(DAY - 1)
    assert c.get("/api/whoami", headers=DASH).status_code == 200
    w.clock.advance(DAY - 1)
    assert c.get("/api/whoami", headers=DASH).status_code == 200
    w.clock.advance(2)                                          # 2 days after sign-in: absolute limit
    assert c.get("/api/whoami", headers=DASH).status_code == 401
    # a shorter limit applies to sessions that already exist
    c = w.signed_in()
    w.board.s.session_max_days = 1
    w.board.s.session_days = 1
    w.clock.advance(DAY)
    assert c.get("/api/whoami", headers=DASH).status_code == 401


# ---------------------------------------------------------------- CSRF


def test_cookie_requests_need_the_custom_header_and_a_matching_origin(w):
    c = w.signed_in()
    assert c.post("/api/admin/pause").status_code == 403                      # no X-Board-Request
    assert c.post("/api/admin/pause", headers={"X-Board-Request": "yes"}).status_code == 403
    assert c.get("/api/state").status_code == 403                             # reads too
    for origin in ("http://evil.example", "http://127.0.0.1:3000", "null", "https://testserver", "http://localhost"):
        r = c.post("/api/admin/pause", headers=DASH | {"Origin": origin})
        assert r.status_code == 403, origin
    assert not w.board.is_paused()
    assert c.post("/api/admin/pause", headers=DASH | {"Origin": "http://testserver"}).status_code == 200
    assert w.board.is_paused()
    assert c.post("/api/admin/unpause", headers=DASH).status_code == 200      # no Origin: header suffices
    assert not w.board.is_paused()
    # 127.0.0.1:8787 is its own origin
    r = c.post("/api/admin/pause", headers=DASH | {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"})
    # (the cookie is named for the Host's port, 8787, so it is still sent and accepted)
    assert r.status_code == 200


def test_bearer_requests_are_unaffected_and_win_over_a_cookie(w):
    c = w.signed_in()
    plain = w.client()
    assert plain.post("/api/admin/pause", headers=w.h() | {"Origin": "http://evil.example"}).status_code == 200
    plain.post("/api/admin/unpause", headers=w.h())
    # an agent bearer alongside the human's cookie acts as the agent, never the human
    r = c.post("/api/admin/pause", headers=w.h("codex") | DASH)
    assert r.status_code == 403 and not w.board.is_paused()
    assert c.get("/api/whoami", headers=w.h("codex")).json()["name"] == "codex"
    # a bad bearer is a 401 even with a valid cookie
    assert c.get("/api/whoami", headers={"Authorization": "Bearer nope"} | DASH).status_code == 401


def test_cookie_auth_never_reaches_mcp(w):
    c = w.signed_in()
    secret = c.cookies.get(COOKIE)
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    app = w.app

    async def go():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
            headers = {"Cookie": f"{COOKIE}={secret}", "X-Board-Request": "1", "Origin": "http://127.0.0.1:8787"}
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787",
                                         headers=headers) as http:
                async with Client(streamable_http_client("http://127.0.0.1:8787/mcp", http_client=http)) as mc:
                    return await mc.call_tool("board_register", {"project": "/p"})

    result = asyncio.run(go())
    assert result.is_error
    assert "unauthorized" in result.content[0].text


def test_cookie_auth_never_reaches_the_chatgpt_gateway(w):
    spec = importlib.util.spec_from_file_location(
        "gateway_for_web_login", Path(__file__).parents[1] / "integrations/chatgpt/gateway.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"name": "chatgpt", "is_human": False})

    for mode, path in (("mcp", "/mcp"), ("actions", "/api/posts")):
        g = TestClient(gateway.create_app("test-chatgpt", mode, httpx.MockTransport(upstream)))
        r = g.post(path, headers={"Cookie": f"{COOKIE}=whatever"} | DASH)
        assert r.status_code == 401
    assert not seen


# ---------------------------------------------------------------- revocation


def test_list_revoke_sign_out_all_and_logout(w):
    a = w.signed_in(user_agent=SAFARI)
    b = w.signed_in(user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0")
    # agents can neither list nor revoke
    for method, path in (("GET", "/api/web-sessions"), ("POST", "/api/web-sessions/revoke-all"),
                         ("POST", "/api/web-sessions/0123456789abcdef/revoke")):
        assert w.client().request(method, path, headers=w.h("codex")).status_code == 403
    listed = a.get("/api/web-sessions", headers=DASH).json()
    assert listed["session_days"] == 30 and listed["session_max_days"] == 90
    sessions = {s["label"]: s for s in listed["sessions"]}
    assert set(sessions) == {"Safari on macOS", "Firefox on Linux"}
    assert sessions["Safari on macOS"]["current"] and not sessions["Firefox on Linux"]["current"]
    assert set(sessions["Safari on macOS"]) == {"id", "label", "created_at", "last_seen", "expires_at", "current"}
    assert not any(s["current"] for s in w.client().get("/api/web-sessions", headers=w.h()).json()["sessions"])

    assert a.post(f"/api/web-sessions/{sessions['Firefox on Linux']['id']}/revoke", headers=DASH).status_code == 200
    assert b.get("/api/whoami", headers=DASH).status_code == 401
    assert a.get("/api/whoami", headers=DASH).status_code == 200
    assert a.post("/api/web-sessions/ffffffffffffffff/revoke", headers=DASH).status_code == 404
    assert a.post("/api/web-sessions/'%20OR%201=1/revoke", headers=DASH).status_code == 404

    c = w.signed_in()
    r = a.post("/api/web-sessions/revoke-all", headers=DASH)
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    assert "Max-Age=0" in r.headers["set-cookie"] or "expires=" in r.headers["set-cookie"].lower()
    assert c.get("/api/whoami", headers=DASH).status_code == 401
    assert not state_rows(w.board, weblogin.SESSION_PREFIX)

    d = w.signed_in()
    assert d.post("/api/web-sessions/logout").status_code == 403              # CSRF header required
    r = d.post("/api/web-sessions/logout", headers=DASH)
    assert r.status_code == 200 and r.json() == {"signed_out": True}
    assert not d.cookies.get(COOKIE)
    assert not state_rows(w.board, weblogin.SESSION_PREFIX)
    assert w.client().post("/api/web-sessions/logout", headers=DASH).json() == {"signed_out": False}


@pytest.mark.parametrize("path", ["/api/summary", "/api/needs-you"])
def test_menu_bar_routes_accept_the_human_cookie_with_the_csrf_header(w, path):
    c = w.signed_in()
    assert c.get(path, headers=DASH).status_code == 200
    assert c.get(path).status_code == 403                                     # no X-Board-Request
    assert c.get(path, headers=DASH | {"Origin": "http://evil.example"}).status_code == 403
    assert w.client().get(path, headers=w.h()).status_code == 200             # the menu bar's bearer is unchanged
    assert w.client().get(path, headers=w.h("codex")).status_code == 403


def test_rotating_or_revoking_the_human_token_ends_web_sessions(w):
    c = w.signed_in()
    create_agent(w.env.settings.agents_path, "human", "human", is_human=True, rotate=True)
    assert c.get("/api/whoami", headers=DASH).status_code == 401


def test_sessions_are_capped(w):
    secrets = []
    for i in range(weblogin.MAX_SESSIONS + 3):
        w.clock.advance(1)
        secrets.append(weblogin.redeem_link(w.board, weblogin.create_link(w.board, w.env.p["human"]), "x")[0])
    assert len(state_rows(w.board, weblogin.SESSION_PREFIX)) == weblogin.MAX_SESSIONS
    with pytest.raises(Unauthorized):
        weblogin.authenticate_session(w.board, secrets[0])
    assert weblogin.authenticate_session(w.board, secrets[-1])[0].name == "human"


# ---------------------------------------------------------------- settings


LOCAL_WEB = "[web]\nsession_days = 7\n"


def test_web_settings_load_overlay_reload_and_are_not_editable_on_the_settings_page(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    (home / "board.toml").write_text("[web]\nsession_days = 30\nsession_max_days = 90\n")
    (home / "board.local.toml").write_text(LOCAL_WEB)
    s = Settings.load()
    assert (s.session_days, s.session_max_days) == (7, 90)
    assert not any(k.startswith("web.") for k in board_settings.EDITABLE)
    token = create_agent(s.agents_path, "human", "human", is_human=True)
    board = Board(s, clock=FakeClock())
    client = TestClient(create_app(board))
    before = (home / "board.local.toml").read_bytes()
    for key in ("web.session_days", "web.session_max_days", "session_days", "limits.session_days"):
        r = client.put("/api/settings", json={key: 365}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, key
        assert "not editable from the dashboard" in r.json()["message"]
    assert (home / "board.local.toml").read_bytes() == before
    keys = {x["key"] for x in client.get("/api/settings", headers={"Authorization": f"Bearer {token}"}).json()["settings"]}
    assert keys == set(board_settings.EDITABLE)
    # hand edits hot-reload; invalid ones keep the last good values
    (home / "board.local.toml").write_text("[web]\nsession_days = 2\nsession_max_days = 3\n")
    assert board.reload_settings(force=True) and (board.s.session_days, board.s.session_max_days) == (2, 3)
    for bad in ("[web]\nsession_days = 0\n", "[web]\nsession_days = 10\nsession_max_days = 5\n",
                "[web]\nsession_days = true\n", "[web]\nunknown = 1\n"):
        (home / "board.local.toml").write_text(bad)
        assert not board.reload_settings(force=True), bad
        assert (board.s.session_days, board.s.session_max_days) == (2, 3)


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    s = Settings.load()
    token = create_agent(s.agents_path, "human", "human", is_human=True)
    create_agent(s.agents_path, "codex", "codex-cli")
    monkeypatch.setenv("BOARD_TOKEN", token)
    return s, token


def test_board_dashboard_opens_a_login_link_never_the_token(cli_home, monkeypatch, capsys):
    _, token = cli_home
    board = Board(Settings.load())
    server = TestClient(create_app(board))
    requests, opened = [], []

    class Reply:
        def __init__(self, r):
            self.r = r

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.r.content

    def urlopen(req, timeout=None):
        requests.append(req)
        path = req.full_url.removeprefix("http://127.0.0.1:8787")
        r = server.request(req.get_method(), path, content=req.data, headers=dict(req.header_items()))
        return Reply(r)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    cli.main(["dashboard"])
    assert len(requests) == 1
    req = requests[0]
    assert req.full_url == "http://127.0.0.1:8787/api/login-links" and req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {token}"
    assert len(opened) == 1 and URL_SHAPE.fullmatch(opened[0])
    assert token not in opened[0] and token not in req.full_url
    out = capsys.readouterr().out
    assert token not in out
    # the link works once
    path = opened[0].removeprefix("http://127.0.0.1:8787")
    assert server.get(path, follow_redirects=False).status_code == 303
    assert server.get(path, follow_redirects=False).status_code == 410


def test_board_dashboard_says_how_to_start_the_server(cli_home, monkeypatch):
    opened = []

    def refused(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr("urllib.request.urlopen", refused)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    with pytest.raises(SystemExit) as e:
        cli.main(["dashboard"])
    assert "not running" in str(e.value) and "board serve" in str(e.value)
    assert not opened


def test_board_dashboard_rejects_an_unexpected_link(cli_home, monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"url": "https://evil.example/login/abc", "expires_in_seconds": 60}).encode()

    opened = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: Reply())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    with pytest.raises(SystemExit):
        cli.main(["dashboard"])
    assert not opened


def test_board_logout_all_revokes_every_web_session(cli_home, capsys):
    _, token = cli_home
    board = Board(Settings.load())
    human = board.authenticate(token)
    secrets = [weblogin.redeem_link(board, weblogin.create_link(board, human), "x")[0] for _ in range(3)]
    with pytest.raises(SystemExit):
        cli.main(["logout"])
    assert weblogin.authenticate_session(board, secrets[0])[0].is_human
    cli.main(["logout", "--all"])
    assert "signed out 3 browser(s)" in capsys.readouterr().out
    for secret in secrets:
        with pytest.raises(Unauthorized):
            weblogin.authenticate_session(board, secret)
