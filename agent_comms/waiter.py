"""`board wait`: the session's side of interactive_wake.py, run by a Claude Code hook with `"asyncRewake": true`.

Claude Code starts the hook in the background when a turn ends (`Stop`) and when the session opens (`SessionStart`),
with the hook's JSON on stdin (`session_id`, `cwd`, `hook_event_name`). This process long-polls the hub's
`POST /api/wait` for that conversation and

- exits 2 with the wake text on stderr when the hub says wake: Claude then wakes the idle session at once and shows
  it that text as a system reminder. The text is the hub's (hosted_wake.wake_text: counts, thread ids and agent
  names) and is passed on only when it has that text's fixed form; nothing else is ever printed;
- exits 0, silently, in every other case: the hub says the feature is off, or that a newer process of the same
  conversation took over (the hook ran again); the hub cannot be reached; the hook's JSON has no conversation id;
  its own time is up (`--max-seconds`, kept below the hook's `timeout` so that Claude never has to end it); the
  Claude Code process that ran the hook is gone (`CLAUDE_PID`, which Claude sets for its hooks, else the parent);
  or it is sent SIGTERM, SIGHUP or SIGINT (Claude ends a hook that way at its timeout and when the session ends).

The agent is `--agent` (the hook passes $AGENT_COMMS_AGENT, as the other hooks do), its token the one `board mcp
--agent` reads; the project is the hook's `cwd`'s checkout and main repository, as `board brief` finds them, unless
`--project` is given. The hub is [server] hub (or AGENT_COMMS_HUB) when this machine is not the hub, else the local
`board serve`. A paused board is not a reason to stop: nothing wakes while it is paused, and the wait goes on.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from typing import Any, Callable

WAKE = 2
CALL_SECONDS = 50.0         # one call's wait at the hub (its maximum is interactive_wake.WAIT_MAX)
RETRY_SECONDS = 120.0       # after the first call: give up once the hub has been unreachable this long
DEFAULT_MAX_SECONDS = 86340.0   # the installed hook's timeout is 86400 (README "Waking a session you opened")
WATCH_SECONDS = 1.0
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TEXT_RE = re.compile(r"agent-comms: [\x20-\x7e]{1,3900}")   # the wake text's fixed start; one printable line


def _hook_input(stdin: Any) -> dict:
    """The hook's JSON on stdin, or {} (no stdin, not JSON, not an object). Never hangs on a pipe left open."""
    import select
    try:
        if stdin is None or stdin.isatty():
            return {}
        try:
            if not select.select([stdin], [], [], 1.0)[0]:
                return {}
        except (OSError, ValueError, TypeError):
            pass    # not a real file descriptor (a test's in-memory stream)
        data = json.loads(stdin.read(1_000_000))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch(environ: dict, leave: Callable[[], None]) -> None:
    """End this process (exit 0) as soon as the Claude Code process that ran the hook is gone."""
    pid = environ.get("CLAUDE_PID", "")
    parent = os.getppid()
    if pid.isdigit() and int(pid) > 1:
        gone = lambda: not _alive(int(pid))     # noqa: E731
    else:
        gone = lambda: os.getppid() != parent   # noqa: E731  (reparented: the parent ended)

    def loop() -> None:
        while not gone():
            time.sleep(WATCH_SECONDS)
        leave()
    threading.Thread(target=loop, daemon=True).start()


def _hub(settings: Any) -> str:
    from . import remote
    hub = remote.hub_url(settings)
    if hub:
        return hub
    host = f"[{settings.host}]" if ":" in settings.host else settings.host
    return f"http://{host}:{settings.port}"


def run(agent: str, projects: list[str] | None = None, max_seconds: float = DEFAULT_MAX_SECONDS, *,
        stdin: Any = None, stderr: Any = None, environ: dict | None = None, client: Any = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
        watch: bool = True) -> int:
    """The exit code (WAKE or 0). Never raises: whatever goes wrong, the session is not woken."""
    try:
        return _run(agent, projects, max_seconds, sys.stdin if stdin is None else stdin,
                    sys.stderr if stderr is None else stderr, os.environ if environ is None else environ,
                    client, sleep, clock, watch)
    except Exception:
        return 0


def _run(agent, projects, max_seconds, stdin, stderr, environ, client, sleep, clock, watch) -> int:
    hook = _hook_input(stdin)
    sid = hook.get("session_id")
    if not isinstance(sid, str) or not UUID_RE.fullmatch(sid):
        return 0
    if watch:
        leave = lambda *_: os._exit(0)      # noqa: E731  silent, whatever the main thread is doing
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, leave)
        _watch(environ, leave)
    cwd = hook.get("cwd") if isinstance(hook.get("cwd"), str) and hook["cwd"].startswith("/") else os.getcwd()
    if not projects:
        from .cli import _repo_roots
        projects = _repo_roots(cwd)
    projects = list(dict.fromkeys(projects))[:8]
    if client is None:
        import httpx
        from .config import Settings, load_agent_token
        token = environ.get("AGENT_COMMS_TOKEN") or load_agent_token(agent)
        client = httpx.Client(base_url=_hub(Settings.load()), trust_env=False,
                              timeout=httpx.Timeout(CALL_SECONDS + 20, connect=5),
                              headers={"Authorization": "Bearer " + token})
    body = {"client_session_id": sid, "projects": projects, "nonce": secrets.token_hex(16), "client": "claude-code"}
    deadline, armed, failing_since, failures = clock() + max_seconds, False, None, 0
    while (left := deadline - clock()) >= 1:
        try:
            r = client.post("/api/wait", json=body | {"wait": min(CALL_SECONDS, left)})
            transient = r.status_code >= 500
            answer = r.json() if r.status_code == 200 else None
        except Exception:
            transient, answer = True, None
        if transient:
            # Unreachable when arming: nothing to wait on. Later (a restart, a sleep, a tailnet blip): try again a
            # while, then give up; the next turn's hook arms again.
            if not armed:
                return 0
            now = clock()
            failing_since = now if failing_since is None else failing_since
            if now - failing_since >= RETRY_SECONDS:
                return 0
            failures += 1
            sleep(min(2.0 ** failures, 15.0))
            continue
        armed, failing_since, failures = True, None, 0
        state = answer.get("state") if isinstance(answer, dict) else None
        if state == "wake":
            text = answer.get("text")
            if isinstance(text, str) and TEXT_RE.fullmatch(text):
                stderr.write(text + "\n")
                stderr.flush()
                return WAKE
            return 0
        if state != "idle":
            return 0        # off, superseded, refused, or an answer this version does not know
    return 0
