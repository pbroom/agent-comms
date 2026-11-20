#!/usr/bin/env python3
"""End-to-end check of hosted sessions: a real hub, a real `board node`, the real `board session` CLI.

Everything lives in one temporary directory: the hub's board.toml, agents.toml and database, the node's own
board.toml and logs, the project checkout, and the identities (a human, an agent, a node) made for this run. The hub
and the node are subprocesses on free localhost ports and are stopped at the end. Nothing here reads or writes the
real board database, board.local.toml, agents.toml, the real token files or ~/Library/LaunchAgents.

    python scripts/session-host-e2e.py                         # a stand-in for Claude (tests/stand_in_claude.py)
    python scripts/session-host-e2e.py --claude-bin "$(command -v claude)"    # the real CLI, with --model haiku
    python scripts/session-host-e2e.py --gateway               # the node reaches the hub through `board gateway`

What it does and checks:
1. `board session start <project> <message> --wait`: the turn runs, the reply is printed.
2. `board session send <id> <message> --wait` to the idle session: written to the process within --max-latency.
3. A long turn, and right behind it a second `send --wait`: the second message is queued while the first turn is in
   flight, the first turn is not interrupted, and the second is written within --max-latency of the first ending.
4. A second agent identity posts a request addressed to the hosted agent, in a thread of the project, over the
   hub's HTTP API: the idle session takes a wake turn within --max-wake. The line written to the process's stdin
   names the thread and the counts and holds no part of the post's body.
5. Two more posts inside the hub's wake interval, one over HTTP and one written straight to the database by another
   process (what `board mcp` does): no wake until the interval has passed and the turn before is over, then ONE wake
   for both.
6. One process took every turn; `close` ends the session and its process; a later post wakes nothing.
7. The project takes a new session; stopping the node stops that session's idle process too.

The session's process is started through a small wrapper named `claude` that copies every stdin line to
logs/session-stdin.log before passing it on, so the script can check what was written. With the real CLI the wrapper
runs `claude -p {prompt} --permission-mode dontAsk --setting-sources= --strict-mcp-config` (no user settings, hooks or
MCP servers) in the empty temp checkout: seven small haiku turns.

    python scripts/session-host-e2e.py --claude-bin "$(command -v claude)" --with-board

runs another, shorter scenario (two haiku turns) in which the session has ONE MCP server: this checkout's `board mcp`
on the temp hub's database, signed in as the temp agent from a token file in the temp directory, with only that
server's tools allowed. The second agent posts a request addressed to the hosted agent and the session is told
nothing but the wake. The script reports what the session did (its tool calls, its reply, the board session it
registered) and checks that the hub recorded a delivery receipt for the post and that `hosted.holds` is true for it
once the wake's grace has passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_comms.config import create_agent  # noqa: E402

NODE = "e2enode"
AGENT = "claude"
POSTER = "codex"                # the second agent identity: it posts the requests that wake the hosted session
WAKE_INTERVAL = 6               # the hub's [dispatch.sessions] wake_min_interval_seconds for this run
MARKER = "E2E-BODY-MARKER-4c1e9a"
BODY_WORDS = ("zebra", "quartz", "lantern")

# The session's process, wrapped: every stdin line is appended to a log before the real program gets it.
WRAPPER = """#!{python}
import subprocess, sys
log = open({log!r}, "ab", buffering=0)
child = subprocess.Popen([{real!r}, *sys.argv[1:]], stdin=subprocess.PIPE)
try:
    for line in sys.stdin.buffer:
        log.write(line)
        child.stdin.write(line)
        child.stdin.flush()
    child.stdin.close()
except OSError:
    pass
sys.exit(child.wait())
"""

# A post written straight to the hub's database by another process, as `board mcp` (stdio) does.
DIRECT_POST = """
import json, os, sys
from agent_comms.config import Settings
from agent_comms.core import Board
board = Board(Settings.load())
p = board.authenticate(os.environ["E2E_POSTER_TOKEN"])
sid = board.register_session(p, sys.argv[1])["session_id"]
post = board.create_post(p, sid, body=sys.argv[3], type="request", thread_id=int(sys.argv[2]), to=[sys.argv[4]])
print(json.dumps({"id": post["id"]}))
"""

BOARD_GRACE = 5                 # --with-board: the hub's wake_grace_seconds, short so `holds` is asked after it
BOARD_BOUND = 90                # --with-board: most seconds from the post to the session's delivery receipt

# --with-board: what the hub's database says about one request, read by another process on that database.
PROBE = """
import json, sys, time
from agent_comms import delivery, hosted, hosted_wake
from agent_comms.config import Settings
from agent_comms.core import Board
board = Board(Settings.load())
agent, project, post_id, sid = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
c, now = board.conn, time.time()
rec = hosted_wake._load(c, sid) or {}
rows = [dict(r) for r in c.execute("SELECT id, project, worktree, started_at, last_seen FROM sessions "
                                   "WHERE agent = ? ORDER BY id", (agent,))]
print(json.dumps({"now": now, "holds": hosted.holds(board, agent, project, post_id, now),
                  "woken_at": (rec.get("woken") or {}).get(str(post_id)), "grace": rec.get("grace"),
                  "receipt": delivery.receipt(c, post_id, agent), "readers": delivery.readers(c, post_id, agent),
                  "sessions": rows}))
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not alive(pid)


def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(x) for x in items) + "]"


class Run:
    def __init__(self, args):
        self.args = args
        self.real = args.claude_bin is not None
        self.tmp = Path(tempfile.mkdtemp(prefix="session-host-e2e-")).resolve()
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.pids: set[int] = set()
        self.port = free_port()
        self.hub_home, self.node_home = self.tmp / "hub", self.tmp / "node"
        self.project = self.tmp / "checkout"
        for d in (self.hub_home, self.node_home, self.project, self.tmp / "tokens", self.tmp / "logs"):
            d.mkdir()
        # The environment every subprocess starts from: no board variable of the caller's survives, and the token
        # locations point into the temp directory (they are never read: tokens are passed in the environment).
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_COMMS_", "BOARD_"))}
        self.env |= {"PYTHONPATH": str(ROOT), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens"),
                     "AGENT_COMMS_TOKEN_FILE": str(self.tmp / "tokens" / "human.token"), "PYTHONUNBUFFERED": "1"}
        if not self.real:
            (self.tmp / "home").mkdir()
            self.env["HOME"] = str(self.tmp / "home")     # the stand-in needs nothing of the user's

    # ------------------------------------------------------------ set-up

    def write_settings(self) -> None:
        agents = self.hub_home / "agents.toml"
        self.human_token = create_agent(agents, "human", "human", is_human=True)
        agent_token = create_agent(agents, AGENT, "claude-code")
        self.poster_token = create_agent(agents, POSTER, "codex-cli")
        self.node_token = create_agent(agents, NODE, "node")
        (self.hub_home / "board.toml").write_text(f"""[server]
host = "127.0.0.1"
port = {self.port}
db_path = "data/board.db"
agents_path = "agents.toml"

[conversations]
enabled = false

[dispatch.nodes.{NODE}]
projects = {toml_list([str(self.project)])}
agents = {toml_list([AGENT])}

[dispatch.sessions]
wake_min_interval_seconds = {WAKE_INTERVAL}
""" + (f"wake_grace_seconds = {BOARD_GRACE}\n" if self.args.with_board else ""))
        real = self.args.claude_bin
        if not self.real:
            real = self.tmp / "stand-in" / "claude"
            real.parent.mkdir()
            real.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "stand_in_claude.py").read_text())
            real.chmod(0o700)
        self.stdin_log = self.tmp / "logs" / "session-stdin.log"
        exe = self.tmp / "bin" / "claude"           # the adapter goes by the executable's name
        exe.parent.mkdir()
        exe.write_text(WRAPPER.format(python=sys.executable, log=str(self.stdin_log), real=str(real)))
        exe.chmod(0o700)
        runner = [str(exe), "-p", "{prompt}", "--permission-mode", "dontAsk"]
        if self.real:
            runner += ["--setting-sources=", "--strict-mcp-config"]
        if self.args.with_board:
            # The session's only MCP server: this checkout's `board mcp` (stdio), on the TEMP hub's database, signed
            # in as the temp agent from a token file in the temp directory. Nothing else is configured or allowed:
            # no user settings (--setting-sources=), no other MCP server (--strict-mcp-config), and only the board's
            # tools are allowed (dontAsk refuses what would prompt; Bash and the file tools are disallowed outright).
            token_file = self.tmp / "tokens" / f"{AGENT}.token"
            token_file.touch(mode=0o600)
            token_file.write_text(agent_token + "\n")
            (self.tmp / "tokens").chmod(0o700)
            mcp = self.tmp / "claude-mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {"agent-comms": {
                "type": "stdio", "command": sys.executable, "args": ["-m", "agent_comms.cli", "mcp", "--agent", AGENT],
                "env": {"AGENT_COMMS_HOME": str(self.hub_home), "AGENT_COMMS_TOKEN_DIR": str(self.tmp / "tokens"),
                        "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}}}}, indent=2))
            runner += ["--mcp-config", str(mcp), "--allowedTools=mcp__agent-comms",
                       "--disallowedTools=Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"]
        (self.node_home / "board.toml").write_text(f"""[conversations]
enabled = false

[dispatch]
kill_grace_seconds = 5

[dispatch.runners]
{AGENT} = {toml_list(runner)}

[dispatch.worktrees]
{json.dumps(str(self.project))} = {json.dumps(str(self.project))}

[dispatch.sessions]
enabled = true
""")

    def spawn(self, name: str, home: Path, *argv: str, **extra_env: str) -> subprocess.Popen:
        log = open(self.tmp / "logs" / f"{name}.log", "w")
        proc = subprocess.Popen([sys.executable, "-m", "agent_comms.cli", *argv], cwd=str(self.tmp), stdout=log,
                                stderr=subprocess.STDOUT, env=self.env | {"AGENT_COMMS_HOME": str(home)} | extra_env)
        self.procs.append((name, proc))
        return proc

    def log_text(self, name: str) -> str:
        try:
            return (self.tmp / "logs" / f"{name}.log").read_text()
        except OSError:
            return ""

    def wait_for(self, what: str, check, seconds: float = 40.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for name, proc in self.procs:
                if proc.poll() is not None:
                    raise SystemExit(f"{name} exited early (code {proc.returncode}):\n{self.log_text(name)}")
            if check():
                return
            time.sleep(0.1)
        raise SystemExit(f"timed out waiting for {what}")

    def hub_up(self) -> bool:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/whoami",
                                     headers={"Authorization": "Bearer " + self.human_token})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as r:
                return json.loads(r.read())["is_human"] is True
        except (OSError, ValueError, KeyError):
            return False

    def start_services(self) -> None:
        self.spawn("hub", self.hub_home, "serve", "--port", str(self.port))
        self.wait_for("the hub to answer", self.hub_up)
        target = self.port
        if self.args.gateway:
            target = free_port()
            self.spawn("gateway", self.hub_home, "gateway", "--port", str(target))
            self.wait_for("the gateway to listen", lambda: listening(target))
        self.spawn("node", self.node_home, "node", "--hub", f"http://127.0.0.1:{target}", "--name", NODE,
                   AGENT_COMMS_TOKEN=self.node_token)
        self.wait_for("the node to connect", lambda: "connected to" in self.log_text("node"))
        print(f"hub on 127.0.0.1:{self.port}" + (f", gateway on 127.0.0.1:{target}" if self.args.gateway else "")
              + f"; {self.log_text('node').strip().splitlines()[0]}")

    # ------------------------------------------------------------ the CLI

    def board(self, *argv: str, ok: bool = True) -> tuple[str, str, float]:
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-m", "agent_comms.cli", *argv], cwd=str(self.tmp), capture_output=True,
                           text=True, timeout=300,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home), "BOARD_TOKEN": self.human_token})
        took = time.monotonic() - t0
        if ok and r.returncode != 0:
            raise SystemExit(f"`board {' '.join(argv)}` exited {r.returncode}:\n{r.stdout}{r.stderr}")
        return r.stdout, r.stderr, took

    def show(self, sid: str) -> dict:
        view = json.loads(self.board("session", "show", sid, "--json")[0])
        if view.get("pid"):
            self.pids.add(view["pid"])
        return view

    def api(self, token: str, method: str, path: str, body: dict | None = None) -> dict:
        """One call to the hub's HTTP API as the identity with this token."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
            return json.loads(r.read())

    def message(self, sid: str, n: int, until, seconds: float) -> dict | None:
        """The session's message `n` once `until(message)` holds, or as it stands when the time is up."""
        deadline = time.monotonic() + seconds
        while True:
            m = next((m for m in self.show(sid)["messages"] if m["n"] == n), None)
            if (m is not None and until(m)) or time.monotonic() >= deadline:
                return m
            time.sleep(0.1)

    def stdin_lines(self) -> list[str]:
        """What was written to the session processes' stdin so far: the text of each turn, in order."""
        return [json.loads(line)["message"]["content"] for line in self.stdin_log.read_text().splitlines()]

    def check(self, condition: bool, what: str) -> None:
        if not condition:
            raise SystemExit(f"FAIL: {what}")
        print(f"  ok: {what}")

    # ------------------------------------------------------------ the scenario

    def scenario(self) -> None:
        limit = self.args.max_latency
        if self.real:
            first, second = "Reply with exactly the word: ready", "Reply with exactly the word: pong"
            long, after = ("Write a story of about 300 words about a lighthouse keeper.",
                           "Reply with exactly the word: after")
            again = "Reply with exactly the word: again"
        else:
            first, second, long, after, again = "ready", "pong", "sleep 4 then tell a long story", "after", "again"

        print("\n[1] board session start <project> <message> --wait")
        out, err, took = self.board("session", "start", str(self.project), first, "--agent", AGENT, "--model", "haiku",
                                    "--wait", "--json")
        data = json.loads(out)
        sid, m1 = data["session"]["session_id"], data["message"]
        print(f"  session {sid} on node {data['session']['node']}; wall {took:.2f}s; queued -> written "
              f"{m1['delivered_at'] - m1['created_at']:.2f}s; turn {m1['ended_at'] - m1['delivered_at']:.2f}s")
        print(f"  reply: {data['reply']!r}")
        self.check(m1["state"] == "done" and bool(data["reply"].strip()), "the first turn ended with a reply")
        self.check(m1["delivered_at"] - m1["created_at"] <= limit, f"written within {limit:g}s of the start")
        if not self.real:
            self.check(data["reply"] == "turn 1: READY", "the stand-in's reply came back unchanged")
        pid = self.show(sid)["pid"]

        print("\n[2] board session send <id> <message> --wait   (the session is idle)")
        out, err, took = self.board("session", "send", sid, second, "--wait")
        m2 = next(m for m in self.show(sid)["messages"] if m["n"] == 2)
        print(f"  wall {took:.2f}s; queued -> written {m2['delivered_at'] - m2['created_at']:.2f}s; "
              f"turn {m2['ended_at'] - m2['delivered_at']:.2f}s")
        print(f"  reply: {out.strip()!r}")
        self.check(m2["state"] == "done" and bool(out.strip()), "the turn ended and stdout is its reply")
        self.check(m2["delivered_at"] - m2["created_at"] <= limit,
                   f"a message to the idle session was written within {limit:g}s")
        if not self.real:
            self.check(out == "turn 2: PONG\n", "stdout is exactly the reply")

        print("\n[3] a long turn, then at once: board session send <id> <message> --wait   (the session is busy)")
        self.board("session", "send", sid, long)
        out, err, took = self.board("session", "send", sid, after, "--wait")
        view = self.show(sid)
        m3, m4 = (next(m for m in view["messages"] if m["n"] == n) for n in (3, 4))
        t0 = m3["created_at"]
        print(f"  long turn:   sent +0.00s, written +{m3['delivered_at'] - t0:.2f}s, ended +{m3['ended_at'] - t0:.2f}s")
        print(f"  queued send: sent +{m4['created_at'] - t0:.2f}s, written +{m4['delivered_at'] - t0:.2f}s "
              f"({m4['delivered_at'] - m3['ended_at']:.2f}s after the long turn ended), ended "
              f"+{m4['ended_at'] - t0:.2f}s; wall {took:.2f}s")
        print(f"  reply: {out.strip()!r}")
        self.check(m3["delivered_at"] - m3["created_at"] <= limit, f"the long turn started within {limit:g}s")
        self.check(m3["created_at"] < m4["created_at"] < m3["ended_at"],
                   "the second message was sent before the long turn ended, so it had to queue")
        self.check(m3["state"] == "done", "the long turn ran to its end: nothing was interrupted")
        self.check(m4["delivered_at"] >= m3["ended_at"], "the queued message was written only after the long turn ended")
        self.check(m4["delivered_at"] - m3["ended_at"] <= limit, f"... and within {limit:g}s of it")
        self.check(m4["state"] == "done" and bool(out.strip()), "the queued turn ended with a reply")
        if not self.real:
            self.check(out == "turn 4: AFTER\n", "its reply is the fourth turn's")

        tid = self.wake_scenario(sid)

        print("\n[6] one process, then close")
        view = self.show(sid)
        self.check(view["turns"] == 6 and view["pid"] == pid, f"one process (pid {pid}) took all six turns")
        self.check(len(self.stdin_lines()) == 6, "six lines were written to its stdin, one per turn")
        listed, _, _ = self.board("session", "list")
        self.check(sid in listed, "board session list shows it")
        out, _, _ = self.board("session", "close", sid)
        print(f"  {out.strip()}")
        deadline = time.monotonic() + 20
        while self.show(sid)["state"] != "ended" and time.monotonic() < deadline:
            time.sleep(0.2)
        self.check(self.show(sid)["state"] == "ended", "the node confirmed the close")
        out, err, _ = self.board("session", "send", sid, "too late", ok=False)
        self.check("has ended" in err, "a closed session takes no message")
        # The hub hears `ended` when the node has signalled the process; the node reaps it on a pass soon after.
        self.check(gone(pid), f"its process (pid {pid}) is gone")

        self.api(self.poster_token, "POST", "/api/posts",
                 {"body": f"after the close {MARKER}", "type": "request", "thread_id": tid, "to": [AGENT],
                  "session_id": self.poster_sid})
        time.sleep(1.5)
        self.check(len(self.stdin_lines()) == 6 and not any(m["n"] > 6 for m in self.show(sid)["messages"]),
                   "a post addressed to the agent after the close wakes nothing")

        print("\n[7] a new session in the same project, left open while the node is stopped")
        out, err, took = self.board("session", "start", str(self.project), again, "--agent", AGENT, "--model", "haiku",
                                    "--wait", "--json")
        data = json.loads(out)
        self.check(data["session"]["session_id"] != sid and data["message"]["state"] == "done",
                   "the project takes a new session once the old one has ended")
        print(f"  reply: {data['reply']!r}")
        self.open_pid = self.show(data["session"]["session_id"])["pid"]
        self.check(bool(self.open_pid) and alive(self.open_pid), f"its process (pid {self.open_pid}) stays up, idle")

    # ------------------------------------------------------------ --with-board: a woken session reads the board

    def probe(self, post_id: int, sid: str) -> dict:
        r = subprocess.run([sys.executable, "-c", PROBE, AGENT, str(self.project), str(post_id), sid],
                           cwd=str(self.tmp), capture_output=True, text=True, timeout=60,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home)})
        if r.returncode != 0:
            raise SystemExit(f"could not read the hub's database:\n{r.stderr[-2000:]}")
        return json.loads(r.stdout)

    def tool_uses(self, sid: str) -> list[dict]:
        """The tool calls the session's process made, in order, from its transcript on the node."""
        uses = []
        for path in self.node_home.rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                content = (ev.get("message") or {}).get("content") if ev.get("type") == "assistant" else None
                uses += [b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        return uses

    def tool_calls(self, sid: str) -> list[str]:
        return [b.get("name", "?") for b in self.tool_uses(sid)]

    def board_scenario(self) -> None:
        """A real session with the temp board's tools: is a wake enough for it to read the board? Reports what the
        session did either way (its tool calls and its reply) before anything is asserted about the receipt."""
        print("\n[1] board session start <project> <message> --wait   (the session has the temp board's MCP tools)")
        out, _, took = self.board("session", "start", str(self.project), "Reply with exactly the word: ready",
                                  "--agent", AGENT, "--model", "haiku", "--wait", "--json")
        data = json.loads(out)
        sid = data["session"]["session_id"]
        print(f"  session {sid}; wall {took:.2f}s; reply: {data['reply']!r}")
        self.check(data["message"]["state"] == "done", "the first turn ended")
        before = self.tool_calls(sid)       # the board's own instructions say to register at session start
        print(f"  tools called in the first turn: {before or 'none'}; board sessions of {AGENT} so far: "
              f"{[row['id'] for row in self.probe(0, sid)['sessions']] or 'none'}")

        print(f"\n[2] {POSTER} posts a request addressed to {AGENT}; nothing else is said to the session")
        self.poster_sid = self.api(self.poster_token, "POST", "/api/sessions",
                                   {"project": str(self.project)})["session_id"]
        tid = self.api(self.poster_token, "POST", "/api/threads",
                       {"title": "delivery check", "project": str(self.project),
                        "session_id": self.poster_sid})["id"]
        t1 = time.time()        # the hub's clock is this machine's
        post = self.api(self.poster_token, "POST", "/api/posts",
                        {"body": f"Delivery check {MARKER}: no work is needed. Reading this post is enough.",
                         "type": "request", "thread_id": tid, "to": [AGENT], "needs_response": True,
                         "session_id": self.poster_sid})
        m2 = self.message(sid, 2, lambda m: m["delivered_at"] is not None, 15)
        self.check(m2 is not None and m2["kind"] == "wake" and m2["delivered_at"] is not None,
                   "the post queued a wake and it was written to the process")
        print(f"  post #{post['id']} in thread {tid} +0.00s; wake written +{m2['delivered_at'] - t1:.2f}s")
        print(f"  stdin line: {self.stdin_lines()[1]!r}")
        self.check(MARKER not in self.stdin_log.read_text(), "the stdin line holds no part of the post's body")

        deadline, state = time.monotonic() + BOARD_BOUND, self.probe(post["id"], sid)
        while state["receipt"] is None and time.monotonic() < deadline:
            time.sleep(0.5)
            state = self.probe(post["id"], sid)
        m2 = self.message(sid, 2, lambda m: m["ended_at"] is not None, 180)
        view = self.show(sid)
        calls = self.tool_calls(sid)[len(before):]
        print(f"  wake turn: {m2['state']}" + (f", ended +{m2['ended_at'] - t1:.2f}s" if m2["ended_at"] else "")
              + (f"; tools denied: {view['denials']}" if view.get("denials") else ""))
        print(f"  tools the session called in the wake turn: {calls or 'none'}")
        for use in self.tool_uses(sid)[len(before):]:
            if use.get("name", "").startswith("mcp__agent-comms__"):   # its own arguments, on the temp board
                print(f"    {use['name'].removeprefix('mcp__agent-comms__')}({json.dumps(use.get('input'))[:300]})")
        print(f"  reply: {view['reply']!r}")
        posts = self.api(self.poster_token, "GET", f"/api/threads/{tid}/posts")["posts"]
        for p in posts:
            print(f"  thread {tid}: #{p['id']} by {p['agent']} ({p['type']}): {p['body'][:160]!r}")
        self.check(m2["state"] == "done", "the wake turn ran to its end")
        self.check(state["receipt"] is not None,
                   f"the hub recorded a delivery receipt for post #{post['id']} and {AGENT} within {BOARD_BOUND}s")
        receipt = state["receipt"]
        print(f"  receipt: board session {receipt['session_id']}, +{receipt['at'] - t1:.2f}s after the post "
              f"(+{receipt['at'] - m2['delivered_at']:.2f}s after the wake was written)")
        self.check(receipt["at"] >= m2["delivered_at"], "the receipt is dated after the wake was written: the read "
                                                        "happened in the wake turn (the session was idle before it)")
        self.check("mcp__agent-comms__board_read_updates" in calls, "the wake turn called board_read_updates")
        self.check(not {"Bash", "Edit", "Write", "NotebookEdit"} & set(calls), "... and no shell or file-writing tool")

        wait = state["woken_at"] + BOARD_GRACE + 0.5 - time.time()
        if wait > 0:
            time.sleep(wait)
        state = self.probe(post["id"], sid)
        print(f"  holds: {state['holds']} at +{state['now'] - t1:.2f}s; wake covered the post at "
              f"+{state['woken_at'] - t1:.2f}s; wake_grace_seconds {state['grace']:g}; readers {state['readers']}")
        self.check(state["now"] - state["woken_at"] > state["grace"] and state["holds"] is True,
                   "hosted.holds is true for the post after the grace: the receipt, dated after the wake, keeps it")
        print("  board sessions of the hosted agent (what it registered with):")
        for row in state["sessions"]:
            print(f"    session {row['id']}: project {row['project']!r}, worktree {row['worktree']!r}")
        mine = next((row for row in state["sessions"] if row["id"] == receipt["session_id"]), {})
        print(f"  the thread's project is {str(self.project)!r}: the reading session registered "
              + ("under the SAME project" if mine.get("project") == str(self.project) else "under ANOTHER project"))
        self.open_pid = view["pid"]
        self.check(bool(self.open_pid) and alive(self.open_pid),
                   f"the session's process (pid {self.open_pid}) stays up")

    def wake_scenario(self, sid: str) -> int:
        limit, body = self.args.max_wake, f"Please look at this. {MARKER} " + " ".join(BODY_WORDS)
        print("\n[4] a second agent posts a request addressed to the hosted agent   (the session is idle)")
        self.poster_sid = self.api(self.poster_token, "POST", "/api/sessions",
                                   {"project": str(self.project)})["session_id"]
        tid = self.api(self.poster_token, "POST", "/api/threads",
                       {"title": f"e2e wake {MARKER}", "project": str(self.project),
                        "session_id": self.poster_sid})["id"]
        t1 = time.time()        # the hub's clock is this machine's
        self.api(self.poster_token, "POST", "/api/posts",
                 {"body": body, "type": "request", "thread_id": tid, "to": [AGENT], "needs_response": True,
                  "session_id": self.poster_sid})
        m5 = self.message(sid, 5, lambda m: m["delivered_at"] is not None, 15)
        self.check(m5 is not None and m5["kind"] == "wake" and m5["delivered_at"] is not None,
                   "the post queued a wake message for the session, and it was written to the process")
        print(f"  {POSTER} posted in thread {tid} +0.00s; wake queued +{m5['created_at'] - t1:.2f}s; "
              f"written to the process +{m5['delivered_at'] - t1:.2f}s")
        self.check(m5["delivered_at"] - t1 <= limit,
                   f"the idle session took the wake turn within {limit:g}s of the post")
        line = self.stdin_lines()[4]
        print(f"  stdin line: {line!r}")
        self.check(f"1 new post(s) addressed to {AGENT} from {POSTER} in thread {tid} (1 needing a response)" in line,
                   "the stdin line holds the counts, the author's name and the thread id")
        self.check("board_read_updates" in line and "untrusted data" in line and "not a message from the human" in line,
                   "it says to call board_read_updates, that board content is untrusted data, and whose text it is")

        print(f"\n[5] two more posts inside the wake interval ({WAKE_INTERVAL}s): one over HTTP, one written to the "
              "database by another process")
        t2 = time.time()
        self.api(self.poster_token, "POST", "/api/posts",
                 {"body": f"second {MARKER}", "type": "request", "thread_id": tid, "to": [AGENT],
                  "session_id": self.poster_sid})
        r = subprocess.run([sys.executable, "-c", DIRECT_POST, str(self.project), str(tid), f"third {MARKER}", AGENT],
                           cwd=str(self.tmp), capture_output=True, text=True, timeout=60,
                           env=self.env | {"AGENT_COMMS_HOME": str(self.hub_home),
                                           "E2E_POSTER_TOKEN": self.poster_token})
        self.check(r.returncode == 0 and "id" in json.loads(r.stdout or "{}"),
                   "the direct database write was accepted" + ("" if r.returncode == 0 else f": {r.stderr[-400:]}"))
        t3 = time.time()
        time.sleep(1.0)
        early = [m for m in self.show(sid)["messages"] if m["n"] > 5]
        m5 = self.message(sid, 5, lambda m: m["ended_at"] is not None, 120)
        m6 = self.message(sid, 6, lambda m: m["ended_at"] is not None, WAKE_INTERVAL + 120)
        self.check(m6 is not None and m6["kind"] == "wake" and m6["state"] == "done", "a second wake turn ran")
        print(f"  first wake: queued +0.00s, turn ended +{m5['ended_at'] - m5['created_at']:.2f}s; posts 2 and 3 "
              f"+{t2 - m5['created_at']:.2f}s and +{t3 - m5['created_at']:.2f}s; second wake queued "
              f"+{m6['created_at'] - m5['created_at']:.2f}s, written +{m6['delivered_at'] - m5['created_at']:.2f}s")
        self.check(early == [] or m6["created_at"] - m5["created_at"] >= WAKE_INTERVAL,
                   "nothing was queued for them at once")
        self.check(m6["created_at"] - m5["created_at"] >= WAKE_INTERVAL - 0.01,
                   f"the second wake was queued no sooner than {WAKE_INTERVAL}s after the first")
        self.check(m6["created_at"] >= m5["ended_at"], "... and only after the first wake's turn had ended")
        lines = self.stdin_lines()
        print(f"  stdin line: {lines[5]!r}")
        self.check(len(lines) == 6 and f"2 new post(s) addressed to {AGENT} from {POSTER} in thread {tid}" in lines[5],
                   "both posts went as ONE wake")
        view = self.show(sid)
        self.check([m["kind"] for m in view["messages"]] == ["user"] * 4 + ["wake"] * 2 and view["queued"] == 0,
                   "four messages of the human's and two wakes; nothing else was queued")
        everything = self.stdin_log.read_text() + self.log_text("node")
        self.check(not any(w in everything for w in (MARKER, *BODY_WORDS, "e2e wake")),
                   "no part of a post body or of the thread title is in the process's stdin or the node's log")
        self.check(view["reply_for"] == 6 and bool((view["reply"] or "").strip()),
                   "the wake turn's reply is the session's last reply (the human's to read)")
        print(f"  reply: {view['reply']!r}")
        return tid

    # ------------------------------------------------------------ teardown

    def stop(self) -> None:
        for name, proc in reversed(self.procs):     # the node first: it stops its session processes
            if proc.poll() is None:
                t0 = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(30)
                    print(f"  {name} stopped {time.monotonic() - t0:.1f}s after SIGTERM")
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
                    print(f"  {name} did not stop on SIGTERM within 30s and was killed")
        self.procs = []

    def leftovers(self) -> list[int]:
        """Session processes still there. Each was started in its own process group (its pid), and the wrapper's
        child (the stand-in, or Claude itself) is in that group: the whole group must be gone."""
        return [pid for pid in self.pids if alive(pid) or group_alive(pid)]

    def cleanup(self) -> None:
        self.stop()
        for pid in self.leftovers():    # only pids this run's own hub reported for this run's own session
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.args.keep:
            print(f"kept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--claude-bin", metavar="PATH", help="the real Claude Code CLI (default: a stand-in script)")
    ap.add_argument("--max-latency", type=float, default=2.0, metavar="SECONDS",
                    help="most seconds from a message being sent to it being written to the process (default 2)")
    ap.add_argument("--max-wake", type=float, default=3.0, metavar="SECONDS",
                    help="most seconds from a post addressed to the agent to its wake being written (default 3)")
    ap.add_argument("--gateway", action="store_true", help="the node reaches the hub through `board gateway`")
    ap.add_argument("--with-board", action="store_true",
                    help="with --claude-bin: another scenario, in which the session has the temp board's MCP tools "
                         "(and only those). A post addressed to its agent must wake it AND be read by it")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory (logs, transcript, database)")
    args = ap.parse_args()
    if args.claude_bin is not None:
        args.claude_bin = os.path.abspath(os.path.expanduser(args.claude_bin))
        if os.path.basename(args.claude_bin) != "claude" or not os.access(args.claude_bin, os.X_OK):
            ap.error("--claude-bin must be an executable named claude")
    elif args.with_board:
        ap.error("--with-board needs --claude-bin: the stand-in calls no tools")
    run = Run(args)
    print("agent: " + (f"real Claude Code at {args.claude_bin}, --model haiku" if run.real
                       else "stand-in (tests/stand_in_claude.py), no model")
          + (", with the temp board's MCP tools" if args.with_board else ""))
    print(f"temp directory: {run.tmp}")
    try:
        run.write_settings()
        run.start_services()
        if args.with_board:
            run.board_scenario()
        else:
            run.scenario()
        print("\n[3] stopping the node (SIGTERM) and the hub" if args.with_board
              else "\n[8] stopping the node (SIGTERM) and the hub")
        run.stop()
        run.check(not alive(run.open_pid), f"the open session's process (pid {run.open_pid}) was stopped with the node")
        run.check(run.leftovers() == [], "no session process is left behind")
        print("\nPASS")
        return 0
    except SystemExit as e:
        print(e if isinstance(e.code, str) else f"exit {e.code}")
        for name in ("node", "hub", "gateway"):
            text = run.log_text(name)
            if text:
                print(f"--- {name} log (last lines) ---\n" + "\n".join(text.splitlines()[-25:]))
        return 1
    finally:
        run.cleanup()


if __name__ == "__main__":
    sys.exit(main())
