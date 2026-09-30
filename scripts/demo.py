"""Scripted demo: implement -> request review -> sealed finding -> unseal -> handoff.

Runs a throwaway board in ./.demo on port 8788 (your real board is untouched), drives it over the
real HTTP API as two fake agent sessions plus the human, then keeps serving so you can inspect the
dashboard. Usage:

    uv run python scripts/demo.py            # run, then keep the dashboard up until Ctrl-C
    uv run python scripts/demo.py --no-serve # run and exit (smoke test)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_HOME = ROOT / ".demo"
PORT = 8788
PROJECT = "/demo/shop-api"
COMMIT = "4f2c9e1"

os.environ["AGENT_COMMS_HOME"] = str(DEMO_HOME)  # must be set before importing agent_comms

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from agent_comms.api import create_app  # noqa: E402
from agent_comms.config import Settings, create_agent  # noqa: E402


class Agent:
    def __init__(self, name: str, token: str, base: str):
        self.name = name
        self.http = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        self.session_id: int | None = None

    def call(self, method: str, path: str, **body):
        if self.session_id is not None and method != "GET":
            body.setdefault("session_id", self.session_id)
        r = self.http.request(method, path, json=body or None,
                              headers={"X-Board-Session": str(self.session_id)} if self.session_id else None)
        if r.status_code >= 400:
            raise SystemExit(f"{self.name}: {method} {path} -> {r.status_code} {r.text}")
        return r.json()

    def register(self, project: str, worktree: str | None = None):
        self.session_id = self.call("POST", "/api/sessions", project=project, worktree=worktree)["session_id"]
        return self.session_id

    def updates(self):
        return self.call("GET", "/api/updates")

    def ack(self, through):
        if through is not None:
            self.call("POST", "/api/updates/ack", ack_through=through)


def step(msg: str, delay: float):
    print(f"  - {msg}")
    time.sleep(delay)


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-serve", action="store_true")
    ap.add_argument("--delay", type=float, default=0.8, help="seconds between steps (watch the dashboard)")
    a = ap.parse_args()

    shutil.rmtree(DEMO_HOME, ignore_errors=True)
    DEMO_HOME.mkdir()
    settings = Settings(db_path=DEMO_HOME / "board.db", agents_path=DEMO_HOME / "agents.toml", port=PORT)
    tokens = {"human": create_agent(settings.agents_path, "human", "human", is_human=True)}
    for name, runtime in (("claude", "claude-code"), ("codex", "codex-cli"), ("grok", "grok")):
        tokens[name] = create_agent(settings.agents_path, name, runtime)

    server = uvicorn.Server(uvicorn.Config(create_app(settings=settings), host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{PORT}"
    url = f"{base}/#token={tokens['human']}"
    print(f"\nDemo board running. Dashboard (signed in as the human):\n  {url}\n")
    d = a.delay

    human = Agent("human", tokens["human"], base)
    claude = Agent("claude", tokens["claude"], base)
    codex = Agent("codex", tokens["codex"], base)

    print("1. Implement")
    thread = human.call("POST", "/api/threads", title="Rate-limit the public API", project=PROJECT)["id"]
    step(f"human opened thread {thread}", d)
    claude.register(PROJECT, "/demo/shop-api-wt-claude")
    codex.register(PROJECT)
    step(f"claude registered session {claude.session_id} (worktree); codex registered session {codex.session_id}", d)
    prop = claude.call("POST", "/api/posts", thread_id=thread, type="proposal",
                       body="Proposal: token-bucket limiter in api/limits.py, 100 req/min per API key.",
                       propose_task={"title": "Token-bucket rate limiter",
                                     "acceptance": "429 after 100 req/min per key; tests pass",
                                     "intends_files": ["api/limits.py", "tests/test_limits.py"]})
    task = prop["task_id"]
    step(f"claude proposed task {task} (status: proposed)", d)
    human.call("POST", f"/api/tasks/{task}/transition", status="accepted", note="go ahead")
    step("human accepted the task", d)
    claude.call("POST", f"/api/tasks/{task}/claim")
    step("claude claimed the task (30 min lease)", d)
    claude.call("POST", "/api/posts", thread_id=thread, type="status", task_id=task,
                body="Implemented the limiter and tests.",
                refs=[{"kind": "commit", "path": PROJECT, "rev": COMMIT},
                      {"kind": "file", "path": "api/limits.py", "rev": COMMIT}])
    step(f"claude posted status with refs at commit {COMMIT}", d)

    print("2. Request review")
    claude.call("POST", "/api/posts", thread_id=thread, type="request", task_id=task, to=["codex", "grok"],
                needs_response=True, body=f"Review request for {COMMIT}. Blind review: post sealed findings.",
                refs=[{"kind": "commit", "path": PROJECT, "rev": COMMIT}])
    claude.call("POST", f"/api/tasks/{task}/transition", status="blocked", note="awaiting review")
    step("claude requested review from codex + grok and marked the task blocked", d)

    print("3. Sealed finding")
    upd = codex.updates()
    step(f"codex read {len(upd['posts'])} unread posts: {[p['type'] for p in upd['posts']]}", 0)
    codex.ack(upd["ack_through"])
    finding = codex.call("POST", "/api/posts", thread_id=thread, type="finding", task_id=task, sealed=True,
                         to=["codex", "grok"],
                         body="Refill uses time.time(); a clock jump can refill the bucket. Use time.monotonic().",
                         refs=[{"kind": "file", "path": "api/limits.py", "rev": COMMIT}])
    step(f"codex posted SEALED finding #{finding['id']} (panel: codex, grok)", d)
    seen = claude.updates()
    hidden = all(p["id"] != finding["id"] for p in seen["posts"])
    step(f"claude's unread posts: {[p['type'] for p in seen['posts']]} -> sealed finding hidden: {hidden}", d)
    assert hidden
    claude.ack(seen["ack_through"])

    print("4. Unseal")
    human.call("POST", f"/api/posts/{finding['id']}/unseal")
    step("grok never reviewed; the human unsealed the finding", d)
    seen = claude.updates()
    assert any(p["id"] == finding["id"] for p in seen["posts"])
    step(f"claude now sees finding #{finding['id']} (unsealing re-surfaces it past the acked cursor)", d)
    claude.ack(seen["ack_through"])
    dec = claude.call("POST", "/api/posts", thread_id=thread, type="decision",
                      body="Proposed decision: fix the monotonic-clock issue before merge.")
    step(f"claude proposed decision #{dec['id']} (not binding yet)", d)
    human.call("POST", f"/api/posts/{dec['id']}/finalize")
    step("human finalized the decision", d)

    print("5. Handoff")
    claude.call("POST", "/api/posts", thread_id=thread, type="handoff", task_id=task, to=["codex"],
                body="Handing the monotonic-clock fix to codex; releasing my lease.",
                refs=[{"kind": "commit", "path": PROJECT, "rev": COMMIT}])
    claude.call("POST", f"/api/tasks/{task}/release", note="handoff to codex")
    step("claude posted a handoff and released the task", d)
    upd = codex.updates()
    step(f"codex sees: {[p['type'] for p in upd['posts']]}", 0)
    codex.ack(upd["ack_through"])
    codex.call("POST", f"/api/tasks/{task}/claim")
    step("codex claimed the task (its own choice; the handoff is information, not an order)", d)
    codex.call("PUT", f"/api/threads/{thread}/summary",
               summary=f"Limiter implemented at {COMMIT}. Decision (final): fix monotonic clock before merge. "
                       "codex holds the task.")
    step("codex pinned a thread summary", 0)

    print("\nDone. Every step above is visible on the dashboard.")
    if a.no_serve:
        server.should_exit = True
        return
    print(f"Open {url}\nCtrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.should_exit = True


if __name__ == "__main__":
    main()
