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
Restart (measured on codex-cli 0.171.0, DESIGN_NOTES "Restarting a running turn"):
- `turn/interrupt` with the turn's id: `{}`, then the turn's `turn/completed` with status `interrupted`
  (`interrupt_turn`, `interrupted`); a `turn/start` written after that starts a NEW turn as usual (a restart's
  message). An `interrupted` end counts as the restart's only once Codex answered THAT `turn/interrupt` request with
  success and the turn that ended is the one it named (R5-01): Codex ends a turn `interrupted` for other causes too,
  and a rejected or unanswered request interrupted nothing.
Mid-turn steering (DESIGN_NOTES "Mid-turn steering, loose contract", part F; measured on codex-cli 0.171.0, gate M5
in "Mid-turn steering, step 2: Codex steering, as built"):
- Every `turn/start` carries `clientUserMessageId` (a fresh random id for the turn's message, N): Codex echoes it as
  the `clientId` of the `userMessage` item in the turn that took the input, on a fresh start (the answer names a new
  turn T, then the item comes in T) and on a join (a `turn/start` while a turn is active is answered with THAT turn,
  and the item comes in it later, once the model reads it).
- A steer is written only for a turn PROVED to be N's: the answer to N's exact `turn/start` request names T and a
  `userMessage` item with N's `clientId` was reported in T, in either order (`owns`). Then `turn/steer {threadId,
  expectedTurnId: T, input}` (`steer`): Codex enforces the target, and answers `{turnId}` at once, or refuses with
  -32600 ("no active turn to steer", "expected active turn id `X` but found `Y`", "expectedTurnId must not be
  empty"). What that very request was answered before N's `turn/completed` is kept for the host (`steer_result`); a
  later answer is logged and changes nothing. Never `next_turn`: a refused or unanswered steer is never sent again.
Plan usage (account_pools.py; from the protocol schema codex-cli 0.171.0 generates, `codex app-server
generate-json-schema`, and checked against lines recorded from real hosted sessions on 2026-11-22/23,
tests/fixtures/rate_limit_samples.jsonl: `limitId` "codex", `primary` a 10080-minute window, `secondary` null:
DESIGN_NOTES "Account pools"). `primary` and `secondary` may each be null:
- The server notification `account/rateLimits/updated` (`{"rateLimits": {...}}`, a sparse rolling update to merge
  into the last one; no `threadId`: it is the account's): `primary` and `secondary` windows, each `usedPercent`
  (0-100), `resetsAt` (epoch seconds) and `windowDurationMins`; `limitId` names the bucket; `rateLimitReachedType`
  says a limit is reached. It is not among the notifications this client opts out of. `account/rateLimits/read`
  would ask for the same; this adapter makes no such request. Each window it carries is one observation (the host
  merges them, each keeping its own time); its percentage, however high, is only a percentage.
- A turn that fails on the plan's limit: `turn/completed` with `turn.error.codexErrorInfo` `usageLimitExceeded`. This,
  and nothing else (not `rateLimitReachedType`, not 100%), marks the account exhausted (the owner's ruling).
"""
from __future__ import annotations

import json
import os
import re

from . import FAILED_TURN, Event, cap_reply, number, usage_record, validate_model, validate_session_id
from .. import hostlog

log = hostlog.get("agent_comms.providers.codex")     # (host-side logging: every line a deferred record, hostlog)
# Codex's refusals of a `turn/steer` before it admitted it (-32600, measured on 0.171.0): the steer never reached the
# turn, so it is not delivered.
STEER_NO_TURN = "no active turn to steer"
# "expected active turn id `X` but found `Y`": evidence only when X is the very id that request named and Y is a
# nonempty id other than X (a refusal that found the very turn it expected contradicts itself: no evidence, R2-01).
STEER_OTHER_TURN = re.compile(r"expected active turn id `([^`]*)` but found `([^`]*)`")
# ("expectedTurnId must not be empty" is Codex's third refusal; this adapter never sends an empty id, so as an answer
# to one of its requests it is no evidence of anything.)
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


def _steer_answer(msg: dict, steer: dict) -> str:
    """What one answer to a `turn/steer` request is evidence of: "ok" (a success naming the very turn the request
    named), "refused" (Codex's -32600 refusal of that request), or "other" (anything else, which is no evidence:
    `unconfirmed`). Only an exclusive, well-formed answer (exactly one of `result` and `error`, nothing else but `id`
    and a `jsonrpc` that is absent or exactly "2.0") read after every byte of the request was written; an integer
    code; a quoted expected id equal to the request's and a found id that is nonempty and different (R2-01)."""
    if not steer.get("written") or set(msg) - {"id", "result", "error", "jsonrpc"} or ("result" in msg) == (
            "error" in msg) or msg.get("jsonrpc", "2.0") != "2.0" or not _proof_id(steer.get("turn")):
        return "other"
    if "result" in msg:
        result = msg["result"]
        ok = (isinstance(result, dict) and set(result) == {"turnId"} and isinstance(result["turnId"], str)
              and result["turnId"] == steer["turn"])
        return "ok" if ok else "other"
    err = msg["error"]
    if (not isinstance(err, dict) or type(err.get("code")) is not int or err["code"] != -32600
            or not isinstance(err.get("message"), str) or set(err) - {"code", "message", "data"}):
        return "other"
    if err["message"] == STEER_NO_TURN:
        return "refused"
    quoted = STEER_OTHER_TURN.fullmatch(err["message"])
    return ("refused" if quoted is not None and quoted.group(1) == steer["turn"] and quoted.group(2)
            and quoted.group(2) != quoted.group(1) else "other")


def _proof_id(value) -> bool:
    """A turn id that can be half of a steer's proof: a nonempty string (R2-01)."""
    return isinstance(value, str) and value != ""


def _fresh_id() -> str:
    """A random id in uuid4's form, from os.urandom (never uuid.uuid4, which a test may make deterministic)."""
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _line(message: dict) -> bytes:
    # json.dumps escapes every line break inside a string: one message is exactly one line.
    return json.dumps(message).encode() + b"\n"


def _unquoted(value: str) -> str:
    return value.strip().strip("\"'")


def _arguments(template: list[str]):
    """A `codex exec` runner's arguments after the executable, as this adapter reads them: (kind, flag, value) with
    kind one of VALUE_FLAGS' kinds ("config" for `-c`/`--config` in every form Codex takes: `-c k=v`, `-ck=v`,
    `-c=k=v`, `--config k=v`, `--config=k=v`), "exec" (the subcommand, once) or "bare" (BARE_KEPT). The prompt
    element and BARE_DROPPED are skipped; anything else, and a bypass flag, raises."""
    exec_seen = False
    i = 1
    while i < len(template):
        arg = template[i]
        i += 1
        if arg == "{prompt}":
            continue                # turns arrive on stdin
        if arg in ("exec", "e") and not exec_seen:
            exec_seen = True
            yield "exec", arg, None
            continue
        if arg.split("=", 1)[0] in BYPASS_FLAGS:
            raise ValueError(BYPASS_REFUSED)
        if arg in BARE_KEPT:
            yield "bare", arg, None
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
        yield kind, flag, value


def config_values(template: list[str]) -> list[str]:
    """The runner's `-c` override values (`key=value`), in order, in whichever form each was written: the same
    reading as `session_command`'s (session_host.bound_template merges with them). Raises as it does."""
    return [value for kind, _, value in _arguments(template) if kind == "config"]


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
    for kind, arg, value in _arguments(template):
        if kind == "exec":
            exec_seen = True
            continue
        if kind == "bare":
            kept.append(arg)
            continue
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


def named_directory(template: list[str]) -> str | None:
    """The directory a `codex exec` runner names with `-C`/`--cd`, when it is a path of its own (not `{project}`):
    the only directory a hosted session of that runner can work in (`session_command`). None: it names none."""
    cd, i = None, 1
    while i < len(template):
        arg = template[i]
        i += 1
        if arg.startswith("--"):
            flag, eq, value = arg.partition("=")
        elif arg.startswith("-") and len(arg) >= 2:
            flag, eq, value = arg[:2], "=" if len(arg) > 2 else "", arg[2:].removeprefix("=")
        else:
            continue
        kind = VALUE_FLAGS.get(flag)
        if kind is None:
            continue
        if not eq:
            if i >= len(template):
                break
            value, i = template[i], i + 1
        if kind == "cd":
            cd = value
    return os.path.normpath(cd) if cd is not None and cd != "{project}" and os.path.isabs(cd) else None


def _name(value) -> str:
    return value if isinstance(value, str) and NAME_RE.fullmatch(value) else "unknown"


def _window_key(limit_id: str, slot: str) -> str:
    """A rate-limit window's identity (DESIGN_NOTES "Account pools", the usage record's rules): its bucket
    (`limitId`, as a plain word; `codex` when none) and its slot (primary, secondary). Its length is how it is shown
    (`_window_label`), never part of what it is: an update that leaves the length out is the same window."""
    bucket = re.sub(r"[^a-z0-9_]", "_", limit_id.lower())[:24] if limit_id else "codex"
    return f"{bucket}_{slot}"


def _window_label(minutes: float | None) -> str | None:
    """How a window is shown: by its length (five_hour, seven_day, else <minutes>_min), or nothing."""
    known = {300.0: "five_hour", 10080.0: "seven_day"}
    return known.get(minutes) or (f"{int(minutes)}_min" if minutes is not None and 0 < minutes < 1e7 else None)


def _error_text(error) -> str:
    """A JSON-RPC error's or a turn error's message as one short line."""
    message = error.get("message") if isinstance(error, dict) else None
    return " ".join(message.split())[:200] if isinstance(message, str) and message.strip() else "no reason given"


class CodexSession:
    waits_for_ready = True      # initialize and thread/start (or thread/resume) come before any turn
    reports_sandbox = True      # ... and their answer names the thread's sandbox: `ready` carries it (`mode`)
    steers = True               # `turn/steer` into a turn proved to be the message's (module text, `owns`)
    restarts = True             # turn/interrupt, whose turn then ends `interrupted`

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
        self._limits: dict[str, dict] = {}      # window -> its last observation (`_rate_limits`), for `_exhausted`
        self._reset()

    def _reset(self) -> None:
        self._next_id = 0
        self._pending = {}
        self._ready = self._dead = False
        self._own_turn: str | None = None       # a turn Codex began in this thread that no message here started
        self._by_interrupt = False              # the last turn that ended was ended by the restart's interrupt
        self._retired: set[int] = set()         # steer requests retired with their turn (a late answer is logged)
        self._steer_final: str | None = None    # what the last ended turn's steer request was answered (`steer_result`)
        self._client_id: str | None = None      # the turn in flight's message id, as `clientUserMessageId`
        self._end_turn()

    def _end_turn(self) -> None:
        # The turn's own requests are retired with it (turn/start, and a restart's turn/interrupt): an answer that
        # still comes belongs to no later turn.
        self._pending.pop(self._start_id, None)
        self._pending.pop(getattr(self, "_restart_id", None), None)
        steer = getattr(self, "_steer", None)
        if steer is not None and self._pending.pop(steer["id"], None) is not None:
            self._retired.add(steer["id"])      # unanswered by the turn's end: an answer that still comes is logged
        self._steer: dict | None = None         # the turn's one `turn/steer`: {"id", "turn", "answer", "written"}
        self._steer_spent = False               # this turn's steer was taken back: no second request in this turn
        self._answer_turn: str | None = None    # the turn the answer to THIS turn/start named (the proof's T)
        self._owned_in: set[str] = set()        # turns in which a userMessage item with this turn's clientId came
        self._start_id = None
        self._turn_open = self._started = False
        self._turn_id: str | None = None
        self._final: str | None = None          # the last final_answer of the turn
        self._plain: str | None = None          # the last message that is not commentary, for a model without phases
        self._denials: list[str] = []
        # A restart's `turn/interrupt` for this turn: its request id, and whether Codex answered that request with
        # success (`interrupted`).
        self._restart_id: int | None = None
        self._restart_ok = False

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

    def turn(self, text: str, kind: str = "user") -> list[bytes]:
        # `kind` has no place in `turn/start` (its input is user items: text here): the text is all Codex gets.
        self._end_turn()
        self._turn_open = True
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
        if self._effort is not None:
            params["effort"] = self._effort     # "for this turn and subsequent turns": sent each time (a resume too)
        # The message's own id, which Codex echoes as the `clientId` of the userMessage item of the turn that took it
        # (the steer's proof, `owns`). Last, so the line is main's with one key added. Random, not uuid4's generator.
        self._client_id = _fresh_id()
        params["clientUserMessageId"] = self._client_id
        self._steer_final = None
        data = self._request("turn/start", params)
        self._start_id = self._next_id
        return [data]

    # ------------------------------------------------------------ steering (module text)

    def owns(self) -> bool:
        """The turn in flight is PROVED to be this message's: the answer to its turn/start named T, and a userMessage
        item with its clientId was reported in T (either order)."""
        return (self._turn_open and not self._dead and _proof_id(self._answer_turn)
                and self._answer_turn in self._owned_in)

    def steer(self, text: str) -> list[bytes] | None:
        """`turn/steer` into the proved turn, once per turn; None while it is not proved (nothing is written)."""
        if not self.owns() or self._steer is not None or self._steer_spent or not isinstance(text, str):
            return None
        data = self._request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self._answer_turn,
                                            "input": [{"type": "text", "text": text}]})
        self._steer = {"id": self._next_id, "turn": self._answer_turn, "answer": None, "written": False}
        return [data]

    def steer_written(self) -> None:
        """The host: every byte of the steer's frame is in the pipe. Only an answer read after this is evidence."""
        if self._steer is not None:
            self._steer["written"] = True

    def steer_retire(self) -> None:
        """The steer's frame was taken back with not one byte of it written: its request id is retired (never
        answered, never sent)."""
        if self._steer is not None:
            self._pending.pop(self._steer["id"], None)
            self._retired.add(self._steer["id"])
            self._steer = None
            self._steer_spent = True    # never a second request in this turn (a recovered gate writes nothing)

    def steer_result(self) -> str | None:
        """What the steer request of the turn that just ended (its `turn/completed`) was answered before that end:
        "ok" (success naming the proved turn), "refused" (one of Codex's three -32600 refusals), "other" (any other
        answer), or None (no answer, or no steer)."""
        return self._steer_final

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

    def takes_interrupt(self) -> bool:
        """Once Codex named the turn (its answer to turn/start, or turn/started), as `interrupt` needs."""
        return self._turn_open and self._turn_id is not None and not self._dead

    def interrupt_turn(self) -> list[bytes] | None:
        """A restart's first step: `turn/interrupt` for the turn in flight (as a timeout's), once Codex named it. Its
        answer is kept (`interrupted`)."""
        data = self.interrupt()
        if data is not None:
            self._restart_id, self._restart_ok = self._next_id, False
        return data

    def interrupted(self, ev: Event) -> bool:
        """The restart's interrupt ended the turn: Codex answered that request with success, and the turn it named
        ended with status `interrupted`."""
        return self._by_interrupt and not ev.ok and ev.meta.get("status") == "interrupted"

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
            if isinstance(rid, int) and rid in self._retired:
                self._retired.discard(rid)
                log.info("a late answer to a turn/steer retired with its turn (request %s): it changes nothing", rid)
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
            # The sandbox Codex says the thread has (`sandbox.type`: readOnly, workspaceWrite, dangerFullAccess).
            sandbox = result.get("sandbox")
            kind = sandbox.get("type") if isinstance(sandbox, dict) else None
            return [Event("ready", provider_id=tid,
                          mode=kind if isinstance(kind, str) and NAME_RE.fullmatch(kind) else None)], []
        if self._steer is not None and rid == self._steer["id"]:
            # The steer's own request (`steer`): kept, for N's end (`_steer_answer`).
            self._steer["answer"] = _steer_answer(msg, self._steer)
            return [], []
        if method == "turn/interrupt" and rid == self._restart_id:
            self._restart_ok = not failed       # the restart's own request: only a success counts (`interrupted`)
            return [], []
        if method == "turn/start" and self._turn_open and rid == self._start_id:
            # Only the answer to the request of the turn in flight (an earlier turn's request was retired when that
            # turn ended, so its late answer never names this one).
            if failed:
                # Codex refused to START the turn: it never took it, so this end is no answer (Event.answered).
                denials = tuple(self._denials)
                self._end_turn()
                return [Event("turn_ended", ok=False, denials=denials, detail=f"Codex refused the turn: {why}",
                              answered=False)], []
            turn = result.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self._turn_id = turn["id"]
                # (the proof's T: only from the answer to this very request, and only a nonempty id: R2-01)
                self._answer_turn = turn["id"] if _proof_id(turn["id"]) else None
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
        if method == "account/rateLimits/updated":     # the account's, not a thread's (module text): advisory
            return self._rate_limits(params.get("rateLimits")), []
        # Only this session's thread. A sub-agent's thread reports its own turns and answers on this connection.
        if not self._ready or params.get("threadId") != self.thread_id:
            return [], []
        if not self._turn_open:
            self._between_turns(method, params)
            return [], []
        if method in ("item/started", "item/completed"):
            item = params.get("item")
            if (isinstance(item, dict) and item.get("type") == "userMessage" and self._client_id is not None
                    and item.get("clientId") == self._client_id and _proof_id(params.get("turnId"))):
                self._owned_in.add(params["turnId"])    # the message's input is in that turn (the proof's other half)
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
            ended = self._ended(turn)
            if ended.meta.get("error_info") == "usageLimitExceeded":
                # The one thing that marks the account exhausted (the owner's ruling): a turn that failed on it.
                return [Event("usage", usage=[self._exhausted()]), ended], []
            return [ended], []
        return [], []

    def _rate_limits(self, snap) -> list[Event]:
        """One `account/rateLimits/updated`: an observation for each window it carries (a sparse update; a window it
        leaves out, or gives as null, is not touched, and keeps its own time where it is kept: the host). A high
        `usedPercent`, or `rateLimitReachedType`, is a high usage, never an exhausted mark. It cannot raise
        (account_pools.py, rule (b))."""
        try:
            if not isinstance(snap, dict):
                return []
            lid = snap.get("limitId") if isinstance(snap.get("limitId"), str) else ""
            out = []
            for slot in ("primary", "secondary"):
                win = snap.get(slot)
                # A number is a reading, an explicit null the provider's clear; omitted or malformed is no information.
                if not isinstance(win, dict) or "usedPercent" not in win:
                    continue
                raw = win["usedPercent"]
                if raw is not None and number(raw) is None:
                    continue
                key = _window_key(lid, slot)
                rec = usage_record(key, number(raw), number(win.get("resetsAt")),
                                   label=_window_label(number(win.get("windowDurationMins"))))
                if rec is None:
                    continue        # malformed: dropped, never replacing what was seen of that window
                if len(self._limits) < 20 or key in self._limits:
                    self._limits[key] = rec         # for the reset of a later exhausted mark
                out.append(rec)
            return [Event("usage", usage=out)] if out else []
        except Exception:
            return []

    def _exhausted(self) -> dict | None:
        """The FAILED_TURN mark of a turn that failed on the limit, with the reset of a window this process last saw
        at 100% or more (the hub counts it until then only if that is after the mark was seen, else for the
        cool-down)."""
        full = [r for r in self._limits.values() if (r["percent"] or 0) >= 100 and r["resets_at"] is not None]
        last = max(full, key=lambda r: r["resets_at"], default=None)
        return usage_record(FAILED_TURN, None, last["resets_at"] if last else None, True,
                            label=(last["label"] or last["window"]) if last else None)

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
        # Codex's own end of a turn it took: one whose id came from its answer to turn/start or from turn/started
        # (Event.answered), whatever the status.
        event = Event("turn_ended", ok=ok, meta=meta, denials=tuple(self._denials), detail=detail,
                      text=cap_reply(text), answered=self._turn_id is not None)
        # (The restart's request is this turn's: `turn/completed` is read only for the turn in flight, and
        # `_end_turn` retires the request with the turn.)
        self._by_interrupt = self._restart_ok
        self._steer_final = self._steer["answer"] if self._steer is not None else None   # read before this end
        self._end_turn()
        return event
