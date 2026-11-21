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
- With `--permission-prompts none` anything that would ask is refused on the spot, so the process never waits for an
  answer. A `control_request` asking to use a tool should therefore never arrive; if one does it is reported and left
  unanswered (nothing is approved), and the host's turn timeout ends the turn.
"""
from __future__ import annotations

import json
import re

from . import Event, cap_reply, validate_model, validate_session_id

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


class ClaudeSession:
    waits_for_ready = False     # `init` only arrives once the first turn has been written
    verifies_mode = True        # ... and says what the turn really runs with: the host's kill switch reads it

    def __init__(self):
        self.session_id: str | None = None
        self._ready = False
        self._dead = False
        self._full = False              # this machine allows the session full access (`configure`)
        self._effort: str | None = None

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
        return out + ["--resume" if resume else "--session-id", self.session_id]

    def adopt(self, provider_id: str | None) -> None:
        """Nothing to adopt: Claude's own id for the session is the hub's session id."""

    def on_open(self) -> list[bytes]:
        return []

    def turn(self, text: str) -> list[bytes]:
        # json.dumps escapes every line break inside the text: one turn is exactly one line.
        return [json.dumps({"type": "user", "message": {"role": "user", "content": text}}).encode() + b"\n"]

    def interrupt(self) -> list[bytes] | None:
        return None

    def session_exists(self, stderr: str) -> bool | None:
        """Claude Code 2.1.322 refuses a start in the wrong mode before any turn and exits 1 (DESIGN_NOTES "Session
        host (PR 2)"): `--session-id` for a session that exists writes "Error: Session ID <uuid> is already in use.";
        `--resume` for one that does not writes "No conversation found with session ID: <uuid>"."""
        if f"Session ID {self.session_id} is already in use" in stderr:
            return True
        if f"No conversation found with session ID: {self.session_id}" in stderr:
            return False
        return None

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
            mode, tools = ev.get("permissionMode"), ev.get("tools")
            # A name that is not a plain tool name still counts as a tool the session has: "unknown", never dropped.
            named = tuple(t if isinstance(t, str) and TOOL_NAME_RE.fullmatch(t) else "unknown"
                          for t in (tools if isinstance(tools, list) else [])[:MAX_TOOLS + 1])
            return events + [Event("turn_started", mode=mode if isinstance(mode, str) and len(mode) <= 40 else None,
                                   tools=named)], []
        if kind == "result":
            ok = ev.get("subtype") == "success" and ev.get("is_error") is False
            return [Event("turn_ended", ok=ok, meta=_meta(ev), denials=tool_names(ev.get("permission_denials")),
                          text=cap_reply(ev.get("result")))], []
        if kind == "control_request":
            request = ev.get("request")
            if isinstance(request, dict) and request.get("subtype") == "can_use_tool":
                return [Event("approval_requested", denials=tool_names([request]))], []
        return [], []
