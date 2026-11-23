"""The seam between the session host (session_host.py) and one agent CLI's wire protocol.

A `ProviderSession` is the protocol state of ONE hosted session. It never touches a process, a file or a clock: the
host hands it the lines the child wrote and gets back what they mean (`Event`s) and what to write in answer (bytes),
so an adapter is testable from strings alone. It is stateful per session, so a request/response protocol with ids and
a handshake (Codex's app-server JSON-RPC) fits the same seam as Claude's one-JSON-line-per-turn stream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# What a child can tell the host. `ready`: the provider's own session exists (`provider_id` names it there).
# `turn_started` / `turn_ended`: one turn's bounds; `ok` is False for a turn the provider reports failed, `denials`
# names the tools it refused (names only, never their inputs) and `meta` holds small scalar facts about the turn.
# `approval_requested`: the provider is waiting for someone to allow a tool (`denials` names it). `fatal`: this
# session cannot go on (`detail` says why); the host ends it and no other session. `start_failed`: THIS PROCESS cannot
# serve the session (its handshake was refused; `detail` says why): the host fails the message that was waiting on it
# and stops the process; the session stays, and the next message starts another process.
# `mode` and `tools` (turn_started only, where the provider reports them): the permission mode the process really
# runs the turn in and the tools it really has, which may differ from what its command line asked for (a settings
# file). The host holds them against the session's access (session_access.verify). Not part of an event's identity.
# On `ready`, from a provider that learns it there (Codex): `mode` is the sandbox its answer names for the thread.
# `sealed`: the process's answer to whether bypassing permission checks is switched off in it (`ok`), from a provider
# that asks (Claude, for a session started from a settings file its host merged).
# `text` (turn_ended only): the turn's reply, capped by `cap_reply`. It is for the human who sent the message and for
# nobody else: the hub keeps the last one per session and never hands it to an agent (hosted.py), except a child
# session's reply, which goes to its parent as untrusted data (hosted_results.py).
# `answered` (turn_ended only): the ADAPTER's word that the provider took the turn (it read it and ended it itself).
# The host passes it on as HostEvent.answered and the hub confirms a result turn by it (or by `turn_started`); it is
# never read off `ok`. Which ends mean the provider took the turn:
#   Claude: a `result` line for the turn, a success or an error of its own (`answered`).
#   Codex:  `turn/completed` for a turn that was accepted, its id from the answer to `turn/start` or from
#           `turn/started` (`answered`), whatever its status: completed, failed, or interrupted after a timeout.
#   Not taken (`answered` False): Codex answering `turn/start` with an error (the turn never started).
# Ends the HOST makes are never answered either: a handshake that fails (`start_failed`, a process that exits
# before it is ready), a process that exits before any event of the turn, a timeout the host ends by stopping the
# process, a write that fails, a turn withdrawn while being written (session_host._fail / _fail_start).
# `usage`: what the provider said about its plan's usage limits (`usage`: a list of observations, one per window it
# named, each `clean_usage`'s shape), from its own stream: Claude's `rate_limit_event`, Codex's
# `account/rateLimits/updated`, or a turn that failed on the limit. The host keeps each window's latest per runner,
# with its own time, for the hub's account pools (account_pools.py); it is advisory and never ends or changes a turn.
KINDS = ("ready", "turn_started", "turn_ended", "approval_requested", "fatal", "start_failed", "sealed", "usage")
# What a provider may say it can do with a turn in flight (`modes`, DESIGN_NOTES "Restarting a running turn"). No
# adapter steers yet (R6): `steers` is False on both, for a later change to implement.
MODES = ("steer", "restart")
TURN_KINDS = ("user", "wake", "child", "task")   # whose turn a message is (hosted.KINDS), as `turn` is told
REPLY_MAX_BYTES = 16 * 1024
REPLY_TRUNCATED = "\n[... reply truncated at 16 KB; the full text is in the session's transcript on its node]"


_WINDOW_RE = re.compile(r"[a-z0-9_]{1,40}")


USAGE_MAX_AGE = 86400          # an observation older than a day is not reported, nor kept at the hub
USAGE_WINDOWS_MAX = 16         # observations (windows) per runner, in a report and at the hub


def number(x: Any) -> float | None:
    """A finite JSON number as a float, else None (a bool is not a number here). Never raises: an integer too large
    for a float, NaN and the infinities are None too."""
    import math
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        f = float(x)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


FAILED_TURN = "failed_turn"    # the window key of a mark a turn that failed on the limit leaves (DESIGN_NOTES table)


def clean_usage(obs: Any, *, age: bool = False) -> dict[str, Any] | None:
    """THE one sanitiser of a usage observation (account_pools.py, rule (b); DESIGN_NOTES "Account pools", the usage
    record's rules), wherever one is read: from a provider's stream, in the host, in the node's report, in the hub's
    hello and in the hub's own record. Returns {window (its identity: a short word), label (how it is shown, or None),
    percent (0 to 1000, to 0.1, or None: an explicit null, the intentional clear), resets_at (epoch seconds, or None),
    exhausted (bool)}, with `age` (seconds, 0 to USAGE_MAX_AGE) when asked for; or None, the observation dropped, when
    it cannot be one: no valid window, a percent that is omitted or not a number from 0 to 1000 (no information:
    never a clear), an exhausted that is not a bool, or (with `age`) no valid age. A malformed reset time or label is
    only left out. It cannot raise."""
    try:
        if not isinstance(obs, dict):
            return None
        window, raw, exhausted = obs.get("window"), obs.get("percent"), obs.get("exhausted", False)
        if not isinstance(window, str) or not _WINDOW_RE.fullmatch(window) or not isinstance(exhausted, bool):
            return None
        pct = number(raw)
        if "percent" not in obs or (raw is not None and (pct is None or not 0 <= pct <= 1000)):
            return None         # omitted or malformed: no information, replacing nothing (null is the clear)
        at, label = number(obs.get("resets_at")), obs.get("label")
        out = {"window": window, "label": label if isinstance(label, str) and _WINDOW_RE.fullmatch(label) else None,
               "percent": round(pct, 1) if pct is not None else None,
               "resets_at": at if at is not None and 0 < at < 1e11 else None, "exhausted": exhausted}
        if age:
            seconds = number(obs.get("age"))
            if seconds is None or not 0 <= seconds <= USAGE_MAX_AGE:
                return None
            out["age"] = round(seconds, 1)
        return out
    except Exception:
        return None


def usage_record(window: Any, percent: Any = None, resets_at: Any = None, exhausted: bool = False,
                 label: Any = None) -> dict[str, Any] | None:
    """An adapter's observation of one window, through `clean_usage` (None: dropped). It cannot raise."""
    return clean_usage({"window": window, "label": label, "percent": percent, "resets_at": resets_at,
                        "exhausted": exhausted})


def cap_reply(text: Any) -> str:
    """A turn's reply as it may leave the node: valid UTF-8 and at most REPLY_MAX_BYTES, the marker that says it was
    cut included. Capping a capped reply changes nothing."""
    if not isinstance(text, str):
        return ""
    data = text.encode("utf-8", "replace")
    if len(data) <= REPLY_MAX_BYTES:
        return data.decode("utf-8", "replace")
    return data[:REPLY_MAX_BYTES - len(REPLY_TRUNCATED.encode())].decode("utf-8", "ignore") + REPLY_TRUNCATED


@dataclass(frozen=True)
class Event:
    kind: str
    provider_id: str | None = None
    ok: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    denials: tuple[str, ...] = ()
    detail: str = ""
    text: str = ""
    mode: str | None = field(default=None, compare=False)
    tools: tuple[str, ...] = field(default=(), compare=False)
    answered: bool = field(default=False, compare=False)     # turn_ended: the provider took the turn (above)
    usage: list[dict[str, Any]] | None = field(default=None, compare=False)     # `usage` only (above)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown provider event kind {self.kind!r}")


class ProviderSession(Protocol):
    # True: no turn may be written until `ready` (a handshake comes first). False: the child takes a turn as soon as
    # it is started, and `ready` arrives with the first turn (Claude's stream-json mode, DESIGN_NOTES).
    waits_for_ready: bool

    def argv(self, template: list[str], *, session_id: str, resume: bool, model: str | None) -> list[str]:
        """The command for this session's process, from the machine's runner template. Called once per process start
        (so again after a restart, with resume=True once the provider's session exists); resets per-process state.
        ValueError: the template or the model cannot be used for a hosted session."""
        ...

    def on_open(self) -> list[bytes]:
        """What to write as soon as the process is up (a handshake), before any turn."""
        ...

    def turn(self, text: str, kind: str = "user") -> list[bytes]:
        """One turn, as the bytes to write. `kind` (TURN_KINDS) says whose turn it is: the human's (`user`), or one
        the hub wrote (`wake`, `child`: a child session's results, `task`: a message from the agent session that
        started this one, in a frame the hub writes). An adapter may pass that on where its provider's
        input has a place for it; Claude's stream-json input and Codex's `turn/start` have none today (both take
        user text), so both write the text alone, and what the model reads is that text."""
        ...

    def feed(self, line: bytes) -> tuple[list[Event], list[bytes]]:
        """One line the child wrote: what it means, and what to write back. Never raises on malformed input."""
        ...

    def interrupt(self) -> list[bytes] | None:
        """How to stop the turn in flight without ending the process, or None: the host terminates the process and
        starts it again (resuming) for the next message."""
        ...

    # A turn in flight (optional; DESIGN_NOTES "Restarting a running turn"). `restarts`: this PROCESS can stop the
    # turn in flight in-band, keeping the process, and its end then says whether it was interrupted (`interrupted`).
    # Read anew whenever it is reported (Claude learns it from its process's `init`): an adapter without it, or with it
    # False, is never asked, and the hub is told it cannot (`modes`). `steers`: putting a message into the turn in
    # flight; False on every adapter for now (R6, the follow-ups in DESIGN_NOTES).
    steers: bool
    restarts: bool

    def takes_interrupt(self) -> bool:
        """The turn in flight can take the in-band interrupt NOW: the host writes `interrupt_turn` only then, and
        waits otherwise. Claude: once THIS turn's `init` came (measured: an interrupt written before it is answered
        `success` and the turn runs whole); Codex: once it named the turn. Optional: without it, at once."""
        ...

    def interrupt_turn(self) -> list[bytes] | None:
        """The in-band interrupt of the turn in flight, for a restart (the process stays), or None when it cannot be
        sent now. The turn then ends with its own `turn_ended`; `interrupted` says whether the interrupt ended it.
        The restart's message is written afterwards, as an ordinary turn. Optional."""
        ...

    def interrupted(self, ev: "Event") -> bool:
        """A `turn_ended` of this adapter says the provider ended the turn BECAUSE it was interrupted by
        `interrupt_turn` (not on its own, not for another failure, and not after the provider refused that
        interrupt). Optional."""
        ...

    def session_exists(self, stderr: str) -> bool | None:
        """What a process that went without ever confirming the session said about it (on stderr, or to the adapter
        itself in its protocol): True, the session it was told to create already exists (the next process must resume
        it); False, the one it was told to resume does not (the next process must create it); None, neither.
        Optional: a provider without it never corrects the host."""
        ...

    def adopt(self, provider_id: str | None) -> None:
        """The provider's own id for this session, as this machine recorded it when an earlier process reported
        `ready` (None: none is on record). For a provider that issues its own id (Codex's thread id) this is what a
        process started with resume=True resumes; one that uses the hub's session id ignores it. Called when the
        session is opened, before any `argv`. Optional."""
        ...

    def runs_in(self, cwd: str) -> None:
        """The directory the host starts this session's process in (this machine's run directory for the project).
        Called when the session is opened, before any `argv`. Optional: for a provider whose protocol must be told
        the directory, or must be kept from being told another one."""
        ...

    def holds_turns(self) -> bool:
        """True while the provider's session is busy with something no message of the host's started (Codex running
        a turn of its own): a turn written now would not be a turn of its own. The host keeps the next message
        until this is False again, for at most the turn timeout. Optional."""
        ...

    def configure(self, *, access: str | None = None, effort: str | None = None) -> None:
        """What the host decided for this session from an access word and this machine's settings
        (session_access.prepare): `access` "full" (this machine allows the session full access), or another word
        for an adapter that must set the sandbox itself whatever the runner says, and `effort`, one of
        session_access.EFFORTS, in the CLI's own terms. Called when the session is opened, before any `argv`, and
        only when there is something to say. Optional: a session that needs it is refused for an adapter without
        it."""
        ...


def modes(provider) -> tuple[str, ...]:
    """What this adapter can do with a turn in flight (MODES), as it says itself: never guessed."""
    return tuple(m for m, attr in (("steer", "steers"), ("restart", "restarts"))
                 if getattr(provider, attr, False) is True)


# A model name is one argv element the hub chose. It may never read as an option or carry anything but a name.
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}")
# A hub-issued session id: a lower-case RFC 4122 UUID as text (it becomes an argv element and a file name).
SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A provider's own id for a session (Codex's thread id), as it may be stored and reported: a short plain token.
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def validate_model(model: Any) -> str | None:
    if model is None:
        return None
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise ValueError("model override is not a plain model name")
    return model


def validate_session_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session id must be a lower-case UUID")
    return session_id


def valid_provider_id(provider_id: Any) -> str | None:
    """The id if it is one that may be stored and reported, else None."""
    return provider_id if isinstance(provider_id, str) and PROVIDER_ID_RE.fullmatch(provider_id) else None


def provider_for(template: list[str], *, codex: bool = False) -> Callable[[], ProviderSession] | None:
    """The adapter for the CLI this runner template starts, or None: it cannot be hosted as a session. `codex`: this
    machine hosts Codex sessions (`[dispatch.sessions] codex = true`); without it a Codex runner has no adapter,
    as before there was one."""
    import os
    from .claude import ClaudeSession
    name = os.path.basename(template[0]) if template else ""
    if name == "claude":
        return ClaudeSession
    if name == "codex" and codex:
        from .codex import CodexSession
        return CodexSession
    return None
