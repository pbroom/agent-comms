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
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from . import access
from .dispatch import (DispatchConfig, _signal_group, child_env, codex_config_conflict, headless_browser_overrides,
                       plain_origins, probe_process, process_start, remove_browser_output, render_argv,
                       browser_output_path, make_browser_output,
                       signed_in_browser_overrides, signed_in_profile, spawn_piped, spawn_process, stop_verified,
                       with_headless_browser, with_managed_env, with_writable_roots)
from .node_groups import key
from .node_sessions import SessionRunner
from .session_host import STATE_FILE as SESSION_STATE_FILE, SessionHost

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
    cwd: str | None = None   # the run's checkout, inspected when the run ends (checkout_state)
    browser_out: str | None = None  # the browser output directory this start made (removed when the run ends)
    exit_code: int | None = None    # its leader's, once it went while members of its group were left
    terminating: bool = False       # SIGTERM sent
    key: Any = None                 # (C1, A3) its checkout's registry key, the one its entry was made under


def checkout_state(cwd: str | None) -> dict | None:
    """What the hub needs to let a later run take over this run's work (workstreams._inactive): whether the
    checkout is clean, inspected here because the hub cannot see this machine's disk."""
    if not cwd or not os.path.isdir(cwd):
        return None
    from . import workstreams
    try:
        blocker = workstreams.checkout_blocker(cwd)
    except Exception as e:
        blocker = f"checkout could not be inspected: {type(e).__name__}"
    return {"clean": blocker is None, "detail": (blocker or "")[:300], "path": os.path.realpath(cwd)[:4096]}


class Node:
    def __init__(self, client: httpx.Client, config: DispatchConfig, log_dir: Path,
                 spawner: Callable[..., Any] = spawn_process, clock: Callable[[], float] = time.monotonic,
                 probe: Callable[[Any, Any], str] = probe_process, start_of: Callable[[int], Any] = process_start,
                 signal_group: Callable[[int, int], None] = _signal_group, sleep: Callable[[float], None] = time.sleep,
                 session_spawner: Callable[..., Any] = spawn_piped,
                 session_group_alive: Callable[[int], bool] | None = None):
        self.client, self.config, self.log_dir, self.spawner, self.clock = client, config, log_dir, spawner, clock
        self.probe, self.start_of, self.signal_group, self.sleep = probe, start_of, signal_group, sleep
        self.session_group_alive = session_group_alive
        from .session_host import group_alive
        self.group_alive = session_group_alive or group_alive       # a run's process group, too
        # (Ownership until verified exit, C1) the membership half of the release rule's proof (node_groups
        # `group_absent`); with a group probe of its own (a test's fake processes), absence by that probe
        from .node_groups import group_absent
        self.group_absent = (lambda pgid: not session_group_alive(pgid)) if session_group_alive else group_absent
        # Hosted sessions (node_sessions.py), only with [dispatch.sessions] enabled = true. They share this node's log
        # directory, and have their own loop and thread: tick() below never touches them.
        self.sessions: SessionRunner | None = SessionRunner(
            client, config, log_dir, clock=clock, self_stop_seconds=SELF_STOP_SECONDS, spawner=session_spawner,
            probe=probe, start_of=start_of, signal_group=signal_group, sleep=sleep,
            **({} if session_group_alive is None else {"group_alive": session_group_alive}),
        ) if config.sessions.enabled else None
        # The node's one registry of the process groups it started and has not seen gone (node_groups): its hosted
        # sessions' and its dispatched runs'. Nothing is started in a directory that holds one being stopped.
        from .node_groups import Groups
        self.groups = self.sessions.host.groups if self.sessions is not None else Groups()
        self.running: dict[str, _Local] = {}
        self.waits_said: set[str] = set()   # runs whose wait for a stopping process group the log has said (once each)
        self.report_usage = False       # the hub's last hello answer asked for usage (account_pools.py)
        self.last_hello = float("-inf")
        self.last_contact = clock()
        self.unreported: list[str] = []   # runs recover() ended locally, to report once the hub answers
        self.unreported_cwd: dict[str, str] = {}   # their checkouts, from the state file, inspected for that report

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
                      "proc_start": getattr(l, "proc_start", None), "cwd": l.cwd,
                      **({"browser_out": l.browser_out} if l.browser_out else {})}
                for rid, l in self.running.items()}
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._state_path())

    def recover(self) -> list[str]:
        """At startup, before any hub contact: runs an earlier node process on this machine left. Each still-live
        leader it can verify is terminated, then its group killed after the grace period. Their ends are reported once
        the hub answers (tick). Returns their run ids. (Known limit, C1: a group whose leader is already gone, with
        members left, is not verified here and so not signalled, and its entry is dropped below; restoring such a
        reservation before admitting work is C3's.)"""
        left = self._load_state()
        stop_verified([(d.get("pid"), d.get("proc_start")) for d in left.values() if isinstance(d, dict)],
                      probe=self.probe, signal_group=self.signal_group, sleep=self.sleep,
                      grace=self.config.kill_grace_seconds)
        for rid, d in left.items():
            # The output of each run this node verified from its own state (its process group stopped above): the
            # exact directory the run recorded, else main's name for it. Nothing else in the log folder is touched:
            # a leftover no state names stays (each attempt's fresh name keeps a retry clear of it).
            out = d.get("browser_out") if isinstance(d, dict) else None
            self._remove_browser_output(rid, out if isinstance(out, str) else self.log_dir / f"{rid}-browser")
        self.unreported = list(left)
        self.unreported_cwd = {rid: d["cwd"] for rid, d in left.items()
                               if isinstance(d, dict) and isinstance(d.get("cwd"), str)}
        self._save_state()
        if self.sessions is None:
            self._stop_left_sessions()      # with hosting on, the session runner's own recover() does this (run)
        return list(left)

    def _stop_left_sessions(self) -> None:
        """Hosting is off on this machine now, but an earlier node process may have hosted sessions and died with
        their processes up (node-sessions.json). They are stopped as a hosting node stops them at its start
        (`SessionHost.recover`: only a pid verified by its start time, its group killed, a bounded wait), which leaves
        that file with no entries. Nothing is hosted, advertised or started for this, and with no such file nothing
        is done and nothing is created. A failure here never keeps the node from starting."""
        if not (self.log_dir / SESSION_STATE_FILE).exists():
            return
        try:
            SessionHost(self.config, self.log_dir, clock=self.clock, probe=self.probe, start_of=self.start_of,
                        signal_group=self.signal_group, sleep=self.sleep,
                        **({} if self.session_group_alive is None else {"group_alive": self.session_group_alive}),
                        ).recover()
        except Exception:
            log.exception("could not stop the session processes an earlier node process left")

    def _flush_unreported(self) -> None:
        """Report the runs recover() stopped. A report the hub refuses (4xx: it no longer knows the run, e.g. pruned
        after a long outage) is dropped; network errors and 5xx are retried next pass. Never blocks the rest of tick."""
        for rid in list(self.unreported):
            try:
                c = checkout_state(self.unreported_cwd.get(rid))
                self._call("POST", f"/api/nodes/runs/{rid}", json={
                    "state": "ended", "error": "node restarted while the run was going; its process was stopped",
                    **({"checkout": c} if c is not None else {})})
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
        if self.config.checkout_roots:   # only then: a hub older than this field refuses any hello that carries it
            body["checkout_roots"] = list(self.config.checkout_roots)
        headless = self.headless_runners()
        if headless:    # only then: a hub older than this field refuses any hello that carries it
            body["headless_browser"] = headless
        held = {k: v for k, v in self.config.runner_access.items() if k in self.config.runners}
        if held:        # only then: a hub older than this field refuses any hello that carries it
            body["access"] = held
        if self.config.runner_homes:    # only then (and see below): the account folder each runner uses, path only
            body["homes"] = dict(sorted(self.config.runner_homes.items()))
        if self.sessions is not None:   # only then: a hub older than this field refuses any hello that carries it
            body["sessions"] = {"max_live": self.config.sessions.max_live}
            # This node speaks the child-session protocol (hosted_tree.py). `runners`: for each runner a child
            # session can really be started from here, the access words it can be started at, as session
            # preparation itself decides (session_host.child_words: an adapter for its CLI, a template the adapter
            # accepts, `full` only where this machine allows it). A hub from before the field refuses the hello;
            # hubs are upgraded before nodes.
            # `dirs`: a runner that names its own directory (a Codex runner's `--cd` with a path) can be a session
            # only there. Its words are those it has in that directory, and the hub holds a child of it against
            # the directory the parent runs in.
            from .session_host import child_words, runner_dir
            words = {k: child_words(self.config, template) for k, template in sorted(self.config.runners.items())}
            # 2: spawn too; 3: it also keeps a full-access session's parent up while the hub says results wait for
            # that session (node_sessions `results_waiting`), so a session may be started under a full-access child;
            # 4: it restarts a turn in flight; 5: it steers into one (node_sessions SESSION_PROTOCOL)
            from .node_sessions import SESSION_PROTOCOL
            body["session_host"] = {"tree": SESSION_PROTOCOL, "runners": {k: w for k, w in words.items() if w}}
            dirs = {k: runner_dir(self.config.runners[k]) for k in body["session_host"]["runners"]}
            if any(dirs.values()):      # only then: the key is newer than the rest
                body["session_host"]["dirs"] = {k: d for k, d in dirs.items() if d}
        usage = self.sessions.usage_report() if self.sessions is not None and self.report_usage else {}
        if usage:       # only for a hub whose last answer asked (`report_usage`: it has account pools)
            body["usage"] = usage
        optional = [k for k in ("usage", "homes") if k in body]     # newest first: what an older hub refuses
        while True:
            # Its SEND time: an acknowledged hello renews the sessions' write authority from it. Taken for each
            # attempt, so a hello sent again (an older hub refused a field) is dated from the one that got through
            # (R3-03).
            sent = self.clock()
            try:
                out = self._call("POST", "/api/nodes/hello", json=body)
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 422 or not optional:
                    raise
                field = optional.pop(0)
                body.pop(field)
            if field == "usage":
                # A hub that asked for usage and refuses it now was rolled back: the hello goes again without it, and
                # none is sent until a hub asks again. Usage only orders a pool's members there.
                log.info("the hub refused this node's usage report: it no longer takes one; not sending it")
            elif not getattr(self, "_homes_refused", False):
                # A hub from before account folders refuses the field. The folders are for the human's view only (the
                # node sets them itself), so the hello goes again without them, and the hub is said to be older.
                log.warning("the hub refused this node's account folders ([dispatch.runner_homes]): it runs an "
                            "older version of agent-comms. The runs still use them; only the hub's view lacks them")
                self._homes_refused = True
        # Account pools (account_pools.py): the hub asks for the usage this node's sessions see, only while it has
        # pools; an older hub never does. Read from each answer, so a hub that stops asking gets none.
        self.report_usage = isinstance(out, dict) and out.get("report_usage") is True
        self.last_hello = self.clock()
        if self.sessions is not None:
            self.sessions.note_hello(sent)      # (node_sessions: a steer's write authority, from the send time)
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
            self.waits_said &= {r.get("run_id") for r in runs if r.get("state") == "assigned"}
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
        made = False        # this start made the run's browser output directory: only then may it remove it
        try:
            template = self.config.runner_for(agent, r.get("runtime"))
            if template is None:
                raise LookupError(f"no runner configured on this machine for {agent}")
            cwd = self.config.checkout_for(project)
            if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
                raise FileNotFoundError(f"this machine has no run directory for project {project!r}")
            if self.groups.blocked(cwd):
                # A process group of this node is being stopped in that checkout (a session's or a run's, inside its
                # kill grace or outliving it): the run waits, assigned, and starts on a later pass. (Asked again,
                # atomically, at the spawn below; this first look only spares the side effects in between.)
                self._say_wait(rid, "run %s waits: a process group in %s is still being stopped", cwd)
                return
            prompt = r.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ValueError("assignment has no prompt")
            from . import runner_preflight
            if runner_preflight.scoped(template, project, self.config.claude_tool_projects):
                template = runner_preflight.scoped_template(template)
                prompt += " " + runner_preflight.dispatched_instruction(template)
            signed_in = r.get("browser_access")
            if signed_in is not None:
                # Signed-in browser work: this machine's own [dispatch.runner_access] must still say this runner holds
                # each origin (the hub may have read an older hello). The runner's own config provides the browser.
                held = set(self.config.access_for(agent, r.get("runtime")))
                if (not isinstance(signed_in, list) or not signed_in
                        or any(access.browser(o) not in held for o in signed_in)):
                    raise LookupError("this machine's runner does not declare a signed-in browser for the bound "
                                      "origin ([dispatch.runner_access])")
            profile = signed_in_profile(self.config, agent, r.get("runtime"), signed_in) if signed_in else None
            out_dir = browser_output_path(self.log_dir, rid)    # this attempt's own: never a leftover's name
            fresh = False       # the run's browser output directory is made once the command line is checked
            # The signed-in browser's output directory is writable too, so a file to upload can be copied in.
            template = with_writable_roots(template, self.config.extra_roots(template, project)
                                           + ([str(out_dir)] if profile is not None else []))
            if profile is not None:
                env = child_env(agent, self.config, runtime=r.get("runtime"))
                if shutil.which(self.config.headless_browser.command, path=env.get("PATH")) is None:
                    raise LookupError("required signed-in browser command is unavailable")
                conflict = codex_config_conflict(env)
                if conflict:
                    raise LookupError(conflict)
                fresh = True        # fresh per run; removed when it ends
                template = with_headless_browser(template, signed_in_browser_overrides(
                    self.config.headless_browser, profile, str(out_dir)))
            browser = r.get("headless_browser")
            if browser is not None:
                origins = plain_origins((browser or {}).get("origins") or []) if isinstance(browser, dict) else []
                if not origins:
                    raise ValueError("assignment asks for a headless browser with no plain origin")
                blocker = self._headless_blocker(agent, r.get("runtime"))
                if blocker:
                    raise LookupError(blocker)
                if profile is None:   # else the signed-in browser's fresh directory, shared by both servers
                    fresh = True        # fresh per run; never reused
                template = with_headless_browser(template, headless_browser_overrides(
                    self.config.headless_browser, origins, str(out_dir)))
            template = with_managed_env(template, child_env(agent, self.config, runtime=r.get("runtime")))
            argv = render_argv(template, prompt=prompt, project=cwd, thread_id=int(r.get("thread_id") or 0))
            # Account folders, at the spawn, on the command line as it will run: its cwd, the workspace a Codex
            # runner names itself and every writable root it is given, generated ones included (agent_accounts.py);
            # before the run's browser output directory is made.
            conflict = self.config.launch_blocker(agent, r.get("runtime"), cwd, argv=argv)
            if conflict:
                raise LookupError(conflict)
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if fresh:
                make_browser_output(self.config, out_dir)   # validated, then made exclusively
                made = True
            k = key(cwd)
            started, child = self.groups.start(cwd, ("run", rid), lambda: self.spawner(
                argv, cwd=cwd, env=child_env(agent, self.config, runtime=r.get("runtime")),
                log_path=self.log_dir / f"{rid}.log"), k=k)
            if not started:
                self._say_wait(rid, "run %s waits: a process group in %s is now being stopped", cwd)
                if made:
                    self._remove_browser_output(rid, out_dir)
                return
        except Exception as e:
            log.warning("could not start run %s: %s", rid, e)
            if made:
                self._remove_browser_output(rid, out_dir)
            self._report(rid, "ended", error=f"{type(e).__name__}: {e}"[:300])
            return
        self.waits_said.discard(rid)
        local = _Local(rid, child, self.clock(), float(r.get("timeout_seconds") or 1800),
                       float(r.get("kill_grace_seconds") or 10), cwd=cwd, browser_out=str(out_dir) if made else None,
                       key=k)
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

    def _say_wait(self, rid: str, text: str, cwd: str) -> None:
        """A run that waits for a stopping process group is asked again on every pass (`poll_seconds`): the log says
        so once for that run (INFO), and on the later passes only at DEBUG."""
        level = logging.DEBUG if rid in self.waits_said else logging.INFO
        self.waits_said.add(rid)
        log.log(level, text, rid, cwd)

    def _remove_browser_output(self, run_id: str, out: Any) -> None:
        """Remove one browser output directory this node made for `run_id` (dispatch.remove_browser_output's
        rules: one of the run's own names, in this log folder, never through a symlink). Nothing for None."""
        if out is None:
            return
        remove_browser_output({"run_id": run_id, "log": str(self.log_dir / f"{run_id}.log"),
                               "headless_browser": {"output_dir": str(out)}})

    def _stop(self, local: _Local, how: str) -> None:
        if not self.groups.may_signal(("run", local.run_id)):
            return      # (C1, R2) HELD: proof-only; no signal and no signal clock
        # Before EITHER signal (a hub's kill can come without its terminate): nothing starts in its checkout until its
        # whole group is gone (node_groups).
        self.groups.stopping(("run", local.run_id))
        if local.terminated_at is None:
            local.terminated_at = self.clock()
        if how == "kill" and not local.killed:
            local.killed = True
            local.child.kill()
        elif how != "kill" and not local.terminating:
            local.terminating = True
            local.child.terminate()

    def _reap(self) -> None:
        """A run is let go of only once its whole process group is gone, as a session's is (session_host `_reap`): a
        leader that exits (on its own, or on SIGTERM) while members of its group are left keeps the run tracked, its
        group stopped (SIGTERM, then SIGKILL after the grace) and its checkout held in the registry (node_groups)."""
        now = self.clock()
        for rid, local in list(self.running.items()):
            code = local.child.poll()
            if code is not None and self.group_alive(local.child.pid) and self.groups.may_signal(("run", rid)):
                if local.exit_code is None:
                    local.exit_code = code
                if local.terminated_at is None:
                    log.warning("run %s: its process exited (code %s) and left members of its group: stopping them",
                                rid, code)
                    self._stop(local, "terminate")
                elif not local.killed and now - local.terminated_at >= local.grace:
                    self._stop(local, "kill")
                continue
            if code is not None:
                if not self._release(rid, local):
                    continue    # (C1) its group is not proved gone: kept, tracked and on record; the next pass asks again
                self._save_state()
                self._remove_browser_output(rid, local.browser_out)
                self._report(rid, "ended", exit_code=local.exit_code if local.exit_code is not None else code,
                             **({"checkout": c} if (c := checkout_state(local.cwd)) is not None else {}))
            elif local.terminated_at is None and now - local.started >= local.timeout:
                self._stop(local, "terminate")
            elif (local.terminated_at is not None and not local.killed
                  and now - local.terminated_at >= local.grace):
                self._stop(local, "kill")

    def stop_all(self) -> None:
        """Shutdown: terminate every run, wait the grace period, kill what remains, report each ended."""
        self.signal_runs()
        deadline = self.clock() + self.config.kill_grace_seconds
        while self.runs_alive() and self.clock() < deadline:
            self.sleep(0.2)
        self.kill_runs()
        self.report_stopped()

    # stop_all's steps, so that shut_down can stop hosted sessions within the same grace period and before any report.

    def signal_runs(self) -> None:
        for local in self.running.values():
            self._stop(local, "terminate")

    def runs_alive(self) -> bool:
        return any(l.child.poll() is None for l in self.running.values())

    def kill_runs(self) -> None:
        for local in self.running.values():
            if local.child.poll() is None:
                self._stop(local, "kill")

    def _release(self, rid: str, local: _Local) -> bool:
        """(Ownership until verified exit, C1) The dispatched run's side of THE release rule (`Groups.release`), for
        its reap and the shutdown's `report_stopped`: the leader's exit observed by its own handle AND a complete,
        successful membership observation of its group with no member. Proved: the run leaves `running` (and so
        its state entry, at the next save) with its registry entry. Not proved: everything is kept."""
        token = ("run", rid)
        was = token in self.groups.held
        fence = (getattr(local, "key", None) or key(local.cwd), local.cwd)
        released = self.groups.release(token,
                                       lambda: local.child.poll() is not None and self.group_absent(local.child.pid),
                                       lambda: self.running.pop(rid, None), fence)
        if not released and not was:
            log.warning("run %s: its process group could not be proved gone: its checkout stays held (no signal is "
                        "sent to it again)", rid)
        return released

    def report_stopped(self) -> None:
        for rid, local in list(self.running.items()):
            if not self._release(rid, local):
                # (C1) its group is not proved gone at shutdown (a member survived its SIGKILL): the run keeps its
                # registry entry (HELD), its handle and its state entry (saved below), and is not reported ended
                # here: the next node process finds it on record (`recover`). That recovery signals only a leader it
                # verifies and then drops the entry: a leaderless survivor is C3's to stop.
                continue
            self._remove_browser_output(rid, local.browser_out)
            c = checkout_state(local.cwd)
            self._report(rid, "ended", exit_code=local.child.poll(), error="node shut down",
                         **({"checkout": c} if c is not None else {}))
        self._save_state()


def shut_down(node: Node) -> None:
    """SIGTERM or Ctrl-C: signal every child, wait one grace period for all of them, kill what remains, and only then
    report. The ordinary runs get what `stop_all` gives them (the same signals, the same grace from their SIGTERM, the
    same reports): this is `stop_all` with the hosted sessions' processes signalled, waited for and killed alongside,
    so a session process that ignores SIGTERM is killed when its grace is over, not after the runs' reports (a report
    can stall for the length of a hub call). Nothing the sessions do can keep the runs from being stopped and
    reported: their steps are guarded, and if stopping the runs itself fails, `stop_all` is still attempted."""
    sessions = node.sessions

    def quietly(step, default=None):
        try:
            return step()
        except Exception:
            log.exception("hosted sessions: a shutdown step failed")
            return default

    stopped = False
    try:
        node.signal_runs()
        deadline = node.clock() + node.config.kill_grace_seconds
        if sessions is not None:
            quietly(sessions.signal)
        while (node.runs_alive() or (sessions is not None and quietly(sessions.alive, False))) \
                and node.clock() < deadline:
            node.sleep(0.2)
        node.kill_runs()
        if sessions is not None:
            quietly(sessions.kill)
        stopped = True
    finally:
        try:
            if stopped:
                node.report_stopped()
            else:
                node.stop_all()
        finally:
            if sessions is not None:
                sessions.shutdown()     # its processes are gone: the bookkeeping, then its reports


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_to_stderr() -> None:
    """`board node`'s log: this package's records at INFO and above to stderr (which launchd writes to node.log),
    with the time and the level. Only the `agent_comms` loggers: the root logger and third-party ones keep their
    levels (httpx and httpcore log each request at INFO, with its URL: those stay out). The package's records do not
    also reach the root's handlers (no line twice, nor bare through Python's last-resort handler). Idempotent, and
    a logging setup already made (a test, a process that embeds the node: a handler on the root or on the package)
    is left alone."""
    pkg = logging.getLogger("agent_comms")
    if pkg.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    pkg.addHandler(handler)
    pkg.setLevel(logging.INFO)
    pkg.propagate = False


def run(hub: str, token: str, *, poll_seconds: float | None = None) -> None:
    from .config import Settings, home
    log_to_stderr()
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
        if node.sessions is not None:
            node.sessions.recover()
        me = node.hello()
        print(f"node {me['node']} connected to {hub}: {len(me['projects'])} project(s), runners "
              f"{', '.join(me['runners']) or 'none'}"
              + (f", hosting up to {config.sessions.max_live} session(s)" if node.sessions is not None else ""),
              flush=True)
        if node.sessions is not None:
            node.sessions.start()   # their own loop: a message starts a turn without waiting for poll_seconds
        try:
            while True:
                try:
                    node.tick()
                except httpx.HTTPError as e:
                    log.warning("hub unreachable: %s", e)
                time.sleep(poll_seconds or config.poll_seconds)
        except KeyboardInterrupt:
            shut_down(node)
        except BaseException:
            if node.sessions is not None:
                node.sessions.shutdown()    # whatever ends this loop, no session process is left running behind it
            raise
