"""Claude Code as a hosted session: `claude -p --input-format stream-json --output-format stream-json --verbose`.

The process stays up and takes one user turn per JSON line on stdin. What the adapter relies on was observed on Claude
Code 2.1.322 (DESIGN_NOTES "Session host (PR 1)"):
- Nothing is written before the first turn. Every turn begins with a `system`/`init` event and ends with exactly one
  `result` event; both carry the `session_id` given as `--session-id` (or `--resume`).
- A `result` lists the tool calls that were refused as `permission_denials`: objects with `tool_name`, `tool_use_id`
  and `tool_input`. Only the names leave this module: an input can hold file contents and paths.
- Each `init` also names the `permissionMode` the turn really runs in and the `tools` the session really has. Both
  can differ from what the command line asked for: a settings file's `permissions.defaultMode` put a process started
  with no bypass flag into `bypassPermissions` (seen live). The adapter passes both on with `turn_started`, and the
  host holds them against the session's access (session_access.verify).
- A `control_request` with `subtype: get_settings` on stdin is answered at once, before any turn, with a
  `control_response` whose `response.effective` is the settings in force, every source merged (seen live). The
  adapter asks it once per process when the command line names a settings FILE and does not select bypass: the host
  wrote that file from the runner's own settings plus the key that switches bypass off, and Claude ignores a
  settings file it finds invalid, whole and silently. The answer (`sealed`) says whether the key is in force. The
  host writes no turn to the process before it, and never keeps the answer itself: `effective` is every setting in
  force, and a settings file can hold credentials (`env`). `transcript_line` gives the transcript one line of the
  adapter's own in its place, with the one bit.
- With `--permission-prompts none` anything that would ask is refused on the spot, so the process never waits for an
  answer. A `control_request` asking to use a tool should therefore never arrive; if one does it is reported and left
  unanswered (nothing is approved), and the host's turn timeout ends the turn.
Restart (measured on Claude Code 2.1.330, DESIGN_NOTES "Restarting a running turn"):
- A user line written while a turn runs is queued by the process and read at the turn's next tool boundary, or, if
  it misses the last one, run by the process as its own next turn: the CLI decides alone. This adapter does not steer
  (`steers` False): its turns are the lines they always were.
- `control_request` `interrupt` ends the turn in flight at once: a `control_response` for that request (`success`,
  with `still_queued`), then the turn's `result` with `subtype: error_during_execution` (terminal_reason
  `aborted_tools`), and the process stays (`interrupt_turn`, `interrupted`; a restart writes its message afterwards,
  as an ordinary turn). An abort counts as the interrupt's only once the process answered THAT request with success:
  one it answered with an error (or not at all) interrupted nothing, and a later abort is the turn's own.
- Every `init` lists the process's `capabilities` (`interrupt_receipt_v1` among them) and `claude_code_version`. This
  process restarts only once an `init` of its own said `interrupt_receipt_v1` (`restarts`): a CLI that does not, or a
  process that has not started a turn yet, reports no restart.
- An interrupt written behind a turn's user line before that turn's `init` is answered `success` at once and the
  turn then runs whole (measured on 2.1.330, DESIGN_NOTES "Review follow-ups (2026-11-23)"). So the host writes it
  only once the turn's own `init` came (`takes_interrupt`), and a receipt that arrives before it never counts.
Plan usage (account_pools.py; from Claude Code 2.1.330's own SDK message schema, which the CLI binary carries, and
checked against lines recorded from real hosted sessions on 2026-11-22/23, tests/fixtures/rate_limit_samples.jsonl:
DESIGN_NOTES "Account pools"):
- `{"type": "rate_limit_event", "rate_limit_info": {...}}`, "emitted when rate limit info changes": `status`
  (allowed | allowed_warning | rejected: whether requests are refused now), `resetsAt` (epoch seconds), `rateLimitType`
  (five_hour, seven_day, ...), `utilization` (a fraction of the limiting window), `surpassedThreshold` (the highest
  warning threshold passed, a fraction), and, marked internal, `unifiedWindows` (each window's `utilization`,
  `resetsAt`, `status`). It is absent for API-key sessions. `usage_of` reads one observation per window
  from it. Recorded:
  `unifiedWindows` in every line (`seven_day`, or `five_hour` and `seven_day`), the top-level `utilization` in some
  only, utilization a fraction 0..1 (0 right after a reset), and overage fields beside them.
- A turn that fails on the limit: an `assistant` message whose `error` is `rate_limit`. That account counts as
  exhausted (until the last refusal's reset time, when one was seen).
Turn correlation (DESIGN_NOTES "Claude turn correlation (2026-11-24)"; measured on Claude Code 2.1.330 for the steering
design on branch claude/steer-loose, its part B):
- Every user line the host writes carries a fresh random `uuid` beside the same `message` (`_user_line`). Claude
  echoes it: the `init` of the turn that took the line lists it in `user_message_uuids`, the turn's `result` in
  `user_message_uuid` and `user_message_uuids` (with every line folded into that turn), and `command_lifecycle` lines
  name it (`queued`, `started`, `completed`, `cancelled`). An interrupted turn's aborted `result` echoes it too.
- A turn the CLI starts itself (a background Agent finishing after the host's turn ended): its `init` lists no uuid
  and has no `origin`; its `result` has no echo, `num_turns` 1 or more, and `origin.kind` `task-notification`. Main
  took those frames for the next message's and later dropped that message's own result.
- Only in a VERIFIED process incarnation are frames attributed by that evidence (`_init_turn`, `_result_turn`: the
  bracket table): its first `init` names a `claude_code_version` in VERIFIED and lists `msg_lifecycle_v1`. VERIFIED
  holds the versions that passed the release gates M3 (a resumed process echoes), M6 (compaction) and M7 (failure
  frames); until one has, it is empty, and every process is attributed by order exactly as before (`Event.turn`
  "order"). Verification is lost for good at the first deviation (a plain `result` where the table falls back to
  order, an `init` that no longer qualifies). While a verified process runs a turn of its own (an unattributed
  bracket) it `holds_turns`: the host writes no message for at most the turn timeout (session_host `_held`, as for
  Codex).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

from . import FAILED_TURN, Event, cap_reply, number, usage_record, validate_model, validate_session_id

# Flags a hosted session sets itself, or that would start, fork or hide a conversation other than the hub's.
OWN_FLAGS = frozenset((
    "--session-id", "--resume", "-r", "--continue", "-c", "--fork-session", "--from-pr", "--teleport",
    "--input-format", "--output-format", "--permission-prompts", "--permission-prompt-tool",
    "--no-session-persistence"))
STREAM_FLAGS = ("--input-format", "stream-json", "--output-format", "stream-json")
NO_PROMPTS = ("--permission-prompts", "none")
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
MAX_DENIALS = 50
MAX_TOOLS = 2000        # tool names taken from one `init`
INTERRUPT_RECEIPT = "interrupt_receipt_v1"  # an `init` capability: a receipt for the in-band interrupt
LIFECYCLE = "msg_lifecycle_v1"      # an `init` capability: user lines' uuids are echoed (module text)
MAX_CAPS = 200
# The Claude Code versions whose processes are attributed by evidence (module text). A version goes in only once the
# release gates M3, M6 and M7 passed on it (DESIGN_NOTES "Claude turn correlation"): never to make a gate pass.
VERIFIED: frozenset[str] = frozenset()
MAX_ECHO = 64           # uuids read from one frame's echo fields
# Brackets (`_bracket`): the turn the process is in, by evidence. OWN: the host's turn in flight (an `init` echoed
# its uuid); UNATTRIBUTED: a turn no message of the host's is known to have started (an unmarked `init`).
OWN, UNATTRIBUTED = "own", "unattributed"
log = logging.getLogger("agent_comms.providers.claude")
# The facts of a `result` the host passes on as `meta`: small scalars only. The reply text travels apart from them
# (Event.text, capped), so nothing that logs or stores `meta` ever holds it.
RESULT_META = ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "stop_reason", "terminal_reason",
               "api_error_status")


BYPASS_REFUSED = ("a Claude runner that bypasses permission checks (--dangerously-skip-permissions or --permission-mode "
                  "bypassPermissions) cannot be hosted as a session unless this node's settings say [dispatch.sessions] "
                  "allow_full_access = true")


def bypasses_permissions(template: list[str]) -> bool:
    """The runner turns Claude's permission checks off, or lets a session turn them off: a hosted session takes
    turns from messages for as long as it lives, so it is started that way only on a machine that allows sessions
    full access (`configure`; session_access.py). Every other permission mode is the machine owner's choice, as for
    a dispatched run."""
    for i, arg in enumerate(template):
        flag, eq, value = arg.partition("=")
        if flag in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"):
            return True
        if flag == "--permission-mode":
            mode = value if eq else (template[i + 1] if i + 1 < len(template) else "")
            if mode.lower() == "bypasspermissions":
                return True
    return False


def selects_bypass(argv: list[str]) -> bool:
    """The command line starts Claude in the bypass mode (unlike `bypasses_permissions`, the flag that only makes
    the mode selectable does not count)."""
    return bypasses_permissions([a for a in argv if a.split("=", 1)[0] != "--allow-dangerously-skip-permissions"])


def settings_file(argv: list[str]) -> bool:
    """The command line's settings (its last `--settings`: the one Claude takes) are a file, not the JSON itself."""
    value = None
    for i, arg in enumerate(argv):
        flag, eq, rest = arg.partition("=")
        if flag == "--settings":
            value = rest if eq else (argv[i + 1] if i + 1 < len(argv) else None)
    return value is not None and not value.lstrip().startswith("{")


def tool_names(denials) -> tuple[str, ...]:
    """The distinct tool names in a result's `permission_denials`, in order. Anything that is not a plain name is
    reported as "unknown" rather than passed on."""
    out: list[str] = []
    for d in denials if isinstance(denials, list) else []:
        name = d.get("tool_name") if isinstance(d, dict) else None
        name = name if isinstance(name, str) and TOOL_NAME_RE.fullmatch(name) else "unknown"
        if name not in out:
            out.append(name)
    return tuple(out[:MAX_DENIALS])


def _meta(ev: dict) -> dict:
    out = {}
    for k in RESULT_META:
        v = ev.get(k)
        if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200):
            out[k] = v
    return out


def _bypass_off(answer: dict) -> bool:
    """A `get_settings` answer (the `response` of a `control_response`) says the key that switches bypass off is
    in force: `response.effective.permissions.disableBypassPermissionsMode == "disable"`."""
    inner = answer.get("response") if answer.get("subtype") == "success" else None
    effective = inner.get("effective") if isinstance(inner, dict) else None
    permissions = effective.get("permissions") if isinstance(effective, dict) else None
    return isinstance(permissions, dict) and permissions.get("disableBypassPermissionsMode") == "disable"




def usage_of(info) -> list[dict] | None:
    """A `rate_limit_event`'s `rate_limit_info` as an Event.usage (module text): one observation per window, each with
    its own reset. From `unifiedWindows` when it has a usable window, else from the top-level `utilization` (or the
    `surpassedThreshold` passed, a lower bound) for the window `rateLimitType` names. `status: rejected` (requests are
    refused now) marks the limiting window exhausted: the one `rateLimitType` names, else one that says `rejected`,
    until ITS own reset (the top-level `resetsAt` only for the window it names). None: nothing usable.
    `unifiedWindows` is marked internal in Claude's schema, so without it the top-level fields alone are read. A
    missing or null field, a window of a name not known here, a new key or a number out of range never fails: it
    cannot raise (account_pools.py, rule (b))."""
    try:
        return _usage_of(info)
    except Exception:
        return None


LIMITING = "limiting"   # the window key of the top-level fields when they name no window type
MISSING = object()      # a fraction that is omitted or malformed: no information (DESIGN_NOTES, the usage table)


def _percent(d: dict, key: str):
    """A fraction field as a percent, three ways kept apart (the usage table): a number (a reading), an explicit null
    (None: the provider's own clear), or MISSING (omitted or malformed: no information, replacing nothing)."""
    if key not in d:
        return MISSING
    if d[key] is None:
        return None
    n = number(d[key])
    return MISSING if n is None else n * 100


def _usage_of(info) -> list[dict] | None:
    if not isinstance(info, dict):
        return None
    kind = info.get("rateLimitType") if isinstance(info.get("rateLimitType"), str) else None
    top_reset, status = number(info.get("resetsAt")), info.get("status")
    raw = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    windows = {n: w for n, w in raw.items() if isinstance(n, str) and isinstance(w, dict)}
    # Each window's reset, read on its own (also for a window that carries no utilization); the top-level reset is
    # the limiting window's, the one `rateLimitType` names.
    resets = {n: number(w.get("resetsAt")) for n, w in windows.items()}
    if kind is not None and resets.get(kind) is None:
        resets[kind] = top_reset
    out: dict[str, dict | None] = {}
    # A MISSING percent is no reading: usage_record (clean_usage, the one sanitiser) drops it, replacing nothing.
    for name, w in windows.items():
        # A window's own `rejected` alone does not mean requests are refused (the schema): the top level says that.
        out[name] = usage_record(name, _percent(w, "utilization"), resets.get(name))
    if not any(out.values()):
        pct = _percent(info, "utilization")
        pct = pct if pct is not MISSING else _percent(info, "surpassedThreshold")
        out = {kind or LIMITING: usage_record(kind or LIMITING, pct, top_reset)}
    if status == "rejected":    # refused now: the limiting window, until ITS own reset
        target = kind or next((n for n, w in windows.items() if w.get("status") == "rejected"), None) or LIMITING
        rec = out.get(target) or {}
        reset = resets.get(target) if resets.get(target) is not None else top_reset if target == LIMITING else None
        out[target] = usage_record(target, rec.get("percent"), reset, True)
    kept = [o for o in out.values() if o is not None]
    return kept or None


def new_uuid() -> str:
    """A fresh random uuid for one user line. From os.urandom, not uuid.uuid4: a caller that makes uuid4
    deterministic (tests/golden_schema_free.py) must see exactly the ids it saw before lines carried one."""
    return str(uuid.UUID(bytes=os.urandom(16), version=4))


def _user_line(text: str, line_id: str) -> bytes:
    # json.dumps escapes every line break inside the text: one turn is exactly one line. The `message` is the same
    # bytes as before lines carried a uuid; the uuid is one more key, last.
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}, "uuid": line_id}).encode() + b"\n"


def echoed(ev: dict, *keys: str) -> set[str]:
    """The uuids a frame echoes in these fields (a string, or a list of strings)."""
    out: set[str] = set()
    for k in keys:
        v = ev.get(k)
        for u in ([v] if isinstance(v, str) else v[:MAX_ECHO] if isinstance(v, list) else []):
            if isinstance(u, str):
                out.add(u)
    return out


def origin_not_human(ev: dict) -> bool:
    """The frame names an `origin` whose `kind` is a word other than `human` (Claude's mark of a turn it started for
    a notification). A frame with no `origin` (every turn of the host's, as measured) does not."""
    origin = ev.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return isinstance(kind, str) and kind != "human"


# The classes of a `result` in a verified process (DESIGN_NOTES "Claude turn correlation", the frames by evidence).
OWNED, OWNED_OTHER, NON_TURN, FOREIGN, PLAIN = "owned", "owned_other", "non_turn", "foreign", "plain"


def result_class(ev: dict, written: set[str], current: str | None) -> str:
    """OWNED: it lists the uuid of the host's turn in flight (`current`); OWNED_OTHER: it lists only other uuids the
    host wrote to this process; NON_TURN: no host uuid, an `origin` that is not human, `num_turns` 0 (a zero-turn
    task notification, success or error: it ends no turn); FOREIGN: the same with `num_turns` 1 or more (a turn the
    CLI started itself); PLAIN: anything else (no evidence either way)."""
    host = echoed(ev, "user_message_uuid", "user_message_uuids") & written
    if current is not None and current in host:
        return OWNED
    if host:
        return OWNED_OTHER
    turns = ev.get("num_turns")
    if origin_not_human(ev) and isinstance(turns, int) and not isinstance(turns, bool):
        if turns == 0:
            return NON_TURN
        if turns >= 1:
            return FOREIGN
    return PLAIN


class ClaudeSession:
    waits_for_ready = False     # `init` only arrives once the first turn has been written
    verifies_mode = True        # ... and says what the turn really runs with: the host's kill switch reads it

    steers = False      # R6: no mid-turn steering (the CLI alone decides which turn takes a mid-turn line)

    @property
    def restarts(self) -> bool:
        """This process said in an `init` that it acknowledges the in-band interrupt."""
        return INTERRUPT_RECEIPT in self._caps and not self._dead

    def __init__(self):
        self.session_id: str | None = None
        self._ready = False
        self._dead = False
        self._full = False              # this machine allows the session full access (`configure`)
        self._effort: str | None = None
        self._ask = False               # this process is asked whether bypass is switched off in it (`on_open`)
        self._asked: str | None = None  # the id of that request, until it is answered
        self._starts = 0
        self._limit: list[dict] | None = None   # the last usage this process reported (`rate_limit_event`)
        self._reset_turns()

    def _reset_turns(self) -> None:
        self._open = False              # a turn is running in this process (written or begun, its result not yet)
        self._begun = False             # ... and its own `init` came (`takes_interrupt`)
        # Turn correlation (module text), per process incarnation.
        self._current: str | None = None    # the uuid of the host's turn in flight (written, its result not yet)
        self._written: set[str] = set()     # every uuid the host wrote to this process
        self._verified: bool | None = None  # None until its first `init`; False for good after a deviation
        self._bracket: str | None = None    # OWN, UNATTRIBUTED or None (no turn open, by evidence)
        self._competing = False         # an `init` not the turn's own came inside OWN (the steering design's F-4)
        self.deviation: str | None = None   # why verification was lost (for the log and the tests)
        self._caps: frozenset[str] = frozenset()   # what this process's `init` said it can (module text)
        self._interrupt_id: str | None = None    # the request id of the interrupt written for the turn in flight
        self._interrupt_ok = False      # ... which the process answered with success

    def configure(self, *, access: str | None = None, effort: str | None = None) -> None:
        """What the host decided for this session (session_access.prepare), before any `argv`. `access` "full":
        this machine allows it full access, so a template that bypasses permission checks is not refused.
        `effort`: Claude Code's `--effort` for the session, replacing the runner's own."""
        self._full, self._effort = access == "full", effort

    def argv(self, template: list[str], *, session_id: str, resume: bool, model: str | None) -> list[str]:
        """The runner template (already scoped where the project asks for it) as a session command: without its
        {prompt} element, with the stream flags, the session's id and `--permission-prompts none`."""
        import os
        self.session_id = validate_session_id(session_id)
        self._ready = self._dead = False
        self._reset_turns()
        model = validate_model(model)
        if not template or os.path.basename(template[0]) != "claude":
            raise ValueError("not a Claude runner")
        if bypasses_permissions(template) and not self._full:
            raise ValueError(BYPASS_REFUSED)
        out, i = [], 0
        while i < len(template):
            arg = template[i]
            flag = arg.split("=", 1)[0]
            i += 1
            if flag in OWN_FLAGS:
                raise ValueError("this Claude runner sets session or stream flags a hosted session sets itself")
            if arg == "{prompt}":
                continue            # turns arrive on stdin
            if (flag == "--model" and model is not None) or (flag == "--effort" and self._effort is not None):
                if "=" not in arg and i < len(template):
                    i += 1          # its value
                continue
            out.append(arg)
        if not any(a in ("-p", "--print") for a in out):
            out.insert(1, "-p")
        out += STREAM_FLAGS
        if "--verbose" not in out:
            out.append("--verbose")     # stream-json output needs it
        out += NO_PROMPTS
        if model is not None:
            out += ["--model", model]
        if self._effort is not None:
            out += ["--effort", self._effort]
        self._ask, self._asked = settings_file(out) and not selects_bypass(out), None
        return out + ["--resume" if resume else "--session-id", self.session_id]

    def adopt(self, provider_id: str | None) -> None:
        """Nothing to adopt: Claude's own id for the session is the hub's session id."""

    def on_open(self) -> list[bytes]:
        """Nothing, or the one question a process started from a settings file is asked (see the module text)."""
        if not self._ask:
            return []
        self._starts += 1
        self._asked = "settings-%s-%d" % ((self.session_id or "")[:8], self._starts)   # one per session and process
        return [json.dumps({"type": "control_request", "request_id": self._asked,
                            "request": {"subtype": "get_settings"}}).encode() + b"\n"]

    def transcript_line(self, line: bytes) -> bytes:
        """What the session's transcript keeps of a line the process wrote: the line, except a `control_response`,
        whose body is never kept (the answer to `get_settings` is the settings in force, credentials included).
        In its place: who answered what kind of answer, and whether bypass is switched off.

        A line that does NOT parse may be that answer cut short (the process died in the middle of it, or its last
        bytes came without a line end), and half a settings body is as secret as a whole one. So it is not kept
        either, only its length, when this process was asked for its settings at all (then nothing unparseable
        of it is kept, before or after the answer), and for any process when the line names a control response or
        a question of this adapter's."""
        try:
            ev = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if self._ask or b"control_response" in line or b"get_settings" in line or (
                    self.session_id is not None and ("settings-" + self.session_id[:8]).encode() in line):
                return json.dumps({"type": "host", "subtype": "line_withheld", "bytes": len(line),
                                   "why": "not a complete JSON line"}).encode()
            return line
        if not isinstance(ev, dict) or ev.get("type") != "control_response":
            return line
        answer = ev.get("response") if isinstance(ev.get("response"), dict) else {}
        rid, kind = answer.get("request_id"), answer.get("subtype")
        return json.dumps({"type": "host", "subtype": "control_response_withheld",
                           "request_id": rid if isinstance(rid, str) and TOOL_NAME_RE.fullmatch(rid) else None,
                           "answer": kind if kind in ("success", "error") else None,
                           "bypass_disabled": _bypass_off(answer)}).encode()

    def turn(self, text: str, kind: str = "user") -> list[bytes]:
        # `kind` has no place in Claude's stream-json input (a user message is the one kind of turn it takes): the
        # text is all it gets, with a fresh uuid of its own (turn correlation, module text).
        self._open, self._begun = True, False
        self._interrupt_id, self._interrupt_ok = None, False
        self._current = new_uuid()
        self._written.add(self._current)
        return [_user_line(text, self._current)]

    def interrupt(self) -> list[bytes] | None:
        return None

    @property
    def verified(self) -> bool:
        """This process incarnation is attributed by evidence (module text)."""
        return self._verified is True and not self._dead

    def holds_turns(self) -> bool:
        """A verified process is running a turn of its own (an unattributed bracket): a line written now would be
        taken into that turn or queued behind it, and that turn's frames are no message's. The host keeps the next
        message until its end, for at most the turn timeout (session_host `_held`: the write barrier)."""
        return self.verified and self._bracket == UNATTRIBUTED

    def takes_interrupt(self) -> bool:
        """Only once THIS turn's `init` came. The process says it can restart in its first `init`, but an interrupt
        written behind a later turn's line, before that turn's `init`, is answered `success` and the turn then runs
        whole (measured on Claude Code 2.1.330: DESIGN_NOTES "Review follow-ups (2026-11-23)"). In a verified process:
        only inside the turn's own bracket (an `init` that echoed its uuid), never during a turn the CLI started."""
        if self.verified:
            return self._open and self._bracket == OWN and self.restarts
        return self._open and self._begun and self.restarts

    def interrupt_turn(self) -> list[bytes] | None:
        """The in-band interrupt of the turn in flight (a restart's first step): the turn's `result` follows."""
        if not self._open or not self.restarts:
            return None
        self._starts += 1
        self._interrupt_id = "interrupt-%s-%d" % ((self.session_id or "")[:8], self._starts)
        self._interrupt_ok = False
        return [json.dumps({"type": "control_request", "request": {"subtype": "interrupt"},
                            "request_id": self._interrupt_id}).encode() + b"\n"]

    def interrupted(self, ev: Event) -> bool:
        """The interrupt ended the turn: the process answered that very request with success, and the turn's result
        is an error whose terminal reason is an abort (`aborted_tools` when it came during a tool call, as measured).
        """
        reason = ev.meta.get("terminal_reason")
        return self._interrupt_ok and not ev.ok and isinstance(reason, str) and reason.startswith("aborted")

    def session_exists(self, stderr: str) -> bool | None:
        """Claude Code 2.1.322 refuses a start in the wrong mode before any turn and exits 1 (DESIGN_NOTES "Session
        host (PR 2)"): `--session-id` for a session that exists writes "Error: Session ID <uuid> is already in use.";
        `--resume` for one that does not writes "No conversation found with session ID: <uuid>"."""
        if f"Session ID {self.session_id} is already in use" in stderr:
            return True
        if f"No conversation found with session ID: {self.session_id}" in stderr:
            return False
        return None

    # ------------------------------------------------------------ turn correlation (module text)

    def _lose(self, why: str) -> None:
        """The first deviation: this incarnation is attributed by order from now on, for good."""
        if self._verified is True:
            log.warning("Claude session %s: this process is attributed by order from now on: %s",
                        self.session_id, why)
            self.deviation = why
        self._verified = False
        self._bracket, self._competing = None, False

    def _init_turn(self, ev: dict) -> str:
        """An `init`: whose turn it starts (Event.turn), and the bracket it leaves open."""
        version = ev.get("claude_code_version")
        qualifies = isinstance(version, str) and version in VERIFIED and LIFECYCLE in self._caps
        if self._verified is None:
            self._verified = qualifies      # pinned to this incarnation by its first `init`
        elif self._verified and not qualifies:
            self._lose("an init no longer names a verified version with msg_lifecycle_v1")
        if not self._verified:
            self._open = self._begun = True     # by order, as before turn correlation
            return "order"
        own = self._current is not None and self._current in echoed(ev, "user_message_uuids")
        if self._bracket is None:
            if own:
                self._bracket, self._competing, self._begun = OWN, False, True
                return "own"
            # An unmarked `init` (or one echoing only lines settled already): a turn no message here started. The
            # write barrier holds until its end (`holds_turns`).
            self._bracket, self._competing = UNATTRIBUTED, False
            log.info("Claude session %s: the CLI began a turn of its own", self.session_id)
            return "other"
        if self._bracket == OWN:
            # An owned bracket is never replaced. Another `init` in it (a compaction's, a re-init, another turn's)
            # is noted (the steering design's F-4) and logged; its access is checked all the same.
            if not own:
                self._competing = True
                log.info("Claude session %s: an init that is not the turn's own came inside its bracket",
                         self.session_id)
            return "other"
        # UNATTRIBUTED, and then the turn in flight's own `init`: the evidence makes the bracket the turn's (an
        # unattributed bracket is no owned one); that another turn's `init` opened it first is noted.
        if own:
            self._bracket, self._competing, self._begun = OWN, True, True
            log.info("Claude session %s: the turn in flight began inside a turn the CLI started", self.session_id)
            return "own"
        return "other"

    def _steer_only(self, ev: dict) -> bool:
        """SEAM for the steering design's F-4 (its part D, step 3): a `result` inside the turn's own bracket that lists
        only a steer written into that bracket. No steer is ever written in this build (`steers` False), so it is
        never the case, and such a result is what any other line's is: nothing (logged)."""
        return False

    def _result_turn(self, ev: dict) -> str | None:
        """A `result`: whose turn it ends (Event.turn), or None for one that ends no turn (the bracket table)."""
        if not self._verified:
            # By order, as before turn correlation: also a `result` before any `init` of this process (a launch
            # refusal: it fails the message that started the process, as it always has).
            self._open, self._current = False, None
            return "order"
        cls = result_class(ev, self._written, self._current)
        if cls == NON_TURN:
            log.info("Claude session %s: a zero-turn notification ended no turn", self.session_id)
            return None
        if cls == OWNED:
            # The turn's own result, in its bracket, in an unattributed one (Claude took the line into the CLI's
            # turn), or with no bracket (its `init` was lost).
            self._open, self._current, self._bracket, self._competing = False, None, None, False
            return "own"
        if self._bracket == OWN:
            if cls == PLAIN:
                # No echo at all: the bracket's own end. An absent echo never discards a result.
                self._open, self._current, self._bracket, self._competing = False, None, None, False
                return "own"
            if self._steer_only(ev):
                pass        # F-4's case would be decided here (the steering design's D); not built: the row below
            log.info("Claude session %s: a %s result inside the turn's bracket ended nothing", self.session_id,
                     "foreign" if cls == FOREIGN else "another line's")
            return "other"
        if cls == PLAIN:
            # A deviation (the measured formats always say whose a result is): by order from now on, this result
            # too (the turn in flight's if there is one, as before turn correlation).
            self._lose("a result with no evidence of whose it is" + (" ended a turn the CLI started"
                                                                    if self._bracket == UNATTRIBUTED else ""))
            self._open, self._current = False, None
            return "order"
        # FOREIGN or OWNED_OTHER: no message's. It ends a turn the CLI started (the barrier lifts), or is only logged.
        log.info("Claude session %s: a turn the CLI %s ended (%s)", self.session_id,
                 "started itself" if self._bracket == UNATTRIBUTED else "ran", cls)
        self._bracket, self._competing = None, False
        return "other"

    def feed(self, line: bytes) -> tuple[list[Event], list[bytes]]:
        if self._dead:
            return [], []
        try:
            ev = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return [], []
        if not isinstance(ev, dict):
            return [], []
        kind, sid = ev.get("type"), ev.get("session_id")
        init = kind == "system" and ev.get("subtype") == "init"
        if (isinstance(sid, str) and sid != self.session_id) or (init and not isinstance(sid, str)):
            self._dead = True
            return [Event("fatal", detail="the process reported a session id other than the one it was given")], []
        if init:
            events = [] if self._ready else [Event("ready", provider_id=sid)]
            self._ready = True
            caps = ev.get("capabilities")
            self._caps = frozenset(c for c in (caps if isinstance(caps, list) else [])[:MAX_CAPS] if isinstance(c, str))
            mode, tools = ev.get("permissionMode"), ev.get("tools")
            # A name that is not a plain tool name still counts as a tool the session has: "unknown", never dropped.
            named = tuple(t if isinstance(t, str) and TOOL_NAME_RE.fullmatch(t) else "unknown"
                          for t in (tools if isinstance(tools, list) else [])[:MAX_TOOLS + 1])
            # Every `init` is reported, whatever its bracket: the host checks each one's access (session_access.verify).
            return events + [Event("turn_started", mode=mode if isinstance(mode, str) and len(mode) <= 40 else None,
                                   tools=named, turn=self._init_turn(ev))], []
        if kind == "result":
            ok = ev.get("subtype") == "success" and ev.get("is_error") is False
            turn = self._result_turn(ev)
            if turn is None:
                return [], []       # a zero-turn notification: it ends no turn (logged)
            # Claude's own end of the turn, a success or an error: it took the turn (Event.answered).
            return [Event("turn_ended", ok=ok, meta=_meta(ev), denials=tool_names(ev.get("permission_denials")),
                          text=cap_reply(ev.get("result")), answered=True, turn=turn)], []
        if kind == "control_response":
            answer = ev.get("response")
            if (self._interrupt_id is not None and isinstance(answer, dict)
                    and answer.get("request_id") == self._interrupt_id):
                # The receipt for the interrupt of the turn in flight: only `success` counts (`interrupted`), and only
                # once the turn's `init` came: one before it was not applied (measured), so a later abort of the
                # turn is its own.
                self._interrupt_ok = answer.get("subtype") == "success" and self._begun
                return [], []
            if self._asked is None or not isinstance(answer, dict) or answer.get("request_id") != self._asked:
                return [], []
            self._asked = None
            return [Event("sealed", ok=_bypass_off(answer))], []
        if kind == "control_request":
            request = ev.get("request")
            if isinstance(request, dict) and request.get("subtype") == "can_use_tool":
                return [Event("approval_requested", denials=tool_names([request]))], []
        if kind == "rate_limit_event":      # plan usage (module text): for the hub's account pools, advisory
            usage = usage_of(ev.get("rate_limit_info"))
            if usage is None:
                return [], []
            self._limit = usage
            return [Event("usage", usage=usage)], []
        if kind == "assistant" and ev.get("error") == "rate_limit":
            # The turn hit the limit: the FAILED_TURN mark, with the reset the last refusal named (when one did). The
            # hub counts it until that reset only if it is after the moment the mark was seen, else for the cool-down.
            last = next((o for o in self._limit or [] if o["exhausted"]), {})
            return [Event("usage", usage=[usage_record(FAILED_TURN, None, last.get("resets_at"), True,
                                                       label=last.get("window"))])], []
        return [], []
