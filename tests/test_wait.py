"""board_read_updates(wait_seconds=...): long-poll semantics, liveness, and not blocking the server."""

import asyncio
import json

import pytest

from agent_comms.api import create_app
from agent_comms.core import MAX_WAIT_SECONDS, Invalid

REF = [{"kind": "commit", "path": "/work/repo", "rev": "abc123"}]


def install_sleep(env, hook=None):
    """Replace the board's blocking sleep with one that advances the fake clock, then runs hook(n, elapsed)."""
    start = env.clock()
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        env.clock.advance(seconds)
        if hook:
            hook(len(calls), env.clock() - start)

    env.board.sleep = fake_sleep
    return calls


def bodies(r):
    return [p["body"] for p in r["posts"]]


def read(env, name="claude", **kw):
    return env.board.read_updates(env.p[name], env.sid[name], **kw)


def last_seen(env, name="claude"):
    return env.board.conn.execute("SELECT last_seen FROM sessions WHERE id = ?", (env.sid[name],)).fetchone()[0]


def test_wakes_on_new_addressed_post(env):
    tid = env.thread()
    install_sleep(env, lambda n, t: env.post("codex", tid, "your turn", "handoff", to=["claude"], refs=REF)
                  if n == 3 else None)
    r = read(env, wait_seconds=30, only="addressed")
    assert bodies(r) == ["your turn"]
    assert r["wait"] == {"seconds": 30, "capped": False, "timed_out": False}
    assert env.clock() - 1_800_000_000.0 < 5  # woke at once, did not sit out the 30 s


def test_times_out_empty(env):
    env.thread()
    calls = install_sleep(env)
    start = env.clock()
    r = read(env, wait_seconds=20)
    assert r["posts"] == [] and r["ack_through"] is None
    assert r["wait"]["timed_out"] is True
    assert 20 <= env.clock() - start < 22
    assert len(calls) >= 20  # polled about once a second


def test_returns_immediately_when_posts_exist(env):
    tid = env.thread()
    env.post("codex", tid, "already here")
    calls = install_sleep(env)
    r = read(env, wait_seconds=30)
    assert bodies(r) == ["already here"] and calls == []
    assert r["wait"]["timed_out"] is False


def test_zero_wait_is_the_old_behaviour(env):
    env.thread()
    calls = install_sleep(env)
    r = read(env)
    assert r["posts"] == [] and calls == [] and "wait" not in r


def test_thread_filter_restricts_what_wakes(env):
    a, b = env.thread("a"), env.thread("b")
    install_sleep(env, lambda n, t: env.post("codex", b, "noise in b") if n == 2 else None)
    r = read(env, wait_seconds=10, thread_id=a)
    assert r["posts"] == [] and r["wait"]["timed_out"] is True
    # the post in b is still unread: waiting never acks, and a wait on b picks it up at once
    assert bodies(read(env, wait_seconds=10, thread_id=b)) == ["noise in b"]
    install_sleep(env, lambda n, t: env.post("codex", a, "now in a") if n == 2 else None)
    assert bodies(read(env, wait_seconds=10, thread_id=a)) == ["now in a"]


def test_only_filter_restricts_what_wakes(env):
    tid = env.thread()
    install_sleep(env, lambda n, t: env.post("codex", tid, "chatter") if n == 2 else None)
    r = read(env, wait_seconds=10, only="addressed")
    assert r["posts"] == [] and r["wait"]["timed_out"] is True
    install_sleep(env, lambda n, t: env.post("codex", tid, "for you", to=["claude"]) if n == 2 else None)
    assert bodies(read(env, wait_seconds=10, only="addressed")) == ["for you"]
    install_sleep(env, lambda n, t: env.post("codex", tid, "for grok", to=["grok"], needs_response=True)
                  if n == 2 else None)
    r = read(env, wait_seconds=5, only="needs_response")  # needs a response from grok, not from claude
    assert r["posts"] == [] and r["wait"]["timed_out"] is True


def test_waiting_never_acks(env):
    tid = env.thread()
    install_sleep(env, lambda n, t: env.post("codex", tid, "x") if n == 2 else None)
    first = read(env, wait_seconds=10)
    again = read(env)  # no ack: same post comes back
    assert bodies(first) == bodies(again) == ["x"] and again["ack_through"] == first["ack_through"]
    r = read(env, wait_seconds=1, ack_through=first["ack_through"])  # ack is applied once, up front
    assert r["posts"] == [] and r["acked_through"] == first["ack_through"]


def test_liveness_refreshed_while_waiting(env):
    env.thread()
    t0 = env.clock()
    seen = []

    def hook(n, elapsed):
        # This runs between polls. A poll never leaves last_seen more than 30 s behind the clock.
        seen.append(env.clock() - last_seen(env))

    install_sleep(env, hook)
    read(env, wait_seconds=120)
    assert last_seen(env) >= t0 + 100  # still being refreshed near the end of the wait
    assert max(seen) <= 30
    assert len(set(round(x) for x in seen)) > 1  # it refreshed repeatedly, not once


def test_liveness_contract_holds_with_coarse_polling(env):
    """Even if polls are far apart, the touch cadence (15 s) keeps the gap under the 30 s contract."""
    env.thread()
    env.board.wait_poll_seconds = 14
    gaps = []
    install_sleep(env, lambda n, t: gaps.append(env.clock() - last_seen(env)))
    read(env, wait_seconds=200)
    assert max(gaps) <= 30


def test_returns_on_pause(env):
    env.thread()
    install_sleep(env, lambda n, t: env.board.set_paused(env.p["human"], True) if n == 3 else None)
    r = read(env, wait_seconds=60)
    assert r["paused"] is True and r["posts"] == []
    assert r["wait"]["timed_out"] is False
    assert env.clock() - 1_800_000_000.0 < 5


def test_returns_immediately_when_already_paused(env):
    env.thread()
    env.board.set_paused(env.p["human"], True)
    calls = install_sleep(env)
    r = read(env, wait_seconds=60)
    assert r["paused"] is True and calls == []


def test_sealed_posts_never_wake_a_non_viewer(env):
    tid = env.thread("review")
    task = env.accepted_task(tid, title="feature")
    # claude is not a reviewer and not the author: a sealed finding must not wake it
    install_sleep(env, lambda n, t: env.post("codex", tid, "SECRET", "finding", task_id=task, refs=REF,
                                             sealed=True, to=["codex", "grok"]) if n == 2 else None)
    r = read(env, wait_seconds=10)
    assert r["posts"] == [] and r["wait"]["timed_out"] is True and "SECRET" not in json.dumps(r)
    install_sleep(env, lambda n, t: env.post("grok", tid, "public note") if n == 2 else None)
    r = read(env, wait_seconds=10)
    assert bodies(r) == ["public note"] and "SECRET" not in json.dumps(r)


def test_wait_is_capped_and_validated(env):
    env.thread()
    install_sleep(env)
    r = read(env, wait_seconds=10_000)
    assert r["wait"] == {"seconds": MAX_WAIT_SECONDS, "capped": True, "timed_out": True}
    assert MAX_WAIT_SECONDS == 300
    for bad in (-1, True, "5", 1.5):
        with pytest.raises(Invalid):
            read(env, wait_seconds=bad)


# ------------------------------------------------------------------ not blocking the server


def test_async_wait_does_not_pin_threads(env):
    """More waiters than the default worker-thread limit (40) all wait at once and a poster still gets through."""
    tid = env.thread()
    env.board.wait_poll_seconds = 0.05
    n = 60

    async def go():
        waiters = [asyncio.create_task(env.board.read_updates_async(
            env.p["claude"], env.sid["claude"], wait_seconds=30, only="addressed")) for _ in range(n)]
        await asyncio.sleep(0.3)  # all of them are now inside their wait
        assert not any(w.done() for w in waiters)
        await asyncio.to_thread(env.post, "codex", tid, "wake up", "handoff", to=["claude"])
        return await asyncio.wait_for(asyncio.gather(*waiters), 15)

    results = asyncio.run(go())
    assert all(bodies(r) == ["wake up"] for r in results)


def test_mcp_waiter_wakes_while_another_client_posts(env):
    """Two real MCP clients over streamable HTTP: one blocked in a wait, one posting."""
    import httpx
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    env.board.wait_poll_seconds = 0.05
    tid = env.thread()
    app = create_app(env.board)

    def tool(c, name, args):
        async def go():
            return json.loads((await c.call_tool(name, args)).content[0].text)
        return go()

    async def go():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))

            def http(agent):
                return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787",
                                         headers={"Authorization": f"Bearer {env.tokens[agent]}"})

            async with http("claude") as h1, http("codex") as h2:
                async with Client(streamable_http_client("http://127.0.0.1:8787/mcp", http_client=h1)) as waiter, \
                        Client(streamable_http_client("http://127.0.0.1:8787/mcp", http_client=h2)) as poster:
                    w_sid = (await tool(waiter, "board_register", {"project": "/work/repo"}))["session_id"]
                    p_sid = (await tool(poster, "board_register", {"project": "/work/repo"}))["session_id"]
                    wait = asyncio.create_task(tool(waiter, "board_read_updates", {
                        "session_id": w_sid, "wait_seconds": 30, "only": "addressed"}))
                    await asyncio.sleep(0.3)
                    assert not wait.done()
                    # the server still answers other calls while the wait is parked
                    listed = await asyncio.wait_for(tool(poster, "board_list_threads", {}), 5)
                    assert listed["threads"]
                    await tool(poster, "board_post", {"session_id": p_sid, "thread_id": tid, "type": "handoff",
                                                      "body": "go", "to": ["claude"], "refs": REF})
                    return await asyncio.wait_for(wait, 10)

    r = asyncio.run(go())
    assert bodies(r) == ["go"] and r["wait"]["timed_out"] is False


def test_http_updates_wait_param(env):
    import httpx

    env.board.wait_poll_seconds = 0.05
    tid = env.thread()
    app = create_app(env.board)

    async def go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
        h = lambda a: {"Authorization": f"Bearer {env.tokens[a]}", "X-Board-Session": str(env.sid[a])}  # noqa: E731
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
            wait = asyncio.create_task(c.get("/api/updates", params={"wait_seconds": 30, "only": "addressed"},
                                             headers=h("claude")))
            await asyncio.sleep(0.3)
            assert not wait.done()
            assert (await c.get("/api/whoami", headers=h("claude"))).status_code == 200
            sent = await c.post("/api/posts", headers=h("codex"), json={
                "body": "go", "type": "handoff", "thread_id": tid, "to": ["claude"], "refs": [REF[0]]})
            assert sent.status_code == 200, sent.text
            return await asyncio.wait_for(wait, 10)

    r = asyncio.run(go())
    assert r.status_code == 200 and bodies(r.json()) == ["go"]
