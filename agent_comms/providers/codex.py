"""Codex as a hosted session: `codex app-server` on stdio.

The process stays up and speaks a JSON-RPC-shaped protocol, one JSON object per line in each direction. What the
adapter relies on was observed on codex-cli 0.171.0 (DESIGN_NOTES "Session host: Codex adapter"):
- Messages carry `id` and `method` and no `jsonrpc` field (one is tolerated on input, none comes back). A line that
  is not one whole JSON message is dropped by the server with a note on its stderr; nothing is written before the
  first request, and any request before `initialize` is refused ("Not initialized").
- Handshake: `initialize` (its result names the Codex home), the client notification `initialized`, then
  `thread/start` (Codex issues the thread id, a UUID) or `thread/resume` for a thread an earlier process had. The
  hub's session id is not Codex's: the thread id is this session's `provider_id`, and the node keeps it on disk.
- The thread works in the directory the process is started in. `thread/start` is never given a `cwd`: with one,
  the app-server records that directory as trusted in the user's config.toml (see `session_command`).
  `thread/resume` is given the node's run directory: without it a thread goes on where it was created.
- Nothing is added to the model's instructions: `developerInstructions` would replace the owner's configured ones.
- A turn: `turn/start` answers with the turn's id, then come `turn/started`, `item/completed` for each item and one
  `turn/completed` whose `turn.status` is `completed`, `interrupted` or `failed` (with `turn.error`). The reply is
  the `agentMessage` item with `phase: "final_answer"`; `commentary` items are progress notes written before tool
  calls. `turn/interrupt` ends the turn in flight within the same instant and keeps the process.
- A sub-agent Codex starts has its own thread, and ITS `turn/started`, `item/completed` (a `final_answer` of its
  own) and `turn/completed` arrive on this same connection. Every notification is therefore matched on `threadId`,
  and a turn's end on the turn id as well: another thread's lines mean nothing here.
- With `approvalPolicy: "never"` Codex asks for nothing: a command the sandbox refuses just fails for the model (no
  item says so), and an MCP tool that would need approval fails with "MCP tool call requires approval, but approval
  policy is never" in an `mcpToolCall` item. Under another policy it sends requests (`id` and `method` from the
  server). The adapter always starts threads with `never`, and still answers every such request with a refusal so
  the process never waits for a person: nothing is ever approved from here. Only the kind or tool name of what was
  refused leaves this module, never a command, a path or an argument.
"""
from __future__ import annotations

import json
import os
import re

from . import Event, cap_reply, validate_model, validate_session_id

THREAD_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
FEATURE_RE = re.compile(r"[A-Za-z0-9_]{1,80}")
SANDBOXES = ("read-only", "workspace-write")
# An access word the host decided for the session (session_access.prepare) as the thread's sandbox. `full` is only
# ever configured on a machine that allows it ([dispatch.sessions] allow_full_access).
ACCESS_SANDBOX = {"read-only": "read-only", "workspace": "workspace-write", "full": "danger-full-access"}
DEFAULT_SANDBOX = "read-only"       # `codex exec` without --sandbox
MAX_DENIALS = 50
CLIENT = {"name": "agent-comms", "version": "0.1.0"}
# Streaming notifications the host has no use for: the items arrive whole in `item/completed`.
OPT_OUT = ("item/agentMessage/delta", "item/plan/delta", "item/reasoning/summaryTextDelta",
           "item/reasoning/summaryPartAdded", "item/reasoning/textDelta", "item/commandExecution/outputDelta",
           "item/fileChange/outputDelta", "item/fileChange/patchUpdated", "item/mcpToolCall/progress",
           "command/exec/outputDelta", "process/outputDelta")

BYPASS_REFUSED = ("a Codex runner that turns the sandbox or approvals off (--dangerously-bypass-approvals-and-sandbox, "
                  "--approve-for-me, or the danger-full-access sandbox) cannot be hosted as a session unless this "
                  "node's settings say [dispatch.sessions] allow_full_access = true")
BYPASS_FLAGS = frozenset(("--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
                          "--approve-for-me", "--yolo"))
# What a `codex exec` runner may carry. With a value: kept for app-server (`-c`, `--enable`, `--disable`), turned into
# a `thread/start` parameter (model, sandbox), checked and dropped (`--cd`: the process is started in the run
# directory instead) or dropped as meaningless without exec's own output (`--color`).
VALUE_FLAGS = {"-c": "config", "--config": "config", "-m": "model", "--model": "model", "-s": "sandbox",
               "--sandbox": "sandbox", "-C": "cd", "--cd": "cd", "--enable": "enable", "--disable": "disable",
               "--color": "color"}
BARE_KEPT = frozenset(("--strict-config",))
BARE_DROPPED = frozenset(("--skip-git-repo-check", "--json"))    # app-server needs no git repository; its output is fixed

NO_ROLLOUT = "no rollout found for thread id"       # thread/resume's error for a thread Codex has no record of
NEW_THREAD = "The next message starts a NEW thread, without the earlier turns"
NEEDS_APPROVAL = "requires approval"                # in an mcpToolCall's error under approvalPolicy never
NO_SURFACE = "agent-comms hosts this session without an approval surface: nothing is approved here"
# Requests the server may send, each answered at once with its refusal. The name is what is reported as denied.
DECLINES = {
    "item/commandExecution/requestApproval": ("commandExecution", {"decision": "decline"}),
    "item/fileChange/requestApproval": ("fileChange", {"decision": "decline"}),
    "item/permissions/requestApproval": ("permissions", {"permissions": {}}),       # grants nothing
    "mcpServer/elicitation/request": ("mcpElicitation", {"action": "decline"}),
    "item/tool/requestUserInput": ("requestUserInput", {"answers": {}}),
    "execCommandApproval": ("commandExecution", {"decision": {"denied": {"rejection": NO_SURFACE}}}),
    "applyPatchApproval": ("fileChange", {"decision": {"denied": {"rejection": NO_SURFACE}}}),
}


def _line(message: dict) -> bytes:
    # json.dumps escapes every line break inside a string: one message is exactly one line.
    return json.dumps(message).encode() + b"\n"


def _unquoted(value: str) -> str:
    return value.strip().strip("\"'")


def session_command(template: list[str], model: str | None = None, cwd: str | None = None) -> tuple[list[str], dict]:
    """A `codex exec` runner template as a hosted session: the `codex app-server` argv and the `thread/start`
    parameters. Every `-c` override, `--enable` and `--disable` is kept; `-m/--model` and `--sandbox` become thread
    parameters (`model` from the caller wins); `exec`, the {prompt} element and exec's own output flags are dropped.
    Anything else is refused: a flag this adapter does not understand is never passed on.

    `--cd` is never sent to Codex. A `thread/start` that names a `cwd` makes the app-server record that directory
    as TRUSTED in the user's config.toml when the sandbox is workspace-write (seen live on 0.171.0), and with trust
    comes the checkout's own Codex config. `codex exec` leaves `cwd` out of its own `thread/start`, and so does a
    hosted session: its process is started in the run directory (`cwd`, the session host's), which is then the
    thread's. So `--cd` may only say that directory: `{project}`, or the same path."""
    if not template or os.path.basename(template[0]) != "codex":
        raise ValueError("not a Codex runner")
    model = validate_model(model)
    kept: list[str] = []
    values: dict[str, str] = {}
    exec_seen = False
    i = 1
    while i < len(template):
        arg = template[i]
        i += 1
        if arg == "{prompt}":
            continue                # turns arrive on stdin
        if arg in ("exec", "e") and not exec_seen:
            exec_seen = True
            continue
        if arg.split("=", 1)[0] in BYPASS_FLAGS:
            raise ValueError(BYPASS_REFUSED)
        if arg in BARE_KEPT:
            kept.append(arg)
            continue
        if arg in BARE_DROPPED:
            continue
        flag, eq, value = arg.partition("=") if arg.startswith("--") else (arg[:2], "=" if len(arg) > 2 else "",
                                                                         arg[2:].removeprefix("="))
        kind = VALUE_FLAGS.get(flag)
        if kind is None:
            raise ValueError("this Codex runner has an argument a hosted session does not understand: "
                             + (flag if arg.startswith("-") else "an extra positional argument"))
        if not eq:
            if i >= len(template):
                raise ValueError(f"this Codex runner's {flag} has no value")
            value, i = template[i], i + 1
        if kind == "config":
            key, sep, val = value.partition("=")
            key = key.strip()
            if not sep or not key:
                raise ValueError("this Codex runner has a -c override that is not key=value")
            if key == "sandbox_mode":
                if _unquoted(val) == "danger-full-access":
                    raise ValueError(BYPASS_REFUSED)
                values["config_sandbox"] = _unquoted(val)       # as in Codex, the last one counts
            kept += ["-c", value]
        elif kind in ("enable", "disable"):
            if not FEATURE_RE.fullmatch(value):
                raise ValueError(f"this Codex runner's --{kind} does not name a feature")
            kept += [f"--{kind}", value]
        elif kind != "color":
            values[kind] = value
    if not exec_seen:
        raise ValueError("not a `codex exec` runner")
    sandbox = values.get("sandbox", values.get("config_sandbox", DEFAULT_SANDBOX))
    if sandbox == "danger-full-access":
        raise ValueError(BYPASS_REFUSED)
    if sandbox not in SANDBOXES:
        raise ValueError("this Codex runner's sandbox must be one of " + ", ".join(SANDBOXES))
    params: dict = {"sandbox": sandbox, "approvalPolicy": "never"}
    model = model if model is not None else validate_model(values.get("model"))
    if model is not None:
        params["model"] = model
    cd = values.get("cd")
    if cd is not None and cd != "{project}" and (cwd is None or os.path.normpath(cd) != os.path.normpath(cwd)):
        raise ValueError("this Codex runner's --cd must be {project}: a hosted session works in the run directory "
                         "its node starts it in")
    return [template[0], "app-server", *kept, "--listen", "stdio://"], params


def _name(value) -> str:
    return value if isinstance(value, str) and NAME_RE.fullmatch(value) else "unknown"


def _error_text(error) -> str:
    """A JSON-RPC error's or a turn error's message as one short line."""
    message = error.get("message") if isinstance(error, dict) else None
    return " ".join(message.split())[:200] if isinstance(message, str) and message.strip() else "no reason given"


class CodexSession:
    waits_for_ready = True      # initialize and thread/start (or thread/resume) come before any turn

    def __init__(self):
        self.session_id: str | None = None
        self.thread_id: str | None = None       # Codex's id for this session: adopted, or issued by thread/start
        self._cwd: str | None = None            # where the host starts this session's process (`runs_in`)
        self._params: dict = {}
        self._resume = False
        self._gone = False                      # the thread this process was told to resume cannot be resumed
        self._pending: dict[int, str] = {}      # request id -> its method, until answered
        self._start_id: int | None = None       # the id of the turn/start request of the turn in flight
        self._access: str | None = None         # the access word whose sandbox the host decided on (`configure`)
        self._effort: str | None = None
        self._reset()

    def _reset(self) -> None:
        self._next_id = 0
        self._pending = {}
        self._ready = self._dead = False
        self._own_turn: str | None = None       # a turn Codex began in this thread that no message here started
        self._end_turn()

    def _end_turn(self) -> None:
        # The turn's own request is retired with it: its answer, should it still come, belongs to no later turn.
        self._pending.pop(self._start_id, None)
        self._start_id = None
        self._turn_open = self._started = False
        self._turn_id: str | None = None
        self._final: str | None = None          # the last final_answer of the turn
        self._plain: str | None = None          # the last message that is not commentary, for a model without phases
        self._denials: list[str] = []

    # ------------------------------------------------------------ the seam

    def adopt(self, provider_id: str | None) -> None:
        self.thread_id = provider_id if isinstance(provider_id, str) and THREAD_ID_RE.fullmatch(provider_id) else None

    def runs_in(self, cwd: str) -> None:
        self._cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None

    def configure(self, *, access: str | None = None, effort: str | None = None) -> None:
        """What the host decided for this session (session_access.prepare), before any `argv`. `access`: the
        thread's sandbox whatever the runner says (ACCESS_SANDBOX); approvals stay off either way. `effort`: sent
        with every `turn/start`."""
        if access is not None and access not in ACCESS_SANDBOX:
            raise ValueError("unknown access for a Codex session")
        self._access, self._effort = access, effort

    def argv(self, template: list[str], *, session_id: str, resume: bool, model: str | None) -> list[str]:
        self.session_id = validate_session_id(session_id)
        argv, self._params = session_command(template, model, self._cwd)
        if self._access is not None:
            self._params["sandbox"] = ACCESS_SANDBOX[self._access]
        self._resume, self._gone = bool(resume), False
        self._reset()
        return argv

    def on_open(self) -> list[bytes]:
        return [self._request("initialize", {"clientInfo": CLIENT,
                                             "capabilities": {"optOutNotificationMethods": list(OPT_OUT)}})]

    def turn(self, text: str) -> list[bytes]:
        self._end_turn()
        self._turn_open = True
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
        if self._effort is not None:
            params["effort"] = self._effort     # "for this turn and subsequent turns": sent each time (a resume too)
        data = self._request("turn/start", params)
        self._start_id = self._next_id
        return [data]

    def holds_turns(self) -> bool:
        """Codex is running a turn of its own in this thread (one no message here started). A `turn/start` written
        now would not start a turn: Codex adds its text to the turn in flight and answers with THAT turn, whose
        result is not the message's. The host keeps the next message until that turn is over."""
        return self._own_turn is not None and not self._dead

    def interrupt(self) -> list[bytes] | None:
        """`turn/interrupt` for the turn in flight, once Codex has named it; before that the host stops the process."""
        if not self._turn_open or self._turn_id is None or self._dead:
            return None
        return [self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self._turn_id})]

    def session_exists(self, stderr: str) -> bool | None:
        """False once Codex refused to resume the thread this process was told to resume (or this machine had no
        thread id to give it): the next process starts a new thread. Codex never refuses a start because a thread
        exists, so this is never True."""
        return False if self._gone else None

    def feed(self, line: bytes) -> tuple[list[Event], list[bytes]]:
        if self._dead:
            return [], []
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return [], []
        if not isinstance(msg, dict):
            return [], []
        rid, method = msg.get("id"), msg.get("method")
        has_id = isinstance(rid, (int, str)) and not isinstance(rid, bool)
        if isinstance(method, str):
            params = msg["params"] if isinstance(msg.get("params"), dict) else {}
            return self._asked(rid, method) if has_id else self._notified(method, params)
        if has_id and ("result" in msg or "error" in msg):
            return self._answered(rid, msg)
        return [], []

    # ------------------------------------------------------------ what the adapter writes

    def _request(self, method: str, params: dict) -> bytes:
        self._next_id += 1
        self._pending[self._next_id] = method
        return _line({"id": self._next_id, "method": method, "params": params})

    def _start_failed(self, detail: str) -> tuple[list[Event], list[bytes]]:
        self._dead = True       # the host stops this process; nothing more of it is read
        return [Event("start_failed", detail=detail[:300])], []

    def _fatal(self, detail: str) -> tuple[list[Event], list[bytes]]:
        self._dead = True
        return [Event("fatal", detail=detail)], []

    def _deny(self, name: str) -> None:
        if name not in self._denials and len(self._denials) < MAX_DENIALS:
            self._denials.append(name)

    # ------------------------------------------------------------ what the process wrote

    def _answered(self, rid, msg: dict) -> tuple[list[Event], list[bytes]]:
        method = self._pending.pop(rid, None) if isinstance(rid, int) else None
        if method is None:
            return [], []
        result = msg.get("result")
        failed = "error" in msg or not isinstance(result, dict)
        why = _error_text(msg.get("error"))
        if method == "initialize":
            if failed:
                return self._start_failed(f"Codex refused the handshake: {why}")
            if self._resume and self.thread_id is None:
                self._gone = True
                return self._start_failed(
                    "this node has no record of the Codex thread for this session, so its conversation cannot be "
                    "resumed. " + NEW_THREAD)
            if self._resume:
                # The run directory as this node resolves it NOW: left out, Codex would go on in the directory the
                # thread was created in, which may be an old checkout. (Unlike on thread/start, a `cwd` here does
                # not make Codex record the directory as trusted: seen live on 0.171.0.)
                where = {"cwd": self._cwd} if self._cwd else {}
                start = self._request("thread/resume",
                                      {"threadId": self.thread_id, "excludeTurns": True} | self._params | where)
            else:
                start = self._request("thread/start", dict(self._params))
            return [], [_line({"method": "initialized"}), start]
        if method in ("thread/start", "thread/resume"):
            if failed and method == "thread/resume":
                # Whatever the reason, this thread is not tried again: a refusal that does not go away would
                # otherwise fail every later message the same way. `_gone` is set before the failure is reported,
                # so the host records the fresh start first (session_exists).
                lost, self.thread_id, self._gone = self.thread_id, None, True
                reason = "it has no record of it (no rollout found)" if NO_ROLLOUT in why else why
                return self._start_failed(f"Codex could not resume thread {lost}: {reason[:120]}. " + NEW_THREAD)
            if failed:
                return self._start_failed(f"Codex could not start the thread: {why}")
            thread = result.get("thread")
            tid = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(tid, str) or not THREAD_ID_RE.fullmatch(tid):
                return self._fatal("Codex reported no usable thread id")
            if method == "thread/resume" and tid.lower() != (self.thread_id or "").lower():
                return self._fatal("the process reported a thread id other than the one it was given")
            self.thread_id, self._ready = tid, True
            return [Event("ready", provider_id=tid)], []
        if method == "turn/start" and self._turn_open and rid == self._start_id:
            # Only the answer to the request of the turn in flight (an earlier turn's request was retired when that
            # turn ended, so its late answer never names this one).
            if failed:
                denials = tuple(self._denials)
                self._end_turn()
                return [Event("turn_ended", ok=False, denials=denials, detail=f"Codex refused the turn: {why}")], []
            turn = result.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self._turn_id = turn["id"]
        return [], []       # turn/interrupt's answer, either way: the turn's own end says what happened

    def _asked(self, rid, method: str) -> tuple[list[Event], list[bytes]]:
        """A request from the server. An approval is refused and reported by its kind; anything else the adapter
        cannot serve is answered with an error, so the process never waits on this side."""
        if method in DECLINES:
            name, result = DECLINES[method]
            self._deny(name)
            return [], [_line({"id": rid, "result": result})]
        if method == "item/tool/call":      # a tool this client would have to run: it offers none
            return [], [_line({"id": rid, "result": {"contentItems": [], "success": False}})]
        return [], [_line({"id": rid, "error": {"code": -32601, "message": NO_SURFACE}})]

    def _notified(self, method: str, params: dict) -> tuple[list[Event], list[bytes]]:
        # Only this session's thread. A sub-agent's thread reports its own turns and answers on this connection.
        if not self._ready or params.get("threadId") != self.thread_id:
            return [], []
        if not self._turn_open:
            self._between_turns(method, params)
            return [], []
        if method == "turn/started":
            turn = params.get("turn")
            tid = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(tid, str):
                return [], []
            if self._turn_id is None:
                self._turn_id = tid
            if tid != self._turn_id or self._started:
                return [], []
            self._started = True
            return [Event("turn_started")], []
        if method == "item/completed":
            if self._turn_id is not None and params.get("turnId") not in (None, self._turn_id):
                return [], []
            item = params.get("item")
            if isinstance(item, dict):
                self._item(item)
            return [], []
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("status") == "inProgress":
                return [], []
            if self._turn_id is not None and turn.get("id") != self._turn_id:
                return [], []
            return [self._ended(turn)], []
        return [], []

    def _between_turns(self, method: str, params: dict) -> None:
        """This thread, with no message's turn in flight: Codex may begin a turn by itself (it goes on with a goal,
        runs a queued prompt). Nothing of it is reported; only that it is running is kept (`holds_turns`), until its
        own end or the thread saying it is idle."""
        if method in ("turn/started", "turn/completed"):
            turn = params.get("turn")
            tid = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(tid, str):
                return          # no turn object, or one without an id: it says nothing (and must not raise)
            if method == "turn/started":
                self._own_turn = tid
            elif tid == self._own_turn and turn.get("status") != "inProgress":
                self._own_turn = None
        elif method == "thread/status/changed":
            status = params.get("status")
            if isinstance(status, dict) and status.get("type") == "idle":
                self._own_turn = None

    def _item(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "agentMessage":
            if isinstance(item.get("text"), str):
                if item.get("phase") == "final_answer":
                    self._final = item["text"]
                elif item.get("phase") != "commentary":
                    self._plain = item["text"]
        elif item.get("status") == "declined":
            self._deny(_name(kind))
        elif kind == "mcpToolCall" and item.get("status") == "failed":
            error = item.get("error")
            if isinstance(error, dict) and NEEDS_APPROVAL in str(error.get("message")):
                self._deny(_name(f"mcp__{item.get('server')}__{item.get('tool')}"))

    def _ended(self, turn: dict) -> Event:
        status, error = turn.get("status"), turn.get("error")
        ok = status == "completed"
        text = self._final
        if text is None:        # the turn's own summary, should the items not have come one by one
            items = [x for x in turn.get("items") or [] if isinstance(x, dict) and x.get("type") == "agentMessage"
                     and isinstance(x.get("text"), str)] if isinstance(turn.get("items"), list) else []
            final = [x["text"] for x in items if x.get("phase") == "final_answer"]
            plain = [x["text"] for x in items if x.get("phase") not in ("final_answer", "commentary")]
            text = final[-1] if final else self._plain if self._plain is not None else plain[-1] if plain else ""
        meta: dict = {}
        if isinstance(status, str) and len(status) <= 40:
            meta["status"] = status
        if isinstance(turn.get("durationMs"), int) and not isinstance(turn.get("durationMs"), bool):
            meta["duration_ms"] = turn["durationMs"]
        info = error.get("codexErrorInfo") if isinstance(error, dict) else None
        info = next(iter(info), None) if isinstance(info, dict) else info
        if isinstance(info, str) and len(info) <= 80:
            meta["error_info"] = info
        detail = "" if ok else (_error_text(error) if isinstance(error, dict)
                                else "the turn was interrupted" if status == "interrupted"
                                else "Codex reported that the turn failed")
        event = Event("turn_ended", ok=ok, meta=meta, denials=tuple(self._denials), detail=detail,
                      text=cap_reply(text))
        self._end_turn()
        return event
