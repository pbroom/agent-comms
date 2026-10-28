"""`board node`: run agents on this machine for the hub's dispatcher (nodes.py has the protocol).

The node connects out to the hub (`--hub`, e.g. https://server.tailnet.ts.net through `board gateway`, or
http://127.0.0.1:8787 on the hub's own machine) with its own token, a node identity created on the hub with
`board create-agent <name> --runtime node`. Everything it runs comes from THIS machine's own `[dispatch]` settings
(board.toml + board.local.toml here): `runners` (any agent CLI: an argv template with {prompt}, {project},
{thread}), `env`, `worktrees` (hub project path -> directory here), `writable_roots`, `claude_tool_projects`,
`max_concurrent`, `headless_browser`. From the hub it takes only the assignment: which agent, which project, the thread
id, the prompt text the hub's dispatcher wrote and, for browser-bound work, the origins the hub bound. A project or
runner this machine does not have is reported as a failed start, never improvised. A browser-bound run gets this
machine's own scoped headless browser (dispatch.headless_browser_overrides), allowed exactly the plain origins in the
assignment, writing only into a fresh `<run id>-browser` directory that is removed once the run has ended.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .dispatch import (DispatchConfig, _signal_group, child_env, codex_config_conflict, headless_browser_overrides,
                       plain_origins, probe_process, process_start, remove_browser_output, render_argv, spawn_process,
                       with_headless_browser, with_writable_roots)

log = logging.getLogger("agent_comms.node")

HELLO_SECONDS = 10.0
# Without hub contact this long, the node stops its own runs: the hub counts them lost after
# nodes.NODE_LOST_SECONDS (180 s) and may start a replacement, which must never run beside the original.
SELF_STOP_SECONDS = 90.0     # leaves room for a hung hub call (CALL_TIMEOUT) and the kill grace before 180 s
CALL_TIMEOUT = httpx.Timeout(20, connect=5)
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
        self.unreported: list[str] = []   # runs recover() ended locally, to report once the hub answers

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
        """At startup, before any hub contact: runs an earlier node process on this machine left. Each still-live
        process it can verify is terminated, then killed after the grace period. Their ends are reported once the hub
        answers (tick), so an unreachable hub never leaves them running. Returns their run ids."""
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
            self._remove_browser_output(rid)
        self.unreported = list(left)
        self._save_state()
        return list(left)

    def _flush_unreported(self) -> None:
        """Report the runs recover() stopped. A report the hub refuses (4xx: it no longer knows the run, e.g. pruned
        after a long outage) is dropped; network errors and 5xx are retried next pass. Never blocks the rest of tick."""
        for rid in list(self.unreported):
            try:
                self._call("POST", f"/api/nodes/runs/{rid}", json={
                    "state": "ended", "error": "node restarted while the run was going; its process was stopped"})
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if isinstance(status, int) and 400 <= status < 500:
                    log.warning("hub refused the report for run %s (%s); dropping it", rid, status)
                    self.unreported.remove(rid)
                    continue
                return      # hub unreachable or failing: retry next pass (hello below raises and is logged)
            self.unreported.remove(rid)

    # ------------------------------------------------------------ hub calls

    def _call(self, method: str, path: str, **kw) -> Any:
        r = self.client.request(method, path, **kw)
        r.raise_for_status()
        self.last_contact = self.clock()
        return r.json()

    def _headless_blocker(self, agent: str, runtime: str | None) -> str | None:
        """Why this machine cannot attach the scoped headless browser to this agent's runner, else None."""
        if not self.config.headless_browser_for(agent, runtime):
            return "this machine gives that runner no headless browser ([dispatch.headless_browser])"
        env = child_env(agent, self.config, runtime=runtime)
        if shutil.which(self.config.headless_browser.command, path=env.get("PATH")) is None:
            return "required headless browser command is unavailable"
        return codex_config_conflict(env)

    def headless_runners(self) -> list[str]:
        """The runner keys this machine can give a scoped headless browser now (reported in hello)."""
        return sorted(k for k in self.config.headless_browser.runners
                      if k in self.config.runners and self._headless_blocker(k, k) is None)

    def hello(self) -> dict:
        body = {"projects": sorted(self.config.worktrees), "runners": sorted(self.config.runners),
                "max_concurrent": self.config.max_concurrent}
        headless = self.headless_runners()
        if headless:    # only then: a hub older than this field refuses any hello that carries it
            body["headless_browser"] = headless
        out = self._call("POST", "/api/nodes/hello", json=body)
        self.last_hello = self.clock()
        return out

    def _report(self, run_id: str, state: str, **fields) -> dict | None:
        try:
            return self._call("POST", f"/api/nodes/runs/{run_id}", json={"state": state, **fields})
        except Exception:
            log.exception("could not report run %s as %s", run_id, state)
            return None

    # ------------------------------------------------------------ one pass

    def tick(self) -> None:
        """One pass. Local runs are reaped and held to their timeouts whatever the hub does; hub errors propagate
        after that (the caller logs them and retries)."""
        try:
            self._flush_unreported()
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
            browser = r.get("headless_browser")
            if browser is not None:
                origins = plain_origins((browser or {}).get("origins") or []) if isinstance(browser, dict) else []
                if not origins:
                    raise ValueError("assignment asks for a headless browser with no plain origin")
                blocker = self._headless_blocker(agent, r.get("runtime"))
                if blocker:
                    raise LookupError(blocker)
                self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                out_dir = self.log_dir / f"{rid}-browser"
                self._remove_browser_output(rid)   # left by a node that died before recording this run
                out_dir.mkdir(mode=0o700)   # fresh per run; never reused
                template = with_headless_browser(template, headless_browser_overrides(
                    self.config.headless_browser, origins, str(out_dir)))
            argv = render_argv(template, prompt=prompt, project=cwd, thread_id=int(r.get("thread_id") or 0))
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            child = self.spawner(argv, cwd=cwd, env=child_env(agent, self.config, runtime=r.get("runtime")),
                                 log_path=self.log_dir / f"{rid}.log")
        except Exception as e:
            log.warning("could not start run %s: %s", rid, e)
            self._remove_browser_output(rid)
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
        ack = self._report(rid, "running", pid=getattr(child, "pid", None))
        if isinstance(ack, dict) and ack.get("state") == "ended":
            self._stop(local, "terminate")    # the hub stopped it while we were starting it
        log.info("started %s for thread %s (run %s)", agent, r.get("thread_id"), rid)

    def _remove_browser_output(self, run_id: str) -> None:
        out = self.log_dir / f"{run_id}-browser"
        remove_browser_output({"run_id": run_id, "log": str(self.log_dir / f"{run_id}.log"),
                               "headless_browser": {"output_dir": str(out)}})

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
                self._remove_browser_output(rid)
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
            self._remove_browser_output(rid)
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
                      timeout=CALL_TIMEOUT, trust_env=False) as client:
        node = Node(client, config, log_dir)
        node.recover()      # local cleanup first: it must not depend on reaching the hub
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
