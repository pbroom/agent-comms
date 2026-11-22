"""The hub's long-poll routes stop when their caller hangs up (DESIGN_NOTES "Long-poll disconnects", 2026-11-21).

The cause was the app's `@app.middleware("http")` (Starlette's BaseHTTPMiddleware): behind it a route's
`request.is_disconnected()` never turned true. api.LocalhostOnly is the same check as plain ASGI. Here:
- each long-poll route, driven straight through the ASGI app with an `http.disconnect` delivered mid-wait, and once
  through a real uvicorn server (and once through the gateway) with the client's socket closed: no look after;
- a wake that comes due after the caller hung up is not handed to the dead call, and the next call gets it;
- the localhost check answers every case exactly as the old middleware did (a copy of it, run as it was).
Fake clock for the board, real time for the waits (hosted._poll's rule)."""

import asyncio
import ipaddress
import itertools
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from agent_comms import api, gateway, hosted, interactive_wake as iw
from agent_comms.api import create_app

from conftest import PROJECT
from test_hosted_sessions import client, senv  # noqa: F401
from test_hosted_wake import ask, ready, text_for, wakes
from test_interactive_wake import NA, ONE, on, rec, wait as wake
from test_remote import Served, free_port


class Call:
    """One HTTP request driven straight into an ASGI app, as a server drives it. Its first receive() is the whole
    body; the next one waits until hang_up() and then says `http.disconnect`, as uvicorn's does once the client
    has closed its socket (and, as uvicorn's, it answers at once without waiting when that has happened)."""

    def __init__(self, app, method="GET", path="/", query="", body=None, token=None, client=("127.0.0.1", 50123),
                 host="127.0.0.1:8787", headers=(), scope_type="http", receive_fails=False, send_fails=False,
                 strict_receive=False):
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        hdrs = [] if host is None else [(b"host", host.encode("latin-1"))]
        if token:
            hdrs.append((b"authorization", b"Bearer " + token.encode()))
        if body is not None:
            hdrs += [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
        hdrs += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        self.scope = {"type": scope_type, "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                      "method": method, "scheme": "http" if scope_type == "http" else "ws", "path": path,
                      "raw_path": path.encode(), "query_string": query.encode(), "root_path": "", "headers": hdrs,
                      "client": client, "server": ("127.0.0.1", 8787), "state": {}}
        if scope_type == "websocket":
            del self.scope["method"]
        self.app, self.body, self.started, self.messages = app, raw, False, []
        self.receive_fails, self.send_fails = receive_fails, send_fails     # after the body / at the response start
        self.strict_receive, self.reading = strict_receive, False         # strict: overlapping reads are refused
        self.gone = asyncio.Event()
        self.hung_up_at = self.ended_at = None

    async def receive(self):
        if self.strict_receive:
            if self.reading:
                raise RuntimeError("receive() called while another receive() is still waiting")
            self.reading = True
            try:
                return await self._receive()
            finally:
                self.reading = False
        return await self._receive()

    async def _receive(self):
        if self.scope["type"] == "websocket":
            if not self.started:
                self.started = True
                return {"type": "websocket.connect"}
            await self.gone.wait()
            return {"type": "websocket.disconnect", "code": 1000}
        if not self.started:
            self.started = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        if self.receive_fails:
            self.receive_fails = False      # once: the next receive works again
            raise RuntimeError("the server's receive failed")
        if not self.gone.is_set():
            await self.gone.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        if self.send_fails and message["type"] == "http.response.start":
            raise OSError("the client's connection is gone")
        self.messages.append(message)

    async def run(self):
        try:
            await self.app(self.scope, self.receive, self.send)
        finally:
            self.ended_at = time.monotonic()

    def hang_up(self):
        self.hung_up_at = time.monotonic()
        self.gone.set()

    def response(self):
        """(status, headers, body) of an HTTP response however its body was chunked; a websocket's messages as sent."""
        if self.scope["type"] == "websocket":
            return self.messages
        [start] = [m for m in self.messages if m["type"] == "http.response.start"]
        body = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        return start["status"], [tuple(h) for h in start.get("headers", [])], body


def count_looks(monkeypatch, module, name) -> list:
    """Wrap a look function: each call's (start, end) in real time."""
    real, looks = getattr(module, name), []

    def look(*a, **kw):
        start = time.monotonic()
        try:
            return real(*a, **kw)
        finally:
            looks.append((start, time.monotonic()))
    monkeypatch.setattr(module, name, look)
    return looks


def hang_up_mid_wait(app, looks, after=lambda: None, settle=2.5, **request) -> Call:
    """Start the call, hang up as soon as its first look has returned (it then sleeps until its next look), run
    `after` (a wake comes due), and give the call `settle` seconds to end. A call still open then is cancelled."""
    async def main():
        call = Call(app, **request)
        task = asyncio.ensure_future(call.run())
        for _ in range(500):
            if looks or task.done():
                break
            await asyncio.sleep(0.01)
        assert looks and not task.done(), "the call is waiting after its first look"
        call.hang_up()
        after()
        await asyncio.wait({task}, timeout=settle)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            call.still_open = True
        else:
            call.still_open = False
            task.result()
        return call
    return asyncio.run(main())


def looks_after(looks, at) -> list:
    return [s for s, _ in looks if s >= at]


# ------------------------------------------------------------ /api/nodes/sessions


def node_version(env, tree=1) -> str:
    return env.laptop.get("/api/nodes/sessions", params={"tree": tree}).json()["version"]


def test_a_node_that_hangs_up_mid_wait_gets_no_more_looks_and_no_wake(senv):
    """Before: the dead fetch went on looking four times a second for the rest of its wait, and a wake that came due
    meanwhile was handed (`handed_at`) into it. Now the call ends at its next look, and the wake waits for the node's
    next fetch, whose hand-over starts its grace."""
    sid = ready(senv)
    tid = senv.thread()
    seen = node_version(senv)
    with pytest.MonkeyPatch.context() as mp:
        looks = count_looks(mp, hosted, "node_sessions")
        call = hang_up_mid_wait(senv.app, looks, after=lambda: ask(senv, tid), settle=1.5,
                                path="/api/nodes/sessions", query=f"wait=10&seen={seen}&tree=1",
                                token=senv.tokens["laptop"])
    assert not call.still_open, "the call ended once its caller had gone"
    assert call.ended_at - call.hung_up_at < hosted.WAIT_POLL_SECONDS + 0.5
    assert looks_after(looks, call.hung_up_at) == [], "no look for a caller that has gone"
    assert wakes(senv, sid) == [], "the wake that came due was not queued and handed into the dead call"
    senv.clock.advance(5)
    listed = senv.laptop.get("/api/nodes/sessions", params={"tree": 1}).json()
    [entry] = [s for s in listed["sessions"] if s["session_id"] == sid]
    assert [m["kind"] for m in entry["messages"]] == ["wake"], "the node's next fetch gets it"
    [m] = wakes(senv, sid)
    [stored] = [x for x in hosted._messages(senv.board.conn, sid) if x["n"] == m["n"]]
    assert stored["handed_at"] == senv.clock(), "handed now, to a node that is there, so its grace starts now"
    assert entry["messages"][0]["text"] == text_for(senv, 1, [tid])


# ------------------------------------------------------------ /api/wait


def test_a_wait_whose_caller_hung_up_makes_no_offer_and_the_next_call_gets_the_wake(senv):
    """Before: the dead call went on heartbeating for the rest of its wait and a post that arrived meanwhile was
    offered into it (taken back only ACK_SECONDS later, unacknowledged). Now it ends at its next look."""
    on(senv)
    tid = senv.thread()
    assert wake(senv) == {"state": "idle"}
    seen_before = rec(senv)["seen"]
    body = {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA, "wait": 10}
    with pytest.MonkeyPatch.context() as mp:
        looks = count_looks(mp, iw, "poll")
        call = hang_up_mid_wait(senv.app, looks, after=lambda: (senv.clock.advance(1), ask(senv, tid)), settle=2.5,
                                method="POST", path="/api/wait", body=body, token=senv.tokens["claude"])
    assert not call.still_open, "the call ended once its caller had gone"
    assert call.ended_at - call.hung_up_at < iw.POLL_SECONDS + 0.5
    assert looks_after(looks, call.hung_up_at) == [], "no look (and no heartbeat) for a caller that has gone"
    r = rec(senv)
    assert r["offer"] is None and r["delivered"] == {}, "nothing was offered into the dead call"
    assert r["seen"] == seen_before, "the record is as the last look before the hang-up left it"
    assert wake(senv) == {"state": "wake", "text": text_for(senv, 1, [tid])}, "the next call gets it"


# ------------------------------------------------------------ through a real server, and through the gateway


def _open(port, request: bytes) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(request)
    return s


def _until(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    assert cond()


def _requests(senv, port, seen):
    body = json.dumps({"client_session_id": ONE, "projects": [PROJECT], "nonce": NA, "wait": 10}).encode()
    host = f"Host: 127.0.0.1:{port}\r\n".encode()
    nodes = (b"GET /api/nodes/sessions?wait=10&seen=" + seen.encode() + b"&tree=1 HTTP/1.1\r\n" + host
             + b"Authorization: Bearer " + senv.tokens["laptop"].encode() + b"\r\n\r\n")
    waits = (b"POST /api/wait HTTP/1.1\r\n" + host + b"Authorization: Bearer " + senv.tokens["claude"].encode()
             + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    return nodes, waits


def _closed_socket_ends_both_waits(senv, port):
    on(senv)
    ready(senv)
    wake(senv)
    nodes, waits = _requests(senv, port, node_version(senv))
    for module, name, request, quiet in ((hosted, "node_sessions", nodes, 1.0), (iw, "poll", waits, 2.0)):
        with pytest.MonkeyPatch.context() as mp:
            looks = count_looks(mp, module, name)
            s = _open(port, request)
            _until(lambda: looks)
            s.close()
            closed = time.monotonic()
            time.sleep(quiet)
        assert looks_after(looks, closed) == [], f"{name}: no look after the caller closed its socket"


def test_a_closed_socket_ends_both_long_polls_on_a_real_server(senv):
    port = free_port()
    server = Served(lambda: create_app(senv.board), port).start()
    try:
        _closed_socket_ends_both_waits(senv, port)
    finally:
        server.stop()


@contextmanager
def through_gateway(senv):
    """The board on one port and the gateway in front of it on another, as on the server Mac: the gateway's port."""
    board_port, gateway_port = free_port(), free_port()
    board = Served(lambda: create_app(senv.board), board_port).start()
    front = Served(lambda: gateway.create_app(f"http://127.0.0.1:{board_port}"), gateway_port).start()
    try:
        yield gateway_port
    finally:
        front.stop()
        board.stop()


def test_a_caller_that_hangs_up_on_the_gateway_ends_the_hubs_long_poll(senv):
    """The gateway waits for the hub's response headers, which a long-poll sends only at its end: it now also
    watches its own caller, and drops the upstream request when that one hangs up, so the hub sees it too."""
    with through_gateway(senv) as port:
        _closed_socket_ends_both_waits(senv, port)


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    out = {}
    t = threading.Thread(target=lambda: out.update(r=fn()), daemon=True)
    t.start()
    return t, out


def test_a_node_that_stays_on_the_gateway_waits_and_gets_its_wake(senv):
    """A connected caller through the gateway's race: the race neither ends the wait early nor loses the answer."""
    sid = ready(senv)
    tid = senv.thread()
    seen = node_version(senv)
    with through_gateway(senv) as port:
        def fetch():
            return httpx.get(f"http://127.0.0.1:{port}/api/nodes/sessions", params={"wait": 10, "seen": seen,
                                                                                   "tree": 1},
                             headers={"Authorization": "Bearer " + senv.tokens["laptop"]}, timeout=15)
        t, out = _in_thread(fetch)
        time.sleep(1.0)
        assert t.is_alive(), "nothing new: it waits"
        ask(senv, tid)
        t.join(10)
    assert out["r"].status_code == 200
    [entry] = [s for s in out["r"].json()["sessions"] if s["session_id"] == sid]
    assert [m["kind"] for m in entry["messages"]] == ["wake"]


def test_a_wait_that_stays_on_the_gateway_waits_and_gets_its_wake(senv):
    on(senv)
    tid = senv.thread()
    wake(senv)
    body = {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA, "wait": 10}
    with through_gateway(senv) as port:
        def call():
            return httpx.post(f"http://127.0.0.1:{port}/api/wait", json=body, timeout=15,
                              headers={"Authorization": "Bearer " + senv.tokens["claude"]})
        t, out = _in_thread(call)
        time.sleep(1.5)
        assert t.is_alive(), "nothing new: it waits"
        ask(senv, tid)
        t.join(10)
    assert out["r"].status_code == 200
    got = out["r"].json()
    assert got["state"] == "wake" and got["text"] == text_for(senv, 1, [tid])


# ------------------------------------------------------------ the gateway's race, in memory


class Tracked(httpx.AsyncByteStream):
    """An upstream response body that records whether the gateway closed it."""

    def __init__(self, body: bytes):
        self.body, self.closed = body, False

    async def __aiter__(self):
        yield self.body

    async def aclose(self):
        self.closed = True


class Upstream(httpx.AsyncBaseTransport):
    """The board as the gateway sees it: /api/whoami names an agent; any other request is answered after `delay`
    seconds (and recorded in `answered`), by `forward` when given. `on_answer` runs just before an answer is
    returned."""

    def __init__(self, delay=0.3, forward=None):
        self.delay, self.forward, self.answered, self.streams = delay, forward, [], []
        self.on_answer = lambda: None

    async def handle_async_request(self, request):
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"name": "claude", "is_human": False})
        await asyncio.sleep(self.delay)
        body = await request.aread()
        self.answered.append((request.method, request.url.path, request.url.query.decode(), body))
        if self.forward is not None:
            return await self.forward.handle_async_request(request)
        stream = Tracked(b'{"state":"idle"}')
        self.streams.append(stream)
        self.on_answer()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)


AGENT = "x" * 24


def gateway_call(upstream, method, path, query="", body=None, hang_up=True, **kw) -> Call:
    """One request through an in-memory gateway whose caller hangs up as soon as the body is sent (`hang_up`)."""
    async def main():
        call = Call(gateway.create_app("http://board.test", transport=upstream), method=method, path=path,
                    query=query, body=body, token=AGENT, **kw)
        if hang_up:
            call.hang_up()
        call.error = None
        try:
            await call.run()
        except Exception as e:      # a send that fails (send_fails) fails the call, as with a real server
            call.error = e
        return call
    return asyncio.run(main())


ACK = {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA, "ack": "f" * 16, "wait": 0}
LONG = {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA, "wait": 10}


@pytest.mark.parametrize("method,path,query,body", [
    ("POST", "/api/wait", "", ACK),                                  # an acknowledgement, as `board wait` sends it
    ("POST", "/api/wait", "", ACK | {"wait": 10}),                   # an ack never long-polls, whatever its wait
    ("POST", "/api/wait", "", LONG | {"ack": None, "wait": 0}),      # wait 0: answered at once
    ("POST", "/api/wait", "", {k: v for k, v in LONG.items() if k != "wait"}),
    ("POST", "/api/wait", "", LONG | {"wait": "abc"}),               # any other shape goes the old way
    ("POST", "/api/wait", "", LONG | {"wait": "true"}),
    ("POST", "/api/wait", "", [LONG]),
    ("POST", "/api/wait", "", b"{not json"),
    ("GET", "/api/nodes/sessions", "", None),
    ("GET", "/api/nodes/sessions", "wait=0&tree=1", None),
    ("GET", "/api/nodes/sessions", "wait=abc", None),
    ("GET", "/api/nodes/sessions", "wait=nan", None),
    ("GET", "/api/nodes/sessions", "wait=10&tree=1", None),          # no `seen`: the board answers at once
])
def test_a_request_that_cannot_long_poll_reaches_the_board_even_when_its_caller_hangs_up(method, path, query, body):
    """Review round 1, S1: only a request that can wait is raced against its caller. Before, an acknowledgement
    whose caller hung up was cancelled on the way, and the same posts could be offered again."""
    upstream = Upstream()
    gateway_call(upstream, method, path, query, body)
    raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    assert upstream.answered == [(method, path, query, raw)], "forwarded to its end, as before the race"


@pytest.mark.parametrize("method,path,query,body", [
    ("POST", "/api/wait", "", LONG),
    ("POST", "/api/wait", "", LONG | {"ack": None}),
    ("GET", "/api/nodes/sessions", "wait=10&seen=ab&tree=1", None),
    ("GET", "/api/nodes/sessions", "wait=0.5&seen=ab", None),
    ("POST", "/api/wait", "", LONG | {"wait": "10"}),                # the board coerces these, so it waits
    ("POST", "/api/wait", "", LONG | {"wait": True}),
])
def test_a_long_poll_whose_caller_hangs_up_is_dropped_upstream(method, path, query, body):
    upstream = Upstream()
    call = gateway_call(upstream, method, path, query, body)
    assert upstream.answered == [], "cancelled before the board answered"
    assert call.response()[0] == 499


def test_an_ack_whose_caller_hung_up_on_the_gateway_is_committed_on_the_board(senv):
    """S1 end to end, in memory: the real hub behind a gateway whose upstream is slow (queued for a connection)."""
    on(senv)
    tid = senv.thread()
    wake(senv)
    ask(senv, tid)
    body = {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA}
    offer = client(senv, "claude").post("/api/wait", json=body).json()
    assert offer["state"] == "wake" and rec(senv)["offer"] is not None
    upstream = Upstream(forward=httpx.ASGITransport(senv.app, client=("127.0.0.1", 50123)))

    async def main():
        call = Call(gateway.create_app("http://127.0.0.1:8787", transport=upstream), method="POST",
                    path="/api/wait", body=body | {"ack": offer["id"], "wait": 0}, token=senv.tokens["claude"])
        call.hang_up()
        await call.run()
        return call
    asyncio.run(main())
    r = rec(senv)
    assert r["offer"] is None and r["delivered"], "the acknowledgement reached the board and was committed"


def test_a_response_the_gateway_got_is_closed_when_its_task_is_cancelled():
    """Review round 1, S2: the upstream response is closed whatever ends the gateway's task after it arrived."""
    upstream = Upstream(delay=0)

    async def main():
        call = Call(gateway.create_app("http://board.test", transport=upstream), method="POST", path="/api/wait",
                    body=LONG, token=AGENT)
        task = asyncio.ensure_future(call.run())
        upstream.on_answer = task.cancel        # the gateway's task is cancelled as the board's answer arrives
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    assert [s.closed for s in upstream.streams] == [True]


@pytest.mark.parametrize("body", [LONG, ACK])
def test_a_response_the_gateway_cannot_send_on_is_still_closed(body):
    """S2: the caller is gone as the answer arrives, and sending the response start fails."""
    upstream = Upstream(delay=0)
    call = gateway_call(upstream, "POST", "/api/wait", body=body, send_fails=True, hang_up=False)
    assert call.error is not None, "the send failed"
    assert [s.closed for s in upstream.streams] == [True], "the response the board sent was closed"


def test_a_watcher_that_fails_before_the_disconnect_keeps_the_request_going():
    """Review round 1, S3: a receive that raises is not a hang-up: a live hook must not get a 499."""
    upstream = Upstream(delay=0.2)
    call = gateway_call(upstream, "POST", "/api/wait", body=LONG, hang_up=False, receive_fails=True)
    status, _, body = call.response()
    assert (status, body) == (200, b'{"state":"idle"}') and len(upstream.answered) == 1
    assert [s.closed for s in upstream.streams] == [True]


class Stubborn(Upstream):
    """An upstream whose send finishes despite being cancelled (it swallows the cancellation and answers)."""

    async def handle_async_request(self, request):
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"name": "claude", "is_human": False})
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        stream = Tracked(b'{"state":"idle"}')
        self.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)


@pytest.mark.parametrize("ends", ["the gateway's task is cancelled", "the caller hangs up"])
def test_a_response_a_cancelled_send_still_produced_is_closed(ends):
    """Review round 2, S2: a send that is cancelled while pending is awaited, and whatever it produced is closed."""
    upstream = Stubborn()

    async def main():
        call = Call(gateway.create_app("http://board.test", transport=upstream), method="POST", path="/api/wait",
                    body=LONG, token=AGENT)
        task = asyncio.ensure_future(call.run())
        await asyncio.sleep(0.2)                # the send is pending at the board
        if ends == "the caller hangs up":
            call.hang_up()
            await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0.1)                # anything still running in the background has finished
    asyncio.run(main())
    assert [s.closed for s in upstream.streams] == [True]


def test_forwarding_does_not_read_while_the_cancelled_watcher_still_reads():
    """Review round 2, S6: with a server that refuses overlapping receive() calls, a live caller's response is sent
    whole: the watcher has finished before the response starts reading for a disconnect."""
    upstream = Upstream(delay=0.2)
    call = gateway_call(upstream, "POST", "/api/wait", body=LONG, hang_up=False, strict_receive=True)
    assert call.error is None
    status, _, body = call.response()
    assert (status, body) == (200, b'{"state":"idle"}')
    assert [m.get("more_body", False) for m in call.messages if m["type"] == "http.response.body"][-1] is False


def _hub_waits(senv, method, query, body) -> bool:
    """Ask the hub itself: does this request wait (it answers only after its wait), or answer at once?"""
    start = time.monotonic()
    if method == "GET":
        r = senv.laptop.get("/api/nodes/sessions?" + query)
    else:
        r = client(senv, "claude").post("/api/wait", content=body, headers={"content-type": "application/json"})
    took = time.monotonic() - start
    assert took < 3, "every case here waits at most about a second"
    return took > 0.3


BASE = {"client_session_id": ONE, "projects": [PROJECT], "nonce": NA}
SEEN = "<seen>"


@pytest.mark.parametrize("method,query,body", [
    ("POST", "", BASE | {"wait": 0.5}),
    ("POST", "", BASE | {"wait": "0.5"}),       # pydantic coerces a numeric string, so the hub waits
    ("POST", "", BASE | {"wait": " 0.5 "}),
    ("POST", "", BASE | {"wait": "5e-1"}),
    ("POST", "", BASE | {"wait": True}),        # ... and a boolean: true is a one-second wait
    ("POST", "", BASE | {"wait": False}),
    ("POST", "", BASE | {"wait": 0}),
    ("POST", "", BASE | {"wait": "0"}),
    ("POST", "", BASE | {"wait": -1}),
    ("POST", "", BASE | {"wait": "nan"}),
    ("POST", "", BASE | {"wait": "true"}),
    ("POST", "", BASE | {"wait": None}),
    ("POST", "", BASE | {"wait": [1]}),
    ("POST", "", BASE),
    ("POST", "", BASE | {"wait": 0.5, "ack": None}),
    ("POST", "", BASE | {"wait": 0.5, "ack": "f" * 16}),     # an acknowledgement never waits
    ("POST", "", BASE | {"wait": True, "ack": "f" * 16}),
    ("POST", "", BASE | {"wait": 0.5, "ack": ""}),
    ("POST", "", [BASE | {"wait": 0.5}]),
    ("POST", "", b"{not json"),
    ("GET", f"wait=0.5&seen={SEEN}&tree=1", None),
    ("GET", f"wait=5e-1&seen={SEEN}", None),
    ("GET", f"wait=%200.5%20&seen={SEEN}", None),
    ("GET", "wait=0.5&tree=1", None),                        # no `seen`: the hub answers at once
    ("GET", "wait=0.5&seen=XYZ", None),                      # a `seen` the hub refuses
    ("GET", f"wait=0&seen={SEEN}", None),
    ("GET", f"wait=nan&seen={SEEN}", None),
    ("GET", f"wait=-1&seen={SEEN}", None),
    ("GET", f"wait=abc&seen={SEEN}", None),
    ("GET", f"seen={SEEN}", None),
    ("GET", f"wait=0.5&wait=0&seen={SEEN}", None),           # a repeated parameter: the last one counts at both
    ("GET", f"wait=0&wait=0.5&seen={SEEN}", None),
    ("GET", f"wait=0.5&seen=XYZ&seen={SEEN}", None),
])
def test_the_gateway_races_exactly_the_requests_the_hub_holds_open(senv, method, query, body):
    """Review round 2, S7: `_can_wait` agrees with the hub's own answer for each shape, so the two cannot drift.
    Every other field is valid, the feature is on and nothing is due: only `wait`, `ack` and `seen` vary."""
    from starlette.datastructures import QueryParams
    on(senv)
    ready(senv)
    assert wake(senv) == {"state": "idle"}
    query = query.replace(SEEN, node_version(senv))
    raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    path = "/api/nodes/sessions" if method == "GET" else "/api/wait"
    assert gateway._can_wait(method, path, QueryParams(query), raw) == _hub_waits(senv, method, query, raw)


def test_the_gateway_reads_wait_and_seen_with_the_hubs_own_rules():
    from agent_comms import api as hub
    assert hub.WaitIn.model_fields["wait"].annotation is float, "gateway._FLOAT coerces as this field does"
    assert gateway._SEEN.pattern == hosted._SEEN.pattern


# ------------------------------------------------------------ the localhost check is unchanged


async def _old_localhost_only(request: Request, call_next):
    """api.create_app's middleware as it was before LocalhostOnly (main at cb25b57), verbatim, and installed as it
    was (`@app.middleware("http")`)."""
    client = request.client.host if request.client else ""
    try:
        loopback = client == "testclient" or ipaddress.ip_address(client).is_loopback
    except ValueError:
        loopback = False
    # Host check defeats DNS rebinding from a browser tab; bearer tokens are still required on top.
    host = request.headers.get("host", "")
    hostname = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
    if not loopback or hostname not in api.LOCAL_HOSTNAMES:
        return JSONResponse({"error": "forbidden", "message": "agent-comms only serves localhost"}, 403)
    return await call_next(request)


CLIENTS = [("127.0.0.1", 50123), ("127.8.9.10", 1), ("::1", 2), ("testclient", 50000), ("::ffff:127.0.0.1", 3),
           ("10.0.0.7", 4), ("192.168.1.20", 5), ("100.101.102.103", 6), ("fe80::1", 7), ("localhost", 8),
           ("not-an-address", 9), ("", 0), None]
HOSTS = ["127.0.0.1:8787", "127.0.0.1", "localhost:8787", "localhost", "[::1]:8787", "[::1]", "[::1", "::1",
         "testserver", "testserver:80", "LOCALHOST", "localhost.", "127.0.0.1.evil.example", "evil.example:8787",
         "evil.example", "", None, "localhost:abc", "[127.0.0.1]:8787", " 127.0.0.1"]
# (method, path, query, body, token name, extra headers)
ROUTES = [("GET", "/api/whoami", "", None, "human", ()),
          ("GET", "/", "", None, None, ()),
          ("GET", "/no/such/route", "", None, None, ()),
          ("POST", "/api/wait", "", b"{not json", "claude", ()),
          ("GET", "/api/nodes/sessions", "wait=0", None, "laptop", ()),
          ("GET", "/api/whoami", "", None, None, (("x-forwarded-for", "10.9.9.9"), ("x-real-ip", "10.9.9.9"))),
          ("GET", "/mcp", "", None, "claude", (("accept", "text/event-stream"),)),
          ("POST", "/mcp", "", b"{}", "claude", (("accept", "application/json, text/event-stream"),))]
LOCAL, REMOTE = ("127.0.0.1", 50123), ("10.0.0.7", 4)


def matrix() -> list[dict]:
    """Every client address with every Host on one route; every route with an allowed, a refused-client and a
    refused-host request; Host given twice, in both orders; and a websocket, which the check never looked at."""
    cases = [dict(method="GET", path="/api/whoami", token="human", client=c, host=h)
             for c, h in itertools.product(CLIENTS, HOSTS)]
    for (method, path, query, body, token, headers), (c, h) in itertools.product(
            ROUTES, [(LOCAL, "127.0.0.1:8787"), (REMOTE, "127.0.0.1:8787"), (LOCAL, "evil.example")]):
        cases.append(dict(method=method, path=path, query=query, body=body, token=token, headers=headers,
                          client=c, host=h))
    for first, second in (("evil.example", "127.0.0.1:8787"), ("127.0.0.1:8787", "evil.example")):
        cases.append(dict(method="GET", path="/api/whoami", token="human", client=LOCAL, host=first,
                          headers=(("host", second),)))
    for c in (LOCAL, REMOTE):
        cases.append(dict(path="/ws", client=c, scope_type="websocket"))
    return cases


def run_matrix(app, tokens, cases) -> list:
    """Each case's response, with the app's lifespan running (the /mcp cases need it)."""
    async def one(case):
        case = dict(case)
        if case.get("token"):
            case["token"] = tokens[case["token"]]
        call = Call(app, **case)
        call.hang_up()      # nothing here waits: a later receive() is the disconnect at once
        await call.run()
        return call.response()

    with TestClient(app) as tc:
        return [tc.portal.call(one, case) for case in cases]


def _comparable(response):
    if isinstance(response, list):
        return response
    status, headers, body = response
    # A new MCP session's id is random on each app; everything else must be equal byte for byte.
    return status, [(k, b"<id>" if k == b"mcp-session-id" else v) for k, v in headers], body


def test_the_localhost_check_answers_every_case_as_the_old_middleware_did(senv):
    new = create_app(senv.board)
    old = create_app(senv.board)
    assert [m.cls for m in new.user_middleware] == [api.LocalhostOnly], "the one middleware, in the same place"
    old.user_middleware = []
    old.middleware("http")(_old_localhost_only)
    cases = matrix()
    got, want = run_matrix(new, senv.tokens, cases), run_matrix(old, senv.tokens, cases)
    for case, g, w in zip(cases, got, want):
        assert _comparable(g) == _comparable(w), case
    statuses = [r[0] for r in got if not isinstance(r, list)]
    assert statuses.count(403) > 200 and statuses.count(200) > 10, "both sides of the check are exercised"
    assert {400, 404, 422} <= set(statuses), "and requests that pass it reach the routes behind it"
    refused = next(r for r in got if not isinstance(r, list) and r[0] == 403)
    assert refused[2] == b'{"error":"forbidden","message":"agent-comms only serves localhost"}'
    assert (b"content-type", b"application/json") in refused[1]


def test_the_lifespan_still_runs_through_the_middleware(senv):
    with TestClient(create_app(senv.board), base_url="http://127.0.0.1:8787") as tc:
        r = tc.post("/mcp", headers={"Authorization": "Bearer " + senv.tokens["claude"],
                                     "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200 and r.headers.get("mcp-session-id"), "the MCP session manager is running"
