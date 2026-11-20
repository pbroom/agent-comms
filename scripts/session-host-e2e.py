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
4. One process took every turn; `close` ends the session and its process.
5. The project takes a new session; stopping the node stops that session's idle process too.

With the real CLI the runner is `claude -p {prompt} --permission-mode dontAsk --setting-sources= --strict-mcp-config`
(no user settings, hooks or MCP servers), run in the empty temp checkout: five small haiku turns.
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
        create_agent(agents, AGENT, "claude-code")
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
""")
        if self.real:
            runner = [self.args.claude_bin, "-p", "{prompt}", "--permission-mode", "dontAsk", "--setting-sources=",
                      "--strict-mcp-config"]
        else:
            exe = self.tmp / "bin" / "claude"       # the adapter goes by the executable's name
            exe.parent.mkdir()
            exe.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "stand_in_claude.py").read_text())
            exe.chmod(0o700)
            runner = [str(exe), "-p", "{prompt}", "--permission-mode", "dontAsk"]
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

        print("\n[4] one process, then close")
        self.check(view["turns"] == 4 and view["pid"] == pid, f"one process (pid {pid}) took all four turns")
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

        print("\n[5] a new session in the same project, left open while the node is stopped")
        out, err, took = self.board("session", "start", str(self.project), again, "--agent", AGENT, "--model", "haiku",
                                    "--wait", "--json")
        data = json.loads(out)
        self.check(data["session"]["session_id"] != sid and data["message"]["state"] == "done",
                   "the project takes a new session once the old one has ended")
        print(f"  reply: {data['reply']!r}")
        self.open_pid = self.show(data["session"]["session_id"])["pid"]
        self.check(bool(self.open_pid) and alive(self.open_pid), f"its process (pid {self.open_pid}) stays up, idle")

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
        return [pid for pid in self.pids if alive(pid)]

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
    ap.add_argument("--gateway", action="store_true", help="the node reaches the hub through `board gateway`")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory (logs, transcript, database)")
    args = ap.parse_args()
    if args.claude_bin is not None:
        args.claude_bin = os.path.abspath(os.path.expanduser(args.claude_bin))
        if os.path.basename(args.claude_bin) != "claude" or not os.access(args.claude_bin, os.X_OK):
            ap.error("--claude-bin must be an executable named claude")
    run = Run(args)
    print("agent: " + (f"real Claude Code at {args.claude_bin}, --model haiku" if run.real
                       else "stand-in (tests/stand_in_claude.py), no model"))
    print(f"temp directory: {run.tmp}")
    try:
        run.write_settings()
        run.start_services()
        run.scenario()
        print("\n[6] stopping the node (SIGTERM) and the hub")
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
