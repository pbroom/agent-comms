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
KINDS = ("ready", "turn_started", "turn_ended", "approval_requested", "fatal", "start_failed", "sealed")
TURN_KINDS = ("user", "wake", "child", "task")   # whose turn a message is (hosted.KINDS), as `turn` is told
REPLY_MAX_BYTES = 16 * 1024
REPLY_TRUNCATED = "\n[... reply truncated at 16 KB; the full text is in the session's transcript on its node]"


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
