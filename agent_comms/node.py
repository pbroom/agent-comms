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

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .dispatch import (DispatchConfig, child_env, render_argv, spawn_process, with_writable_roots)

log = logging.getLogger("agent_comms.node")

HELLO_SECONDS = 10.0


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
                 spawner: Callable[..., Any] = spawn_process, clock: Callable[[], float] = time.monotonic):
        self.client, self.config, self.log_dir, self.spawner, self.clock = client, config, log_dir, spawner, clock
        self.running: dict[str, _Local] = {}
        self.last_hello = float("-inf")

    # ------------------------------------------------------------ hub calls

    def _call(self, method: str, path: str, **kw) -> Any:
        r = self.client.request(method, path, **kw)
        r.raise_for_status()
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
        if self.clock() - self.last_hello >= HELLO_SECONDS:
            self.hello()
        runs = self._call("GET", "/api/nodes/assignments").get("runs", [])
        for r in runs:
            rid = r.get("run_id")
            if not isinstance(rid, str):
                continue
            local = self.running.get(rid)
            if r.get("state") == "assigned" and local is None:
                self._start(r)
            elif local is not None and r.get("stop") in ("terminate", "kill"):
                self._stop(local, r["stop"])
            elif r.get("state") == "running" and local is None:
                # Started by an earlier node process on this machine: its process is not ours to track.
                self._report(rid, "ended", error="node restarted while the run was going")
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
        self.running[rid] = _Local(rid, child, self.clock(), float(r.get("timeout_seconds") or 1800),
                                   float(r.get("kill_grace_seconds") or 10))
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
                self._report(rid, "ended", exit_code=code)
            elif local.terminated_at is None and now - local.started >= local.timeout:
                self._stop(local, "terminate")
            elif (local.terminated_at is not None and not local.killed
                  and now - local.terminated_at >= local.grace):
                self._stop(local, "kill")

    def stop_all(self) -> None:
        for local in self.running.values():
            self._stop(local, "terminate")


def run(hub: str, token: str, *, poll_seconds: float | None = None) -> None:
    from .config import Settings, home
    settings = Settings.load()
    config = DispatchConfig.from_dict(settings.dispatch, settings=settings)
    log_dir = home() / "data" / "node-logs"
    with httpx.Client(base_url=hub.rstrip("/"), headers={"Authorization": "Bearer " + token},
                      timeout=httpx.Timeout(30), trust_env=False) as client:
        node = Node(client, config, log_dir)
        me = node.hello()
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
