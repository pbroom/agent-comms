"""`board node`: run agents on this machine for the hub's dispatcher (nodes.py has the protocol).

The node connects out to the hub (`--hub`, e.g. https://server.tailnet.ts.net through `board gateway`, or
http://127.0.0.1:8787 on the hub's own machine) with its own token, a node identity created on the hub with
`board create-agent <name> --runtime node`. Everything it runs comes from THIS machine's own `[dispatch]` settings
(board.toml + board.local.toml here): `runners` (any agent CLI: an argv template with {prompt}, {project},
{thread}), `env`, `worktrees` (hub project path -> directory here), `writable_roots`, `claude_tool_projects`,
`max_concurrent`. From the hub it takes only the assignment: which agent, which project, the thread id and the
prompt text the hub's dispatcher wrote. A project or runner this machine does not have is reported as a failed
start, never improvised.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .dispatch import (DispatchConfig, _signal_group, child_env, probe_process, process_start, render_argv,
                       spawn_process, with_writable_roots)

log = logging.getLogger("agent_comms.node")

HELLO_SECONDS = 10.0
# Without hub contact this long, the node stops its own runs: the hub counts them lost after
# nodes.NODE_LOST_SECONDS (180 s) and may start a replacement, which must never run beside the original.
SELF_STOP_SECONDS = 120.0
STATE_FILE = "node-runs.json"   # run id -> {pid, proc_start}: what a restarted node must clean up


@dataclass
class _Local:
    run_id: str
    child: Any
    started: float
    timeout: float
    grace: float
    terminated_at: float | None = None
    killed: bool = False


class Node:
    def __init__(self, client: httpx.Client, config: DispatchConfig, log_dir: Path,
                 spawner: Callable[..., Any] = spawn_process, clock: Callable[[], float] = time.monotonic,
                 probe: Callable[[Any, Any], str] = probe_process, start_of: Callable[[int], Any] = process_start,
                 signal_group: Callable[[int, int], None] = _signal_group, sleep: Callable[[float], None] = time.sleep):
        self.client, self.config, self.log_dir, self.spawner, self.clock = client, config, log_dir, spawner, clock
        self.probe, self.start_of, self.signal_group, self.sleep = probe, start_of, signal_group, sleep
        self.running: dict[str, _Local] = {}
        self.last_hello = float("-inf")
        self.last_contact = clock()

    # ------------------------------------------------------------ local state (survives a node restart)

    def _state_path(self) -> Path:
        return self.log_dir / STATE_FILE

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {rid: {"pid": getattr(l.child, "pid", None),
                      "proc_start": getattr(l, "proc_start", None)} for rid, l in self.running.items()}
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._state_path())

    def recover(self) -> list[str]:
        """At startup: runs an earlier node process on this machine left. Each still-live process it can verify is
        terminated, then killed after the grace period; each run is then reported ended. Returns their run ids."""
        left = self._load_state()
        alive = []
        for rid, d in left.items():
            pid = d.get("pid") if isinstance(d, dict) else None
            if isinstance(pid, int) and self.probe(pid, d.get("proc_start")) == "ours":
                self.signal_group(pid, signal.SIGTERM)
                alive.append((pid, d.get("proc_start")))
        if alive:
            self.sleep(self.config.kill_grace_seconds)
            for pid, start in alive:
                if self.probe(pid, start) == "ours":
                    self.signal_group(pid, signal.SIGKILL)
        for rid in left:
            self._report(rid, "ended", error="node restarted while the run was going; its process was stopped")
        self._save_state()
        return list(left)

    # ------------------------------------------------------------ hub calls

    def _call(self, method: str, path: str, **kw) -> Any:
        r = self.client.request(method, path, **kw)
        r.raise_for_status()
        self.last_contact = self.clock()
        return r.json()

    def hello(self) -> dict:
        out = self._call("POST", "/api/nodes/hello", json={
            "projects": sorted(self.config.worktrees), "runners": sorted(self.config.runners),
            "max_concurrent": self.config.max_concurrent})
        self.last_hello = self.clock()
        return out

    def _report(self, run_id: str, state: str, **fields) -> None:
        try:
            self._call("POST", f"/api/nodes/runs/{run_id}", json={"state": state, **fields})
        except Exception:
            log.exception("could not report run %s as %s", run_id, state)

    # ------------------------------------------------------------ one pass

    def tick(self) -> None:
        """One pass. Local runs are reaped and held to their timeouts whatever the hub does; hub errors propagate
        after that (the caller logs them and retries)."""
        try:
            if self.clock() - self.last_hello >= HELLO_SECONDS:
                self.hello()
            runs = self._call("GET", "/api/nodes/assignments").get("runs", [])
            listed = set()
            for r in runs:
                rid = r.get("run_id")
                if not isinstance(rid, str):
                    continue
                listed.add(rid)
                local = self.running.get(rid)
                if r.get("state") == "assigned" and local is None:
                    if r.get("stop"):
                        self._report(rid, "ended", error="stopped before start")
                    else:
                        self._start(r)
                elif local is not None and r.get("stop") in ("terminate", "kill"):
                    self._stop(local, r["stop"])
                elif r.get("state") == "running" and local is None:
                    # Started by an earlier node process here and not in our state file: nothing left to stop.
                    self._report(rid, "ended", error="node restarted while the run was going")
            for rid, local in list(self.running.items()):
                if rid not in listed:
                    # The hub has ended this run (stopped it, or counted this node lost): it must not go on.
                    self._stop(local, "terminate")
        finally:
            if self.clock() - self.last_contact >= SELF_STOP_SECONDS:
                for local in self.running.values():
                    self._stop(local, "terminate")
            self._reap()

    def _start(self, r: dict) -> None:
        rid, agent, project = r["run_id"], r.get("agent"), r.get("project")
        try:
            template = self.config.runner_for(agent, r.get("runtime"))
            if template is None:
                raise LookupError(f"no runner configured on this machine for {agent}")
            cwd = self.config.worktrees.get((project or "").rstrip("/") or "/")
            if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
                raise FileNotFoundError(f"this machine has no run directory for project {project!r}")
            prompt = r.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ValueError("assignment has no prompt")
            from . import runner_preflight
            if (project in self.config.claude_tool_projects and os.path.basename(template[0]) == "claude"
                    and not runner_preflight.read_only(template)):
                template = runner_preflight.scoped_template(template)
                prompt += " " + runner_preflight.DENIED_TOOL_INSTRUCTION
            template = with_writable_roots(template, self.config.extra_roots(template, project))
            argv = render_argv(template, prompt=prompt, project=cwd, thread_id=int(r.get("thread_id") or 0))
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            child = self.spawner(argv, cwd=cwd, env=child_env(agent, self.config, runtime=r.get("runtime")),
                                 log_path=self.log_dir / f"{rid}.log")
        except Exception as e:
            log.warning("could not start run %s: %s", rid, e)
            self._report(rid, "ended", error=f"{type(e).__name__}: {e}"[:300])
            return
        local = _Local(rid, child, self.clock(), float(r.get("timeout_seconds") or 1800),
                       float(r.get("kill_grace_seconds") or 10))
        try:
            local.proc_start = self.start_of(child.pid)
        except Exception:
            local.proc_start = None
        self.running[rid] = local
        self._save_state()
        self._report(rid, "running", pid=getattr(child, "pid", None))
        log.info("started %s for thread %s (run %s)", agent, r.get("thread_id"), rid)

    def _stop(self, local: _Local, how: str) -> None:
        if how == "kill" and not local.killed:
            local.killed = True
            local.child.kill()
        elif local.terminated_at is None:
            local.terminated_at = self.clock()
            local.child.terminate()

    def _reap(self) -> None:
        now = self.clock()
        for rid, local in list(self.running.items()):
            code = local.child.poll()
            if code is not None:
                del self.running[rid]
                self._save_state()
                self._report(rid, "ended", exit_code=code)
            elif local.terminated_at is None and now - local.started >= local.timeout:
                self._stop(local, "terminate")
            elif (local.terminated_at is not None and not local.killed
                  and now - local.terminated_at >= local.grace):
                self._stop(local, "kill")

    def stop_all(self) -> None:
        """Shutdown: terminate every run, wait the grace period, kill what remains, report each ended."""
        for local in self.running.values():
            self._stop(local, "terminate")
        deadline = self.clock() + self.config.kill_grace_seconds
        while any(l.child.poll() is None for l in self.running.values()) and self.clock() < deadline:
            self.sleep(0.2)
        for local in self.running.values():
            if local.child.poll() is None:
                self._stop(local, "kill")
        for rid, local in list(self.running.items()):
            del self.running[rid]
            self._report(rid, "ended", exit_code=local.child.poll(), error="node shut down")
        self._save_state()


def run(hub: str, token: str, *, poll_seconds: float | None = None) -> None:
    from .config import Settings, home
    settings = Settings.load()
    config = DispatchConfig.from_dict(settings.dispatch, settings=settings)
    log_dir = home() / "data" / "node-logs"

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)      # launchd and `kill` stop the node cleanly, runs included
    with httpx.Client(base_url=hub.rstrip("/"), headers={"Authorization": "Bearer " + token},
                      timeout=httpx.Timeout(30), trust_env=False) as client:
        node = Node(client, config, log_dir)
        me = node.hello()
        node.recover()
        print(f"node {me['node']} connected to {hub}: {len(me['projects'])} project(s), runners "
              f"{', '.join(me['runners']) or 'none'}")
        try:
            while True:
                try:
                    node.tick()
                except httpx.HTTPError as e:
                    log.warning("hub unreachable: %s", e)
                time.sleep(poll_seconds or config.poll_seconds)
        except KeyboardInterrupt:
            node.stop_all()
