"""`board node`'s hosted sessions: the node side of hosted.py, driving session_host.SessionHost.

The node's run loop (node.py) sleeps `poll_seconds` between passes, far too long for a message to start a turn
"at once". Sessions therefore have their own loop, on their own thread, and only that thread touches the SessionHost
(which stays single-threaded):

- Nothing to do: one long-poll of `GET /api/nodes/sessions?wait=20&seen=<version>`. The hub answers as soon as
  anything this node acts on changes (a new message, a new session, a close), so an idle session's message is written
  to its process within a fraction of a second.
- Something in flight: `SessionHost.tick()` every TICK_SECONDS and a plain fetch every SYNC_SECONDS.
- What the host reports (`HostEvent`s) is posted to the hub in order, per session; a report the hub cannot take now
  is kept and sent again. The hub applies reports idempotently.

Rules the hub relies on:
- One message at a time. The hub holds the queue; this loop hands the host a session's next message only once the one
  before it is over. A message waits at the hub while its session's process cannot start (`max_live` session
  processes are up and none is idle), and nothing running is ever stopped to make room: only an idle process is.
- Never twice. The number of the last message handed to each session is on disk before the host gets it
  (HANDED_FILE). A message at or below that number which the hub still lists and the host no longer holds was lost
  (the node restarted): it is reported failed, never written again. That number says nothing else: in particular
  not whether the provider's session exists.
- Start-up: `SessionHost.recover()` first (stop what an earlier node process left), then the hub's sessions for this
  node are opened again, resuming exactly those the provider confirmed (the hub's `resume`, or the last host's
  record), and a turn that was in flight is reported failed. Where that guess is wrong the host corrects itself
  after one refused start (session_host._correct_start_mode).
- A hub call failed (unreachable, paused, an error): turns already running go on, but nothing new is handed over
  until a fetch has succeeded again, because the queue and the stop flags this node holds may be out of date.
  No hub for `self_stop_seconds` (node.SELF_STOP_SECONDS): turns in flight are stopped then, by the deadline
  watchdog whatever hub call the loop is waiting on (`enforce_deadlines`), and reported failed once the hub
  answers; idle processes stay. From then on nothing starts and no new turn is written, for any session, until a
  listing fetched since contact came back has been applied (`_may_start`): what waits in an inbox stays there.
- A session the hub no longer lists is closed here. Shutdown stops every process (`stop_all`); the sessions go on
  at the next start. From the moment shutdown begins, shutdown alone reports: the loop posts nothing more.
- `[dispatch.sessions]`: `idle_hours` (a process idle that long is stopped; its session stays and the next message
  resumes it) and `max_live`.
- A provider's own id for a session (Codex's thread id) is the host's to keep (session_host.PROVIDER_FILE). This
  loop reports it to the hub with the status, for the record and the human, and never takes one from the hub.
- Child sessions (hosted_tree.py). A hub that has them says more about a session: an `access` word (None on a
  root: "as your runner has it"), and for a child its `parent`, `isolation` and `effort`. The host turns the word
  into flags from this machine's runner and settings (session_access.prepare) and this loop reports the access the
  session really has. A child with `isolation: worktree` runs in a git worktree of its own
  (session_worktrees.py, recorded in WORKTREES_FILE), made when it is first opened and removed once the session has
  ended and it holds nothing unique. A hub that says none of this gets none of it: the session is opened and
  reported exactly as before.
- This node says it speaks that protocol on every fetch (`tree=1`), and only then does the hub list children to it.
- Restart (`tree` 4, SESSION_PROTOCOL; DESIGN_NOTES "Restarting a running turn"). A hub that has it lists a queued
  message sent with mode `restart` with its `mode` and `into` (the turn in flight it replaces); the hub accepts one
  only when nothing else is queued for the session, so it is the session's next message. The node asks the host to
  interrupt that turn in-band (`interrupt_for`, as a cancel goes to the host) and otherwise treats the message as the
  ordinary queued message it is: handed over by the ordinary path once the turn has ended, under the ordinary
  watermark. Reports say what the session's adapter can do (`modes`) and carry `restarted_by` on a turn's end, to a
  hub that names the feature `restart` in its listing.
- Steering (`tree` 5; DESIGN_NOTES "Mid-turn steering, loose contract", parts E and G). A hub that has it lists a
  steer S on its session's entry (`steer`: N, S's uuid, its text), only from an offer it revalidated in that
  listing's own transaction. This loop passes it to the host (`steer_offer`) with whether the node's WRITE AUTHORITY
  held for that offer, and passes an omission (any applied listing that does not list S) as a revocation for good
  (`steer_revoke`). Write authority: the node's contact is fresh by the hub's own evidence, its last hub-acknowledged
  hello (`note_hello`), or report whose answer names N as the session's turn in flight, SENT under
  STEER_CONTACT_SECONDS before (listing GETs do not count). An offer is used only if the fetch that brought it was
  sent while that held and it has not lapsed since, and the fetch's age on the monotonic clock is under
  STEER_OFFER_AGE when its answer is read; it must still hold at every write (`_steer_authority`). Loss is one-way:
  the deadline watchdog revokes a steer whose contact lapsed (`_steer_watch`) and cuts a frame half written past
  its limit (session_host.steer_deadlines). The text is held to the hub's cap again (STEER_MAX_BYTES). While a steer
  is held, the session's status is posted at least every STEER_STATUS_SECONDS. A turn's end carries the host's
  `steer` outcome to a hub that names `steer`; to any other it goes at once without it, and a 422 sends it again
  without it (`_for_hub`, the 422 path).
- A child runs only under its parent AS THE PARENT IS HERE NOW. Roots are reconciled first; a child is opened, and
  handed a turn, only while its parent is open on this node and has at least the child's access (`_parent_problem`).
  The hub's stored words are not enough: this machine's runner may have been edited since. Otherwise the child is
  ended with the reason.
- `full` under a parent needs the parent's LIVE process to have said it runs that way (session_host: `ceiling`).
  While the parent has no process, one that is going down, or a new one that has not said so, a `full` child is
  held: it stays open, its messages stay queued at the hub, and no turn is handed to it. That is decided from the
  parent's process as it is at the moment the child is considered, so a parent that is about to be started in a
  pass never lends an earlier process's word to a child in that pass. The hub is told the ceiling with every
  status, and refuses to make such a child meanwhile. A parent whose process a working full child depends on is
  not stopped to make room, nor for being idle; one whose full child is only waiting is spared while anything else
  can be stopped (`_admit`, `_lends_full`). A node whose `max_live` is 1 cannot run a full child at all, and such
  a child is refused (at the hub, which knows the number from the node's hello) or ended here with the reason.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import session_access, session_worktrees
from .dispatch import DispatchConfig
from .session_host import STEER_CONTACT_SECONDS, HostEvent, SessionHost, received_under

log = logging.getLogger("agent_comms.node_sessions")


def _finite(meta: dict) -> dict:
    """Optional metadata with every non-finite number left out, and every string made valid Unicode (`_clean`): it
    could not be serialised in a report otherwise."""
    import math
    return {_clean(k): _clean(v) for k, v in meta.items() if not (isinstance(v, float) and not math.isfinite(v))}


def _clean(v):
    """(Review round 2, shoulds) A string with invalid Unicode (a lone surrogate) made encodable: each such code
    point replaced. Anything else as it is."""
    if isinstance(v, str):
        return v.encode("utf-8", "replace").decode("utf-8") if not v.isascii() else v
    return v

TICK_SECONDS = 0.2          # between passes while any session has work
SHUTDOWN_BOUND_MAX = 3600.0     # hosted.SHUTDOWN_BOUND_MAX (named here, so the node does not import the hub's module)
SYNC_SECONDS = 1.0          # between fetches of the hub's list while any session has work
LONG_POLL_SECONDS = 20      # how long one fetch may wait at the hub when nothing has work
RETRY_SECONDS = 2.0         # after a hub call failed, with nothing else to do
WATCH_SECONDS = 0.5         # how often the deadline watchdog looks (`_watch`): the self-stop and the kills after it
LOCK_SLACK_SECONDS = 2.0    # the most the watchdog waits for the loop's lock (the loop holds it only for local work)


def shutdown_bound(self_stop_seconds: float, kill_grace: float, watch_seconds: float | None = None) -> float:
    """How long after its session loop's last successful hub call this node may still be stopping a turn's process:
    the bound the loop states to the hub (`_sync`, hosted.may_still_run), derived here from what it depends on.

    Let S be when that call was SENT (`_call` takes the send time: the hub recorded the contact at or after it,
    hosted.note_contact, so the hub counts from S or later). No hub call, however long it blocks, delays either
    deadline: the watchdog (`_watch`), off the hub-I/O path, looks every `watch_seconds` and may wait up to
    LOCK_SLACK_SECONDS for the loop's lock:
      SIGTERM to every turn's process group   <= S + self_stop + (watch + slack)
      SIGKILL to what is left of each group   <= SIGTERM + kill_grace + (watch + slack)
    and a SIGKILLed group is gone (a member that outlives SIGKILL is limit c). So the bound is
      self_stop + kill_grace + 2 * (watch + slack)
    (with the shipped values 90 + 10 + 2 * 2.5 = 105 s). The constants are read when it is called: raising one
    raises the bound.

    It assumes the loop's lock is free within LOCK_SLACK_SECONDS. The loop also holds it for local git worktree
    work (a child's own worktree made or removed), bounded only by each git command's own timeout: if that runs
    longer while the hub hears nothing from this node, the watchdog is late by as much (limit e, DESIGN_NOTES; it
    says so in the log, `enforce_deadlines`)."""
    watch = WATCH_SECONDS if watch_seconds is None else watch_seconds
    return float(self_stop_seconds + kill_grace + 2 * (watch + LOCK_SLACK_SECONDS))
JOIN_SECONDS = 1.0          # shutdown waits this long for a pass in progress; a loop waiting at the hub is not waited for
REPORT_WAIT_SECONDS = 25.0  # ... and this long for a report the loop is in the middle of posting (CALL_TIMEOUT is 20 s)
ACK_LOG_MAX = 512           # acknowledgements kept for the continuity of a fetch (`_publish_ack`, `_offer_contact`)
BAD_OFFER_LOG_SECONDS = 300.0   # a session's offers that cannot be taken are said at most once in this long (R9-01)
MAX_BATCH = 100             # events in one report
HANDED_FILE = "node-session-handed.json"   # session id -> number of the last message handed to its host
SESSION_PROTOCOL = 5        # `tree` on every fetch and hello: 2 spawn, 3 the hold, 4 restart, 5 steer (hosted_tree)
# Write authority: the last hub-acknowledged hello or report naming N, sent within STEER_CONTACT_SECONDS (75 s,
# defined once, in session_host, where the attempt's record renews by it) ...
STEER_OFFER_AGE = 20.0          # ... and an offer is used only from a fetch younger than this when its answer is read
STEER_STATUS_SECONDS = 20.0     # while a steer is held, the session's status is posted at least this often
STEER_MAX_BYTES = 12_288        # hosted_steer.STEER_MAX_BYTES (named here, so the node does not import the hub's module)
CHECKPOINT_SECONDS = 5.0        # Claude steering: the checkpoint's own call, at most this long (it never waits for more)
SEQ_FILE = "node-session-seq.json"         # {"seq": the highest report stamp this node has issued}
WORKTREES_FILE = "node-session-worktrees.json"   # session id -> {path, checkout}: a child session's own worktree
SWEEP_SECONDS = 3600.0      # how often the worktrees of sessions that are gone are looked at again
PARENT_ENDED = "its parent session ended"
PARENT_GONE = ("its parent session is not open on this node (it could not be opened here, or has ended): a child "
               "session runs only under its parent")
PARENT_UNKNOWN = "its parent session's access is not known on this node"
ROOT_ACCESS = ("the hub named an access level for a session that has no parent. A session of its own runs as this "
               "node's runner for its agent says; an access word is only taken for a child, which is held to its "
               "parent's")
NO_ROOM_FOR_FULL = ("this node runs at most {n} session process at a time ([dispatch.sessions] max_live): a "
                    "full-access child takes turns only while its parent's process is up beside its own, so it "
                    "needs max_live of at least 2")
ABOVE_PARENT = ("its access ({have}) is above what its parent session has on this node now ({limit}): a child session "
                "never has more access than its parent")
LOST = "the node restarted while this turn was in flight; it was not replayed"
CANCELLED = "cancelled by the session that started it"      # hosted_results.CANCELLED: the hub's outcome reads it
FRESH_START = (". Before that the agent refused to resume this session's conversation: the next message starts a NEW "
               "one, without the earlier turns")


def _bind_of(view: dict) -> str | None:
    """The binding key the hub gave a session (hosted_spawn.bind_key: 64 hex), or None."""
    bind = view.get("bind")
    return bind if isinstance(bind, str) and len(bind) == 64 and all(ch in "0123456789abcdef" for ch in bind) \
        else None


class _Stopping(threading.Event):
    """The node's stopping flag. Setting it first marks every held steer attempt stopping, under that record's lock
    (SessionHost.steer_stopping, R4-01): a steer write either returned before, or does not happen. Otherwise the
    plain event."""

    def __init__(self, host):
        super().__init__()
        self._host = host

    def set(self) -> None:
        self._host.steer_stopping()
        super().set()


class SessionRunner:
    def __init__(self, client: httpx.Client, config: DispatchConfig, log_dir: Path, *, host: SessionHost | None = None,
                 clock: Callable[[], float] = time.monotonic, self_stop_seconds: float = 90.0,
                 long_poll: float = LONG_POLL_SECONDS, sync_seconds: float = SYNC_SECONDS,
                 watch_seconds: float | None = None, **host_kw):
        self.client, self.log_dir, self.clock = client, Path(log_dir), clock
        self.config = config
        self.host = host or SessionHost(config, self.log_dir, clock=clock, **host_kw)
        self.host.turn_gate = self._turn_allowed      # a child's turn is held to its parent when it is WRITTEN, too
        self.host.admit = lambda s: self._admit(s, starting=True)    # every process start takes a slot here
        self.host.may_start = self._may_start       # ... and none, nor any new turn, past the self-stop deadline
        self.host.may_start_query = self._may_start_query   # the same, with no side effect (turn correlation's)
        self.host.steer_authority = self._steer_authority     # ... and no steer byte without the node's write authority
        self.host.steer_capable = self._steer_capable         # ... nor a Claude one unless the hub names `held`
        # The node's acknowledged EVIDENCE (the module text): when the last hub-acknowledged hello was SENT (node.py
        # says it, `note_hello`), per session the send time of the last acknowledged report and the turn in flight its
        # answer named, and every acknowledgement in order (`ack_log`: seq, sent, received, the session a report was
        # for or None for a hello, the N it named). Authority itself lives only in each attempt's record
        # (session_host.SteerSlot, review round 3, structure 1); `last_fetch` is the latest fetch, frozen when its
        # answer came: (sent, received, the evidence when it was sent, the log's seq when sent and when received).
        self.hello_ack = float("-inf")
        self.report_ack: dict[str, tuple[float, Any]] = {}
        self.ack_log: list[tuple[int, float, float, str | None, Any]] = []
        self.ack_seq = 0
        self.last_fetch: tuple | None = None
        # The evidence above, between the loop, node.py's hellos and the watchdog: its own short lock, never the
        # host's (R2-09). Order: the host's lock (where held), then this, then an attempt's.
        self.ack_lock = threading.RLock()
        # (R8-04, R9-01) The steer diagnostics, each bounded by construction:
        # - a held attempt's reasons (another turn, not the held attempt): session -> (its uuid, the reasons said),
        #   at most one entry per open session, of at most two reasons; gone with the slot (a new attempt is said
        #   afresh);
        # - a refused report's 422: the steer-carrying events it was said for, gone when they leave the queue (at
        #   most one entry per such event pending);
        # - an offer that cannot be taken (malformed ids, an unknown session): session -> when it was last said,
        #   once per BAD_OFFER_LOG_SECONDS whatever the uuid offered; at most one entry per listed session, pruned
        #   to open ones.
        self._said_held: dict[str, tuple[str, set]] = {}
        self._said_422: list[tuple] = []
        self._said_bad: dict[str, float] = {}
        # session id -> {access, worktree}: what a hub with child sessions is also told about it (never an older one)
        self.tree: dict[str, dict] = {}
        self.parent_of: dict[str, str] = {}           # child session id -> its parent's, as the hub listed it
        # The full children whose subtree the hub's listing says is open (`subtree_open`, hosted_tree.subtree_open:
        # a session under it the hub has not recorded as ended, a result anywhere in it not yet delivered, claimed or
        # kept undelivered, or a message queued at the hub for it or under it). Such a child's next turn may come,
        # and needs its parent's live process: its parent is spared meanwhile (`_lends_full`). The hub's own record.
        self.subtree_open: set[str] = set()
        self.worktrees: dict[str, dict] = self._load_worktrees()
        self.swept = float("-inf")
        self.max_live, self.idle_seconds = config.sessions.max_live, config.sessions.idle_hours * 3600
        self.self_stop_seconds, self.long_poll, self.sync_seconds = self_stop_seconds, long_poll, sync_seconds
        self.watch_seconds = WATCH_SECONDS if watch_seconds is None else watch_seconds
        self.shutdown_bound = shutdown_bound(self_stop_seconds, self.host.kill_grace, self.watch_seconds)
        if self.shutdown_bound > SHUTDOWN_BOUND_MAX:
            # The hub keeps a slot for at most that long while it does not hear from this node (hosted.may_still_run):
            # a longer stop would outlast it. Refused at start-up rather than capped quietly.
            raise ValueError(f"[dispatch] kill_grace_seconds is too large for hosted sessions: this node could take "
                             f"{self.shutdown_bound:g} s to stop a session's process after the hub last heard from it, "
                             f"and the hub waits at most {SHUTDOWN_BOUND_MAX:g} s")
        self.pending: list[tuple[str, dict]] = []     # (session id, event) not yet taken by the hub, oldest first
        self.checkpoints: dict[str, dict] = {}       # Claude steering: a held end's checkpoint, sent once (`_checkpoint`)
        # (Review round 2, ruling K) The checkpoint sender's own queue: at most one per session, posted by its own
        # thread (`_send_checkpoints`), never by the loop that drives ordinary reports. Dropped at shutdown.
        self.checkpoint_queue: dict[str, dict] = {}
        self.checkpoint_gen: dict[str, Any] = {}      # (ruling P) the session incarnation each checkpoint was made in
        self.checkpoint_cond = threading.Condition()
        self.checkpoint_thread: threading.Thread | None = None
        self.queue: dict[str, list[dict]] = {}        # session id -> the hub's queued messages not yet handed over
        self.handed: dict[str, int] = self._load_handed()
        self.ending: set[str] = set()                 # sessions this node reported (or is reporting) ended
        self.dropped: set[str] = set()                # sessions the hub no longer lists: closed here, not reported
        # session id -> (state, pid, resume, the provider's own id when it is not the session id) as of the last pass
        self.status: dict[str, tuple] = {}
        self.reported: dict[str, tuple] = {}          # ... as the hub last took it
        self.last_busy: dict[str, float] = {}         # session id -> when it last had a turn or a message
        self.recovered: dict[str, dict] = {}
        self.version: str | None = None
        self.hub_features: set[str] = set()           # report fields the hub named as taken (its listing's `features`)
        self.features_known = False                   # ... as a listing applied since the last failed call said
        self.features_gen = 0                         # ... counted up at every listing that sets them (`_call`'s gen)
        self.refused: set[str] = set()                # sessions whose last report a hub that named it refused (422)
        self.last_sync = float("-inf")
        self.retry_at = float("-inf")
        self.fresh = False                            # the last hub call was a fetch that succeeded: `queue` is current
        # The last contact (`last_contact`, read-only; renewed only by `_renew_contact`). At start-up it is the start:
        # nothing can start before the first listing anyway (a session is opened, and its inbox filled, only from a
        # listing the loop applied; `recover()` only stops what an earlier process left and reads its records).
        self._last_contact = clock()
        self.lock = threading.Lock()                  # the host, between the loop, the watchdog and shutdown()
        self.watched: list[HostEvent] = []            # what the watchdog's self-stop reported, for the loop to post
        self.watched_steer: list[HostEvent] = []      # ... of a steer attempt's session: posted before LOST detection
        # When the node was last seen past its self-stop deadline, or None: from then on nothing starts until a
        # listing from a fetch SENT after that moment has been applied (`_may_start`, `pass_once`).
        self.latched_at: float | None = None
        self.latch_lock = threading.Lock()            # the latch, between the loop's calls, the loop and the watchdog
        self.report_lock = threading.Lock()           # the report queue: one flush at a time, shutdown's last
        self.seq = self._load_seq()                   # the last report's stamp (`_stamp`), an earlier process's included
        self.seq_unsaved = False                      # the last attempt to put it on disk failed (said once)
        self.hub_seq = 0                              # the highest stamp the hub says it applied, for any session
        self.sent: dict[str, int] = {}                # session id -> stamp of this process's last status the hub took
        self.stopping = _Stopping(self.host)
        self.thread: threading.Thread | None = None
        self.watch_thread: threading.Thread | None = None

    # ------------------------------------------------------------ local state (survives a node restart)

    def _handed_path(self) -> Path:
        return self.log_dir / HANDED_FILE

    def _load_handed(self) -> dict[str, int]:
        try:
            data = json.loads(self._handed_path().read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and type(v) is int} if isinstance(data, dict) else {}

    def _write_private(self, path: Path, data: Any) -> None:
        """Atomically, mode 600 (as session_host's state file)."""
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _save_handed(self) -> None:
        self._write_private(self._handed_path(), self.handed)

    def _load_worktrees(self) -> dict[str, dict]:
        try:
            data = json.loads((self.log_dir / WORKTREES_FILE).read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)
                and isinstance(v.get("path"), str) and isinstance(v.get("checkout"), str)}

    def _save_worktrees(self) -> None:
        try:
            self._write_private(self.log_dir / WORKTREES_FILE, self.worktrees)
        except OSError as e:
            log.warning("could not record the session worktrees in %s (%s)", WORKTREES_FILE, e)

    def _load_seq(self) -> int:
        try:
            seq = json.loads((self.log_dir / SEQ_FILE).read_text()).get("seq")
        except (OSError, ValueError, AttributeError):
            return 0
        return seq if type(seq) is int and seq > 0 else 0

    def recover(self) -> None:
        """Once, before any hub contact: stop the session processes an earlier node process left here."""
        self.recovered = self.host.recover()
        for sid, d in self.recovered.items():
            turn = d.get("turn")
            if isinstance(turn, str) and turn.isdigit() and int(turn) > self.handed.get(sid, 0):
                self.handed[sid] = int(turn)
                self._save_handed()

    def usage_report(self) -> dict[str, list[dict]]:
        """For the node's hello, to a hub that asked (account_pools.py): per runner, each window's latest plan usage
        its sessions' providers reported, with that window's own age in seconds (never a time of this machine's
        clock), at most a day old, each through providers.clean_usage. It never raises: advisory."""
        from .providers import clean_usage
        try:
            now = self.clock()
            with self.host.usage_lock:
                seen = {k: dict(v) for k, v in self.host.usage.items()}
            out = {}
            for key, windows in sorted(seen.items()):
                # (clean_usage drops one older than providers.USAGE_MAX_AGE)
                kept = [c for c in (clean_usage(o | {"age": max(0.0, now - o["at"])}, age=True)
                                    for o in windows.values()) if c is not None]
                if kept:
                    out[key] = kept
            return out
        except Exception:
            log.debug("the usage report could not be made; it is advisory and is left out", exc_info=True)
            return {}

    # ------------------------------------------------------------ hub calls

    def _call(self, method: str, path: str, *, gen: int | None = None, spare_422: bool = False, **kw) -> Any:
        # The contact counts from when the call was SENT: the hub records it at or after that (hosted.note_contact),
        # so the node's self-stop clock never runs behind the hub's (`shutdown_bound`).
        sent = self.clock()
        try:
            r = self.client.request(method, path, **kw)
            r.raise_for_status()
        except Exception:
            # Whatever answers next may be another hub (one rolled back to an earlier version): until a fetch says
            # again which report fields it takes, none beyond the first ones are sent (`_for_hub`), and an event that
            # needs one waits (`_takes`). A call made under a listing's features (`gen`: a report carrying a steer
            # outcome) forgets only THOSE: a refusal of it that comes after a newer listing leaves that one's (R4-03).
            # `spare_422`: a 422 forgets nothing here; the caller decides what it says (R5-05).
            code = getattr(getattr(sys.exc_info()[1], "response", None), "status_code", None)
            if not (spare_422 and code == 422) and (gen is None or gen == self.features_gen):
                self.hub_features, self.features_known = set(), False
            raise
        self._renew_contact(sent)
        return r.json()

    def _add(self, session_id: str, event: dict) -> None:
        if session_id not in self.dropped:
            self.pending.append((session_id, event))

    @staticmethod
    def _event(e: HostEvent) -> dict:
        """A HostEvent as the hub takes it (hosted.report). Message ids are the hub's message numbers."""
        d: dict[str, Any] = {"kind": e.kind}
        if e.message_id is not None and e.message_id.isdigit():
            d["message"] = int(e.message_id)
        if e.kind == "turn_ended":
            d |= {"ok": bool(e.ok), "delivered": bool(e.delivered), "detail": e.detail[:300],
                  "denials": list(e.denials), "meta": dict(e.meta), "text": e.text,
                  "answered": bool(e.answered)}     # the provider itself ended it (HostEvent.answered)
            if isinstance(e.restarted_by, str) and e.restarted_by.isdigit():
                d["restarted_by"] = int(e.restarted_by)     # the provider said that restart's interrupt ended it
            if isinstance(e.steer, dict):
                d["steer"] = dict(e.steer)      # the host's word on the steer into this turn (never waits: `_takes`)
                if isinstance(d["steer"].get("result"), dict):      # (Claude: S's own result, finite numbers only)
                    res = d["steer"]["result"]
                    d["steer"]["result"] = dict(res) | {
                        "meta": _finite(dict(res.get("meta") or {})), "text": _clean(res.get("text", "")),
                        "detail": _clean(res.get("detail", ""))}
        elif e.kind == "approval_requested":
            d["denials"] = list(e.denials)
        elif e.kind == "ended":
            d["detail"] = e.detail[:300]
        return d

    # Sent only to a hub whose listing names their feature (hosted.NODE_REPORT_FEATURES): field -> feature.
    OPTIONAL_FIELDS = {"answered": "answered", "restarted_by": "restart", "steer": "steer"}

    def _for_hub(self, event: dict) -> dict:
        """The event as this hub takes it: without a field it has not named in its listing's `features`."""
        drop = [k for k, f in self.OPTIONAL_FIELDS.items() if k in event and f not in self.hub_features]
        return {k: v for k, v in event.items() if k not in drop} if drop else event

    def _takes(self, event: dict) -> bool:
        """This event may go to the hub now. A turn's end with `restarted_by` (what only a hub of the restart protocol
        reads) waits while what the hub takes is not known (a call failed and no listing has said it again): never
        sent stripped of it to a hub that may take it, and (`_post_pending`) nothing of its session goes before it.
        To a hub that said it does not take it (one from before restarts) it goes without it (`_for_hub`): that hub
        records the turn as failed, with the detail that says it was interrupted (and a parent hears of it as of any
        turn: R4-02)."""
        return "restarted_by" not in event or "restart" in self.hub_features or self.features_known

    def _emit(self, events: list[HostEvent]) -> None:
        if self.watched_steer:  # (a steer attempt's watched ends first, as in `pass_once`)
            events, self.watched_steer = self.watched_steer + list(events), []
        if self.watched:        # (under the lock, like every caller of this: what the watchdog stopped comes first)
            events, self.watched = self.watched + list(events), []
        self._dispatch(events)

    def _dispatch(self, events: list[HostEvent]) -> None:
        """(Review round 3, ruling M) THE one way a host event reaches the hub, whatever its source (the loop's tick,
        the watchdog's lists, a close, a cancel, a stop of every turn, shutdown, what a failed tick left in the
        host's outbox): a checkpoint (`held`) to the checkpoint sender, every other event into the report queue."""
        for e in events:
            if e.kind == "held":
                self._checkpoint(e)     # (Claude steering: a report of its own, never one of the events)
                continue
            if e.kind == "ended":
                self.ending.add(e.session_id)
                self.queue.pop(e.session_id, None)
            self._add(e.session_id, self._event(e))

    def _checkpoint(self, e: HostEvent) -> None:
        """The checkpoint of a held end (Claude steering, part G): sent alone, in a report of its own, after that
        session's pending events, only when the hub's last listing named `steer` and what it takes is known. It never
        waits, never holds, reorders or joins an ordinary event, and is dropped (logged) on refusal or failure, never
        resent."""
        if e.message_id is None or not e.message_id.isdigit() or e.session_id in self.dropped:
            return
        if not ("held" in self.hub_features and self.features_known):
            # (Review round 1, ruling C) A feature of its own: a step-2 hub names `steer` but takes no checkpoint.
            log.info("session %s: the checkpoint of message %s is not sent: the hub has not said it takes it",
                     e.session_id, e.message_id)
            return
        s = self.host.sessions.get(e.session_id)
        self.checkpoint_gen[e.session_id] = s.gen if s is not None else None    # (ruling P) its incarnation
        self.checkpoints[e.session_id] = {"message": int(e.message_id), "ok": bool(e.ok),
                                          "detail": _clean(e.detail[:300]), "denials": list(e.denials),
                                          "meta": _finite(dict(e.meta)), "text": _clean(e.text),
                                          "answered": bool(e.answered)}

    def _post_checkpoints(self, ready: dict) -> None:
        """The checkpoints taken out of the queue by `_flush` (each one's session had nothing pending before it),
        posted OUTSIDE the report lock, one try each, with CHECKPOINT_SECONDS at most: its own call, so its failure,
        refusal or stall changes nothing else (no feature knowledge forgotten, no contact renewed, no report of
        another session stripped or held up longer than that). Ruling C."""
        for sid, held in ready.items():
            if self.checkpoint_thread is not None and not self._checkpoint_due(sid, held):
                continue        # (ruling P) rechecked immediately before the POST
            try:
                r = self.client.request("POST", f"/api/nodes/sessions/{sid}", json={"events": [], "held": held},
                                        timeout=CHECKPOINT_SECONDS)
                r.raise_for_status()
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                log.warning("session %s: the checkpoint of message %s was not taken (%s); it is not sent again", sid,
                            held["message"], code if code is not None else type(e).__name__)

    def _flush(self, final: bool = False) -> bool:
        """Post what the hub has not taken yet, per session and in order. False: the hub could not be reached (the
        rest is kept). A report the hub refuses for good (it does not know the session, or cannot read the report)
        is dropped, so one bad event never holds the others back. Once shutdown has begun only shutdown's own flush
        (`final`) posts: the loop's would race it, and an older status sent after shutdown's would bring a dead pid
        back to life on the hub."""
        with self.report_lock:
            if self.stopping.is_set() and not final:
                return False
            ok = self._post_pending()
            # (Claude steering) the checkpoints whose sessions have nothing pending before them (part G), taken out
            # of the queue here and posted after the lock is let go (ruling C)
            ready = {sid: self.checkpoints.pop(sid) for sid in list(self.checkpoints)
                     if not any(x == sid for x, _ in self.pending)}
        if ready and not self.stopping.is_set():
            if self.checkpoint_thread is not None:
                with self.checkpoint_cond:
                    self.checkpoint_queue.update(ready)     # (at most one per session: a later one replaces it)
                    self.checkpoint_cond.notify()
            else:
                self._post_checkpoints(ready)       # (no sender thread: a node driven pass by pass, as in tests)
        return ok

    @staticmethod
    def _modes_for(modes, features) -> list:
        """What the session's modes say to a hub with these features (review round 3, ruling O): `steer` only to a
        hub that steers; a Claude session's `steer` and `held` (its adapter holds N's end) as a PAIR, both or
        neither, and only to a hub that names both: never `["restart", "held"]`."""
        claude = "held" in modes
        pair = "steer" in features and "held" in features
        return [m for m in modes if (m not in ("steer", "held") if claude and not pair
                                     else m != "steer" or "steer" in features)]

    @staticmethod
    def _base_modes(body: dict) -> list | None:
        """The report's modes without the steering capabilities (`steer`, `held`), when it had any; else None."""
        modes = body.get("modes")
        if not isinstance(modes, list) or not ({"steer", "held"} & set(modes)):
            return None
        return [m for m in modes if m not in ("steer", "held")]

    def _send_checkpoints(self) -> None:
        """The checkpoint sender's thread (ruling K): one queued checkpoint at a time, each rechecked at dispatch
        (`_checkpoint_due`) and posted with its own timeout; ordinary reports never wait for it. Ends with shutdown;
        what is still queued then is dropped (nothing waits for it)."""
        while not self.stopping.is_set():
            with self.checkpoint_cond:
                if not self.checkpoint_queue:
                    self.checkpoint_cond.wait(0.5)
                    continue
                sid = next(iter(self.checkpoint_queue))
                held = self.checkpoint_queue.pop(sid)
            self._post_checkpoints({sid: held})     # (rechecked there, immediately before the POST: ruling P)
        with self.checkpoint_cond:
            self.checkpoint_queue.clear()

    def _checkpoint_due(self, sid: str, held: dict) -> bool:
        """At dispatch (ruling K): the hub's current capability still names `held`, and the session and N are still
        the ones the checkpoint was made for (hosted here, not dropped, N's end still held). Else it is dropped."""
        if self.stopping.is_set():
            return False        # (ruling P) never once shutdown began
        if not ("held" in self.hub_features and self.features_known) or sid in self.dropped:
            return False
        s = self.host.sessions.get(sid)
        h = s.held if s is not None else None
        return (h is not None and h.end.message_id == str(held.get("message"))
                and self.checkpoint_gen.get(sid, s.gen) == s.gen)

    wall_ns = staticmethod(time.time_ns)

    def _stamp(self) -> int:
        """An id for the next report that is above every earlier one of this node: the hub ignores the status of a
        report that is not newer than the last it applied, so one that arrives late (its call timed out here, then it
        committed there) cannot undo a later one. Above the last stamp issued, the wall clock, and the highest stamp
        the hub says it applied (it comes with every fetch).

        The last stamp issued is on disk (SEQ_FILE) before the report that carries it is sent, and the next node
        process starts from it. So whatever the clock did across a restart, the new process never issues a stamp an
        old one issued, nor one below it: an old report can neither tie with a new one and win by arriving first, nor
        overwrite the final report of a shutdown after the node has stopped fetching. If the file cannot be written
        the report goes out all the same (the hub's floor still orders what the hub has applied)."""
        self.seq = max(self.seq + 1, self.wall_ns(), self.hub_seq + 1)
        try:
            self._write_private(self.log_dir / SEQ_FILE, {"seq": self.seq})
            self.seq_unsaved = False
        except OSError as e:
            if not self.seq_unsaved:
                log.warning("could not record the report stamp in %s (%s); reporting goes on", SEQ_FILE, e)
            self.seq_unsaved = True
        return self.seq

    def _post_pending(self) -> bool:
        order: list[str] = []
        for sid, _ in self.pending:
            if sid not in order:
                order.append(sid)
        order += [sid for sid, st in self.status.items() if sid not in order and self.reported.get(sid) != st]
        sent_items: set[int] = set()
        for sid in order:
            # In order, up to the first event this hub may not take yet (`_takes`): nothing of the session overtakes
            # it, so the hub applies them as they happened.
            mine = []
            for i, (x, e) in enumerate(self.pending):
                if x != sid:
                    continue
                if not self._takes(e) or len(mine) >= MAX_BATCH:
                    break
                mine.append((i, e))
            events = [e for _, e in mine]
            if not events and (self.status.get(sid) is None or self.reported.get(sid) == self.status.get(sid)):
                continue        # only events this hub does not take wait for this session: nothing to post now
            # A field the hub has not named as one it takes (its listing's `features`) is not sent: a hub from
            # before it refuses the whole report, and its turn ends would be lost.
            body: dict[str, Any] = {"events": [self._for_hub(e) for e in events], "seq": self._stamp()}
            status = self.status.get(sid)
            if status is not None:
                body |= {"state": status[0], "pid": status[1], "resume": status[2]}
                if status[3] is not None:
                    # Codex's thread id: for the hub's record, never read back. (A hub from before the field refuses
                    # the report; hubs are upgraded before nodes.)
                    body["provider_id"] = status[3]
                # The access the session really has here and a child's own worktree, for a hub that asked.
                body |= {k: v for k, v in self.tree.get(sid, {}).items() if v is not None}
                if "restart" in self.hub_features:
                    # what its adapter can do with a turn in flight now (`steer` only to a hub that steers)
                    # (Review round 2, ruling J) a Claude session's steering (`steer` with `held`) only to a hub
                    # whose listing names `held`: a step-2 hub never sees a Claude steer offered
                    body["modes"] = self._modes_for(status[4], self.hub_features)
            named = bool(self.hub_features)     # (a failed call forgets them: `_call`)
            try:
                sent = self.clock()     # its SEND time: a hub-acknowledged report renews the write authority from it
                path = f"/api/nodes/sessions/{sid}"
                if not any("steer" in x for x in body["events"]):
                    answer = self._call("POST", path, json=body)        # main's call
                else:
                    # A report carrying a steer outcome: main's call, and after a 422 (a hub that named `steer`
                    # refuses it now: rolled back) one plain retry at once, in this very flush, synchronous, with
                    # main's per-call timeout (S2-07, R4-03). Each attempt has its own payload, built from `body`,
                    # which neither changes; both are made under this listing's features (`gen`), so a refusal
                    # never forgets those of a newer listing. In the final flush exactly the same.
                    gen = self.features_gen
                    first = json.loads(json.dumps(body))
                    try:
                        answer = self._call("POST", path, json=first, gen=gen, spare_422=True)
                    except Exception as e:
                        if getattr(getattr(e, "response", None), "status_code", None) != 422:
                            raise
                        self._say_422(sid, tuple(e for e, x in zip(events, body["events"]) if "steer" in x))
                        plain = {k: v for k, v in body.items() if k != "events"}
                        plain["events"] = [{k: v for k, v in x.items() if k != "steer"} for x in body["events"]]
                        sent = self.clock()
                        answer = self._call("POST", path, json=plain, gen=gen)
                        # (R5-05) The plain retry got through: this hub takes everything else this listing said, so
                        # only `steer` is forgotten, and only while that listing's features stand. (A failed retry
                        # forgot every feature in `_call`, as main does for a failed report.)
                        if gen == self.features_gen:
                            self.hub_features = self.hub_features - {"steer"}
                self._ack_report(sid, sent, answer)
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                base = self._base_modes(body)
                if code in (400, 422) and base is not None:
                    # (Review round 3, ruling O) The hub rejected what the report said the session can do (a hub
                    # rolled back to step 2 refuses `held`): the capabilities are dropped as a pair and the report,
                    # its native ends with it, is sent again at once with the base modes. Never discarded for that.
                    log.warning("the hub refused the report for session %s (%s) with its steering modes: sending it "
                                "again without them", sid, code)
                    self.hub_features = self.hub_features - {"steer", "held"}
                    try:
                        sent = self.clock()
                        answer = self._call("POST", f"/api/nodes/sessions/{sid}", json=dict(body, modes=base))
                        self._ack_report(sid, sent, answer)
                        sent_items.update(i for i, _ in mine)
                        if status is not None:
                            self.reported[sid] = status
                        continue
                    except Exception as e2:
                        e, code = e2, getattr(getattr(e2, "response", None), "status_code", None)
                if code not in (400, 404, 422):
                    log.warning("could not report session %s: %s", sid, e)
                    self._drop_sent(sent_items)
                    return False
                if code == 422 and named and sid not in self.refused:
                    # Refused whole by a hub that, as its last listing said, takes what the report carried: it may
                    # have been rolled back since. Nothing is dropped: what it takes is not known until a listing says
                    # it again (only the first fields are sent meanwhile, and `restarted_by` waits), and the report
                    # goes again after it, once. Refused again after that, it is dropped as below.
                    self.refused.add(sid)
                    log.warning("the hub refused the report for session %s as malformed (422): asking again what it "
                                "takes before reporting again", sid)
                    self._drop_sent(sent_items)
                    return False
                if code == 422:
                    # The hub could not read the report at all: most likely it is older than this node (update the
                    # hub before its nodes). Said once per report, at error level, as it costs the turn ends in it.
                    log.error("the hub refused the report for session %s as malformed (422), so the %d event(s) in "
                              "it are lost: the hub is probably older than this node. Update the hub to this "
                              "version (hubs before nodes)", sid, len(events))
                else:
                    log.warning("hub refused the report for session %s (%s); dropping it", sid, code)
            self.refused.discard(sid)
            if status is not None:
                self.reported[sid] = status
                self.sent[sid] = body["seq"]
            sent_items |= {i for i, _ in mine}
        self._drop_sent(sent_items)
        return True

    def _drop_sent(self, sent_items: set[int]) -> None:
        """Drop exactly the events this pass sent (appends during the flush only ever come after them), keeping the
        order: also when a later session's report stops the pass, so an earlier session's events are not sent again."""
        self.pending = [item for i, item in enumerate(self.pending) if i not in sent_items]
        self._prune_422()

    def _say_422(self, sid: str, steer_events: tuple) -> None:
        """(R8-04, R9-01) The 422-and-retry warning, once per report: per set of steer-carrying events (the queue's
        own objects), for as long as they wait in the queue."""
        self._prune_422()
        if any(len(k) == len(steer_events) and all(a is b for a, b in zip(k, steer_events)) for k in self._said_422):
            return
        self._said_422.append(steer_events)
        log.warning("the hub refused the report for session %s with a steer outcome (422): sending it again without "
                    "the outcome", sid)

    def _prune_422(self) -> None:
        if self._said_422:
            queued = [e for _, e in self.pending]
            self._said_422 = [k for k in self._said_422 if all(any(e is q for q in queued) for e in k)]

    def _sync(self, wait: float) -> dict:
        params: dict[str, Any] = {"wait": wait, "tree": SESSION_PROTOCOL,   # children, spawn, the hold, restart
                                  # how long it may take to stop a session's process after the hub last heard from it
                                  # (`shutdown_bound`), stated before any work is listed to it; and the listing it last
                                  # applied (hosted.may_still_run)
                                  "bound": self.shutdown_bound}
        if self.version:
            params["applied"] = self.version
        if wait and self.version:
            params["seen"] = self.version
        if not wait:
            return self._call("GET", "/api/nodes/sessions", params=params)
        return self._call("GET", "/api/nodes/sessions", params=params, timeout=httpx.Timeout(wait + 20, connect=5))

    # ------------------------------------------------------------ one pass

    def active(self) -> bool:
        """Something needs the short cadence: a turn, a message to hand over, a process going down, a report. Not
        a message that only waits for its session's parent (a full child whose parent has no live process that
        confirmed full access): that can last hours, nothing here can end the wait, and the parent's own next turn
        is work enough to bring the short cadence back for it. Nor one the hub holds back, listed without its text (an
        agent's message to its child while a dispatched run uses the checkout): the hub's next answer brings it."""
        return bool(self.pending or any(q and not self._waits_on_parent(sid) and isinstance(q[0].get("text"), str)
                                        and not self._start_blocked(self.host.sessions.get(sid))
                                        for sid, q in self.queue.items())
                    or any(self.reported.get(sid) != st for sid, st in self.status.items())
                    or any(s.busy or (s.inbox and not self._held_inbox(sid, s) and not self._start_blocked(s)) or s.ended
                           or (s.child is not None and s.stopping_since is not None)
                           for sid, s in self.host.sessions.items()))

    def stopping_checkouts(self) -> set[str]:
        """The directories (real paths) that hold a process group of this node being stopped (node_groups)."""
        return {os.path.realpath(d) for d in self.host.groups.stopping_dirs()}

    def _start_blocked(self, s) -> bool:
        """This session has no process, and none may start now: a process group is being stopped in its checkout."""
        return s is not None and s.child is None and not s.ended and self.host.groups.blocked(s.cwd)

    def _held_inbox(self, sid: str, s) -> bool:
        """A message already handed to a child waits in its inbox only for the parent (its parent's process went
        while the child's was in its handshake): the child's process is up and past its handshake, so it has
        nothing to say meanwhile, and the parent's next turn is the work that brings the short cadence back."""
        return self._waits_on_parent(sid) and s.child is not None and s.state == "idle"

    def pass_once(self, wait: float | None = None) -> bool:
        """One pass: report, fetch the hub's list (waiting there when nothing has work), reconcile, hand over the next
        messages, tick the host, report. Returns False when the hub could not be reached; the host is ticked and held
        to its timeouts either way."""
        reach = self.clock() >= self.retry_at     # after a call failed, the hub is left alone for RETRY_SECONDS
        ok = reach and self._flush()
        if wait is None:
            wait = self.long_poll if ok and not self.active() else 0
        data = None
        fetch_sent = self.clock()
        if ok and (wait or not self.fresh or self.clock() - self.last_sync >= self.sync_seconds):
            try:
                with self.ack_lock:
                    fetch_sent = self.clock()
                    snapshot = (self.hello_ack, dict(self.report_ack)), self.ack_seq   # as it stood when it was sent
                data = self._sync(wait)
                with received_under(self.ack_lock, self.clock) as received:
                    # Frozen when the answer came (R3-03): its receipt time (the offer's age is judged here; read
                    # under the lock that freezes the history, R4-02) and the acknowledgements received while it was
                    # out; what comes later counts only as current authority.
                    self.last_fetch = (fetch_sent, received, snapshot[0], snapshot[1], self.ack_seq)
                self.last_sync = self.clock()
            except Exception as e:
                log.warning("could not fetch hosted sessions: %s", e)
                ok = False
        if data is not None:
            self.fresh = True
        elif not ok:
            # The queue and the stop flags held here may be out of date (the human may just have closed a session):
            # turns already running go on, nothing new is handed over until a fetch has succeeded again.
            self.fresh = False
        with self.lock, self.host.one_drain_budget():   # (R5-04) one drain deadline for this whole hold of the lock
            if self.stopping.is_set():
                return ok
            now = self.clock()
            if self.watched_steer:
                # What the watchdog drained or decided about a steer attempt (N's native end, a cut) goes into the
                # report queue BEFORE the listing is reconciled, so no synthetic LOST of N can come ahead of it (S2-06),
                # through the one dispatcher (ruling M: a checkpoint among them goes to the sender, as from a tick).
                events, self.watched_steer = self.watched_steer, []
                self._dispatch(events)
            # (Ruling M) What the host published and nobody took (a tick that raised after publishing) goes before
            # the listing is reconciled too: no LOST is synthesised for a turn whose native end is already decided.
            retained = self.host.take_outbox()
            if retained:
                self._dispatch(retained)
            if data is not None:
                self._reconcile(data, fetch_sent)       # (its closes and cancels share the hold's drain deadline)
                # Contact is back and a listing is applied (a close or a cancel the hub decided meanwhile has been,
                # above): what waits in an inbox may start again, in `tick` below. Only a listing from a fetch SENT
                # after the latch was set: one sent before may have been built before the hub decided.
                self._unlatch(fetch_sent, now)
            if self._silent(now):
                self._emit(self.host.stop_turns(self._self_stop_detail()))
            self._steer_watch(now)
            if self.fresh:
                self._hand_over(now)
            self._park_idle(now)
            events = self.host.tick()
            self._emit(events)
            if self.fresh and any(e.kind == "turn_ended" for e in events):
                self._hand_over(now)        # the message behind a turn that just ended is written in this same pass
                self._emit(self.host.tick())
            self._after_tick(now)
        if ok and (self.pending or any(self.reported.get(sid) != st for sid, st in self.status.items())):
            ok = self._flush()
        if reach and not ok:
            self.fresh = False
            self.retry_at = self.clock() + RETRY_SECONDS
        return ok

    def _held(self, s) -> set[int]:
        ids = [m.id for m in s.inbox] + [m.id for m in (s.writing, s.in_flight) if m is not None]
        return {int(i) for i in ids if i.isdigit()}

    def _reconcile(self, data: dict, fetch_sent: float | None = None) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            log.warning("the hub's session list is not one; ignoring it")     # never read as "no sessions"
            return
        # What this listing's hub says it takes, read FIRST: only a listing that names `steer` speaks for steers
        # (an offer, or an omission that revokes); any other fences them (S2-05).
        named = data.get("features")
        features = {f for f in named if isinstance(f, str)} if isinstance(named, list) else set()
        steer_aware = "steer" in features
        held_aware = "held" in features     # (ruling J) a Claude session's steer is only taken from a hub naming it
        listed = {v["session_id"]: v for v in data["sessions"]
                  if isinstance(v, dict) and isinstance(v.get("session_id"), str)}
        # Roots first, then each level under the one above it: a child is only opened under a parent that is open
        # here (`_parent_problem`), and a deeper tree's child's parent is itself a child.
        def depth(view: dict) -> int:
            n, cur = 0, view
            while isinstance(cur.get("parent"), str) and n < 10:
                n, cur = n + 1, listed.get(cur["parent"]) or {}
            return n

        for sid, view in sorted(listed.items(), key=lambda kv: depth(kv[1])):
            applied = view.get("status_seq")
            if type(applied) is int:
                self.hub_seq = max(self.hub_seq, applied)
                if applied > self.sent.get(sid, applied):
                    # The hub applied a status that is not this process's last one (an earlier node process's
                    # report arrived late, with a stamp from a clock that ran ahead): say again how it stands.
                    self.reported.pop(sid, None)
            s = self.host.sessions.get(sid)
            if sid in self.ending:
                if not any(x == sid and e["kind"] == "ended" for x, e in self.pending):
                    self._add(sid, {"kind": "ended", "detail": "the session ended on its node"})
                continue
            if view.get("stop"):
                why = PARENT_ENDED if view.get("stop") == "parent" else "closed by the human"
                if s is not None:
                    self._emit(self.host.close(sid, why))
                else:
                    self._ended_without_process(sid, why)
                continue
            parent = view.get("parent") if isinstance(view.get("parent"), str) else None
            if view.get("subtree_open") is True:
                self.subtree_open.add(sid)
            else:
                self.subtree_open.discard(sid)
            if parent is not None:
                self.parent_of[sid] = parent
                if self._parent_problem(parent, None) is not None:     # before anything is opened or made for it
                    self._end_child(sid, self._parent_problem(parent, None))
                    continue
            if s is None:
                left = self.recovered.pop(sid, None) or {}
                # Resume only a session the provider confirmed: the hub's record of it (the host's own `resume`,
                # reported with every status) or the last host's state file. That a message was handed over, or
                # even written, proves nothing: Claude refuses `--resume` for a conversation that does not exist
                # just as it refuses `--session-id` for one that does. A wrong guess heals in the host after one
                # refused start (session_host._correct_start_mode). For a provider with an id of its own (Codex) the
                # host's own record overrules this flag either way (`SessionHost.open`): a recorded id is resumed,
                # and a recorded fresh start is not, even when the hub (which may not have heard) says resume.
                resume = view.get("resume") is True or left.get("resume") is True
                try:
                    s = self.host.open(sid, agent=view.get("agent"), project=view.get("project"),
                                       runtime=view.get("runtime"), model=view.get("model"), resume=resume,
                                       **self._tree_open(sid, view))
                except Exception as e:
                    log.warning("cannot host session %s: %s", sid, e)
                    self._ended_without_process(sid, f"this node cannot host the session: {type(e).__name__}: {e}")
                    continue
                self.last_busy[sid] = self.clock()
            # Exactly the binding the hub lists now, nothing cached beyond it: given (a grant came) or withdrawn (a
            # revoke or an expiry, once no child it started is left). The next process gets it as it is; a process
            # started with another one takes no new message (the host's write boundary, session_host `unbound`) and
            # is stopped once idle (`_park_idle`), the conversation resuming in the next.
            s.bind = _bind_of(view)
            if parent is not None and self._parent_problem(parent, s) is not None:
                self._end_child(sid, self._parent_problem(parent, s))   # the access it has here is above its parent's
                continue
            if parent is not None and s.access == "full" and self.max_live < 2:
                self._end_child(sid, NO_ROOM_FOR_FULL.format(n=self.max_live))  # it could never take a turn here
                continue
            self._tree_status(sid, view, s)
            held, queue = self._held(s), []
            for m in sorted((m for m in view.get("messages", []) if isinstance(m, dict) and type(m.get("n")) is int),
                            key=lambda m: m["n"]):
                n = m["n"]
                if (m.get("mode") == "restart" and type(m.get("into")) is int and m.get("state") == "queued"
                        and m.get("cancel") is not True):
                    # Sent to replace the turn in flight `into`: that turn is interrupted in-band (when it is the
                    # host's turn in flight here, and its CLI can), as a cancel is passed to the host below; the
                    # message itself is the next ordinary one, handed over as any is once that turn has ended.
                    self.host.interrupt_for(sid, str(m["into"]), str(n))
                if m.get("cancel") is True:
                    # The session that started this turn cancelled it (hosted_spawn.cancel): stopped where it is if
                    # the host has it, never handed over if not. Reported once; the hub then lists it no more.
                    # Held here or not (a restart's: its interrupt is no longer asked for), every event the cancel
                    # returns is reported, in order: none is dropped (R2-04). One not held here that returned
                    # nothing emits nothing, as on main (R3-07: an empty emit would post the watchdog's ends ahead of
                    # the LOST detection below).
                    cancelled = self.host.cancel(sid, str(n), CANCELLED)
                    if n in held or cancelled:
                        self._emit(cancelled)
                    if n not in held and (n > self.handed.get(sid, 0) and m.get("state") == "queued"
                                          and not any(x == sid and e.get("message") == n for x, e in self.pending)):
                        self._add(sid, {"kind": "turn_ended", "message": n, "ok": False, "delivered": False,
                                        "detail": CANCELLED})
                    if n in held or n > self.handed.get(sid, 0):
                        continue
                if n > self.handed.get(sid, 0) and m.get("state") == "queued":
                    queue.append(m)
                elif n not in held and not any(x == sid and e.get("message") == n for x, e in self.pending):
                    # Handed to a host that no longer has it, and no result on its way: the turn was lost.
                    self.handed[sid] = max(self.handed.get(sid, 0), n)
                    self._save_handed()
                    # If the host found its own record that the provider refused to resume this session (the node
                    # died before that failure reached the hub), the human is told here what follows from it.
                    self._add(sid, {"kind": "turn_ended", "message": n, "ok": False,
                                    "detail": LOST + (FRESH_START if s.fresh_start else ""),
                                    "delivered": m.get("state") == "delivered"})
            self.queue[sid] = queue
            self._steer_listed(sid, view, fetch_sent, steer_aware and (held_aware or not self.host.holds(sid)))
        for sid, s in self.host.sessions.items():
            live = self.host.steer_live(sid)
            if live is not None and sid not in listed:
                if steer_aware and (held_aware or not self.host.holds(sid)):
                    self.host.steer_revoke(sid, live[1], "omitted")     # not listed at all: an omission too
                else:
                    self.host.steer_fence(sid)
            if sid not in listed and not s.ended:
                # The hub ended it (closed while this node was away, or its record is gone): it must not go on.
                self.dropped.add(sid)
                self.status.pop(sid, None)
                self.reported.pop(sid, None)
                self._emit(self.host.close(sid, "the hub no longer lists this session"))   # (its own: dropped below)
        for sid in [x for x in self.queue if x not in listed]:
            del self.queue[sid]
        self.ending &= set(listed)
        self.subtree_open &= set(listed)
        self.pending = [(sid, e) for sid, e in self.pending if sid not in self.dropped]
        if set(self.handed) - set(listed):
            self.handed = {sid: n for sid, n in self.handed.items() if sid in listed}
            self._save_handed()
        self.host.keep_provider_ids(set(listed))
        self.host.keep_private_files(set(listed))     # ... and the settings and MCP files made for sessions that are gone
        if self.worktrees and self.clock() - self.swept >= SWEEP_SECONDS:
            # Worktrees of child sessions that are gone here (ended while this node was away, or kept because they
            # held something): each is removed once it holds nothing unique.
            self.swept = self.clock()
            for sid in [x for x in self.worktrees if x not in listed and x not in self.host.sessions]:
                self._drop_worktree(sid)
        self.version = data.get("version") if isinstance(data.get("version"), str) else None
        if "restart" in features and "restart" not in self.hub_features:
            self.reported.clear()       # a hub that takes `modes` now: every session says it again
        elif (("steer" in features and "steer" not in self.hub_features)
              or (("held" in features) != ("held" in self.hub_features))):
            # ... and one that takes `steer` in them now (S2-09), or whose `held` came or went (round 3, ruling O):
            # only a session whose reported modes change says them again.
            for sid, st in self.status.items():
                if self._modes_for(st[4], features) != self._modes_for(st[4], self.hub_features):
                    self.reported.pop(sid, None)
        self.hub_features, self.features_known = features, True
        self.features_gen += 1
        for sid in [x for x in self.report_ack if x not in self.host.sessions]:
            del self.report_ack[sid]        # acknowledgements are kept for open sessions only (S2-13)
        self._prune_said()

    def _prune_said(self) -> None:
        """(R9-01) A held attempt's reasons go with its slot; every diagnostic of a closed session goes."""
        for sid in list(self._said_held):
            held = self.host.steer_slot(sid) if sid in self.host.sessions else None
            if held is None or held.uuid != self._said_held[sid][0]:
                del self._said_held[sid]
        self._said_bad = {k: v for k, v in self._said_bad.items() if k in self.host.sessions}
        self._prune_422()

    # ------------------------------------------------------------ child sessions (hosted_tree.py)

    def _tree_open(self, sid: str, view: dict) -> dict:
        """What a hub with child sessions adds to opening a session: its access word and effort, and for a child
        with its own worktree that directory, made now if it is not there. Nothing for an entry without the keys:
        the session is then opened exactly as before."""
        if "access" not in view and "parent" not in view:
            return {}
        is_child = isinstance(view.get("parent"), str)
        # A child of an interactive session (hosted_spawn.py) has no parent here: its word is the hub's, from the
        # human's grant, and this machine's own settings (allow_full_access, the runner) still decide what it can be.
        interactive = not is_child and "parent" in view and view.get("interactive") is True
        if not is_child and not interactive and view.get("access") is not None:
            # The checks that hold a session to a parent (`_parent_problem`, the ceiling) only run for an entry that
            # names one. So a word on an entry that names none would be applied unchecked: `full` on any runner of
            # a node that allows full access. This hub never sends that; it is refused here all the same.
            raise ValueError(ROOT_ACCESS)
        kw: dict[str, Any] = {"access": view.get("access"), "effort": view.get("effort")}
        if _bind_of(view) is not None:
            kw["bind"] = _bind_of(view)     # the key that proves the session's process is this session's
        isolation = view.get("isolation") if is_child or interactive else None
        if isolation == "worktree":
            checkout = self.config.checkout_for(view.get("project"))
            if not checkout:
                raise FileNotFoundError(f"this machine has no run directory for project {view.get('project')!r}")
            path = session_worktrees.path_for(self.config, checkout, sid)
            known = sid in self.worktrees
            self.worktrees[sid] = {"path": path, "checkout": checkout}
            self._save_worktrees()      # on record before it exists: a crash in between leaves nothing unknown
            try:
                kw["cwd"] = session_worktrees.ensure(self.config, checkout, sid)
            except Exception:
                if not known:
                    del self.worktrees[sid]
                    self._save_worktrees()
                raise
        elif isolation not in (None, "shared"):
            raise ValueError("unknown isolation for a child session")
        return kw

    def _parent_problem(self, parent_id: str, s) -> str | None:
        """Why a child may not be opened or take a turn under its parent as the parent is on this node NOW, or None.
        `s`: the child once it is open; its access here is then held against the parent's. Both are computed from
        the command lines this machine's runners give today (session_host): valid before either process has said
        anything, and after any restart or change of a runner."""
        parent = self.host.sessions.get(parent_id)
        if parent is None or parent.ended or parent_id in self.ending or parent_id in self.dropped:
            return PARENT_GONE
        if s is None:
            return None
        if parent.access not in session_access.LEVELS or s.access not in session_access.LEVELS:
            return PARENT_UNKNOWN
        if not session_access.within(s.access, parent.access):
            return ABOVE_PARENT.format(have=s.access, limit=parent.access)
        return None

    def _awaits_parent(self, parent_id: str, s) -> bool:
        """The child's access is within what its parent was STARTED as, but above what the parent's LIVE process
        has confirmed (`ceiling`: `full` only while the process whose `init`, or whose answer to thread/start, said
        so is the parent's process and is up). It is held, not ended: no turn until then."""
        parent = self.host.sessions.get(parent_id)
        return (parent is not None and s.access in session_access.LEVELS and parent.ceiling in session_access.LEVELS
                and not session_access.within(s.access, parent.ceiling))

    def _end_child(self, sid: str, why: str) -> None:
        """End a child here and tell the hub why: it is not opened, or is closed if it was open."""
        log.warning("child session %s: %s", sid, why)
        if sid in self.host.sessions:
            self._emit(self.host.close(sid, why))
        else:
            self._ended_without_process(sid, why)

    def _ended_without_process(self, sid: str, why: str) -> None:
        """A session that ends here with no process (never opened here): its end."""
        self.ending.add(sid)
        self._add(sid, {"kind": "ended", "detail": why[:300]})

    def _tree_status(self, sid: str, view: dict, s) -> None:
        """Whether this session's reports carry its access and worktree: only for a hub that asked. The access
        is the one computed from the session's command line when it was opened (session_host): it is there from the
        first report on."""
        if "access" not in view and "parent" not in view:
            self.tree.pop(sid, None)
            return
        self.tree.setdefault(sid, {})
        self._tree_now(sid, s, root="parent" not in view)

    def _tree_now(self, sid: str, s, root: bool | None = None) -> None:
        """What this session's reports carry beyond its state, as it is NOW (called on every fetch and after every
        tick, for a session whose hub asked): its access; for a root also its `ceiling` (the most a child may have
        under it now: `full` only while its live process has confirmed it) and its `checkout` (where it runs, which
        a runner that names its own directory is held against when a child is asked for); a child's own worktree."""
        if sid not in self.tree:
            return
        root = sid not in self.parent_of if root is None else root
        tree = {"access": s.access, "worktree": self.worktrees.get(sid, {}).get("path"),
                "ceiling": s.ceiling if root else None, "checkout": s.cwd if root else None}
        if self.tree.get(sid) != tree:
            self.tree[sid] = tree
            self.reported.pop(sid, None)        # say how it stands again, with these

    def _drop_worktree(self, sid: str) -> None:
        """The session is over here: remove its own worktree if nothing unique is in it, and tell the hub. One that
        holds uncommitted work, or commits on no branch, stays where it is and is looked at again later."""
        w = self.worktrees.get(sid)
        if w is None:
            return
        try:
            outcome = session_worktrees.remove_if_safe(w["checkout"], w["path"])
        except Exception:
            log.exception("session %s: could not look at its worktree", sid)
            outcome = "kept"
        if outcome == "kept":
            log.info("session %s: its worktree %s is kept (uncommitted work, or commits on no branch)", sid, w["path"])
            return
        del self.worktrees[sid]
        self._save_worktrees()
        self._add(sid, {"kind": "worktree_removed"})

    def _waits_on_parent(self, sid: str) -> bool:
        """This session is a child that is open here and held: nothing is handed to it until its parent's live
        process has confirmed what the child needs (`_awaits_parent`)."""
        s, parent_id = self.host.sessions.get(sid), self.parent_of.get(sid)
        return s is not None and not s.ended and parent_id is not None and self._awaits_parent(parent_id, s)

    def _turn_allowed(self, s) -> bool:
        """The host asks this as it is about to write a turn. A message is handed to a child only under a parent
        that allows it (`_hand_over`), but it can wait in the child's inbox for a handshake, and the parent's
        process can go in that time: so the same is asked again here, of the parent as it is now. False leaves the
        message where it is; a child whose parent is gone is ended at the next fetch."""
        parent_id = self.parent_of.get(s.session_id)
        if parent_id is None:
            return True
        parent = self.host.sessions.get(parent_id)
        return parent is not None and not parent.ended and not self._awaits_parent(parent_id, s)

    def _under(self, sid: str) -> list[str]:
        """The sessions below `sid` on this node, as the hub listed them (children, theirs, and so on)."""
        out, level = [], [sid]
        while level:
            level = [kid for kid, pid in self.parent_of.items() if pid in level and kid not in out]
            out += level
        return out

    def _full_above(self, sid: str) -> str | None:
        """The `full` child at or above `sid` (deeper trees: only a root's child can be full) whose parent's live
        process vouches for it, or None."""
        cur, hops = sid, 0
        while cur in self.parent_of and hops < 10:
            s = self.host.sessions.get(cur)
            if s is not None and not s.ended and s.access == "full":
                return cur
            cur, hops = self.parent_of[cur], hops + 1
        return None

    def _lends_full(self, sid: str, waiting: bool = True) -> bool:
        """This session's live process is what lets a `full` child of it take turns. KEPT (True whatever `waiting`
        says) while that child or a session under it is at work here (a turn in flight, or a message handed to it);
        with `waiting`, SPARED while the hub's listing says the child's subtree is open (`subtree_open`: anything
        under it not ended, or a result or a message for it or under it not yet settled), for whatever comes of it is
        that child's next turn. Stopping that process would hold the child until the parent's next turn."""
        parent = self.host.sessions.get(sid)
        if parent is None or parent.ceiling != "full":
            return False
        for kid, pid in self.parent_of.items():
            k = self.host.sessions.get(kid)
            if pid != sid or k is None or k.ended or k.access != "full":
                continue
            for x in [kid] + self._under(kid):
                d = self.host.sessions.get(x)
                if d is not None and not d.ended and (d.busy or d.inbox):
                    return True
            if waiting and kid in self.subtree_open:
                return True
        return False

    def _admit(self, s, starting: bool = False) -> bool:
        """May this session's process start now? Not while `max_live` processes are up: then the longest-idle one is
        stopped (one at a time) and the message waits; with none idle it just waits.

        Which idle process may be stopped. Never the parent a full child is AT WORK under, and never the parent of
        the full child asking here (it would then have a slot and nobody to vouch for it). A parent whose full
        child is only WAITING (a message queued at the hub) is spared as long as something else can be stopped
        instead. If nothing else can, that pin does not hold: it could not be honoured anyway, the waiting child
        having no slot either, and with every slot held by a parent waiting for a child nothing would ever start.
        Its child is then held until the parent's next turn, like any full child whose parent has no process."""
        if s.child is not None:
            return True
        # A session waiting for its checkout (a group being stopped there: node_groups) holds no slot: the message in
        # its inbox waits for the registry to let its process start (session_host `_spawn`) and does not count here.
        # `starting`: asked by the host itself at the moment it would start the process (`SessionHost.admit`), for
        # every start, first or retried: then only the process groups that are there count (a stopping one too,
        # until it is gone), and the session that asks is not one of them, so no two starts share one slot.
        # And one that asks while its checkout is blocked is let through to its inbox at once, stopping nobody: it
        # cannot start yet, and asks for its slot (here, `starting`) only once the registry would let it start.
        if not starting and self.host.groups.blocked(s.cwd):
            return True
        live = [x for x in self.host.sessions.values()
                if x.child is not None or (not starting and x.inbox and not self.host.groups.blocked(x.cwd))]
        if len(live) < self.max_live:
            return True
        if not any(x.child is not None and x.stopping_since is not None for x in live):
            # (Not one whose provider runs a turn of its own in it: by the host's books it is idle, but it is working.)
            # Never the process that vouches for the asker: the parent of the full child it is, or is under (its
            # results are that child's turns, which need that parent's live process).
            top = self._full_above(s.session_id)
            own = self.parent_of.get(top) if top is not None else None
            idle = [x for x in live if x.child is not None and not x.busy and not x.inbox and not x.ended
                    and not self.host.holding(x) and x.session_id != own
                    and not self._lends_full(x.session_id, waiting=False)]
            spared = [x for x in idle if not self._lends_full(x.session_id)]
            if spared or idle:
                self.host.park(min(spared or idle, key=lambda x: self.last_busy.get(x.session_id, 0.0)).session_id)
        return False

    def _hand_over(self, now: float) -> None:
        for sid, queue in list(self.queue.items()):     # ending a child here drops its queue
            s = self.host.sessions.get(sid)
            if (not queue or s is None or s.ended or s.busy or s.inbox or s.stopping_since is not None
                    or not isinstance(queue[0].get("text"), str)):
                continue
            if sid in self.parent_of and self._parent_problem(self.parent_of[sid], s) is not None:
                # Not a turn under a parent that is gone here, or that has less access now than this child.
                self._end_child(sid, self._parent_problem(self.parent_of[sid], s))
                continue
            if sid in self.parent_of and self._awaits_parent(self.parent_of[sid], s):
                continue        # a full child whose parent's process has not confirmed full access (yet, or again)
            if not self._admit(s):
                continue
            m = queue.pop(0)
            self.handed[sid] = m["n"]
            self._save_handed()         # on disk first: after a crash this message is lost, never written twice
            try:
                # Its kind goes with it to the provider (`turn`): whose turn it is, which the text alone does not say.
                # A kind this node does not know (a newer hub) is refused, never passed off as the human's.
                kind = m.get("kind") if m.get("kind") is not None else "user"
                self.host.send(sid, m["text"], message_id=str(m["n"]), kind=kind)
            except ValueError as e:
                self._add(sid, {"kind": "turn_ended", "message": m["n"], "ok": False, "delivered": False,
                                "detail": f"the node refused the message: {e}"[:300]})
            self.last_busy[sid] = now

    def _park_idle(self, now: float) -> None:
        for sid, s in self.host.sessions.items():
            if (s.unbound and not s.ended and not s.busy and not s.inbox and not self._lends_full(sid)
                    and self.host.park(sid)):
                log.info("session %s: its process was stopped to start again with its binding as it is now", sid)
        for sid, s in self.host.sessions.items():
            if s.busy or s.inbox or self._lends_full(sid):
                self.last_busy[sid] = now
            elif s.child is not None and now - self.last_busy.setdefault(sid, now) >= self.idle_seconds:
                if self.host.park(sid):
                    log.info("session %s: idle for %g hours, its process was stopped", sid, self.idle_seconds / 3600)

    def _after_tick(self, now: float) -> None:
        for sid, s in list(self.host.sessions.items()):
            if s.busy or s.inbox:
                self.last_busy[sid] = now
            if s.ended and s.child is None:
                retained = self.host.take_outbox()     # (ruling M) delivered before the session is forgotten
                if retained:
                    self._dispatch(retained)
                self.host.forget(sid)
                for d in (self.status, self.reported, self.last_busy, self.queue, self.sent, self.tree,
                          self.parent_of):
                    d.pop(sid, None)
                with self.ack_lock:
                    self.report_ack.pop(sid, None)
                # The session is gone here, so "the hub no longer lists it: report nothing" has done its work. It
                # must be lifted BEFORE the worktree is looked at: that its worktree was removed is the one thing
                # the hub still takes for a session it ended itself, and it would be swallowed with the rest.
                self.dropped.discard(sid)
                self._drop_worktree(sid)        # a child's own worktree, now that its process is gone
            elif not s.ended and sid not in self.dropped:
                # (and what its adapter can do with a turn in flight NOW: Claude says it in each process's `init`)
                self.status[sid] = (s.state, getattr(s.child, "pid", None), bool(s.resume), self._own_id(s),
                                    self.host.modes(sid))
                self._tree_now(sid, s)      # a root's ceiling changes when its process confirms full access
                if (self.host.steer_live(sid) is not None
                        and now - self.report_ack.get(sid, (float("-inf"),))[0] >= STEER_STATUS_SECONDS):
                    self.reported.pop(sid, None)    # a steer is held: its status goes again (part E), renewing contact

    @staticmethod
    def _own_id(s) -> str | None:
        """The provider's id for the session when it issued one of its own (Codex's thread id). Claude's is the
        session id, which the hub has: nothing is added to its reports."""
        return s.provider_id if s.provider_id != s.session_id else None

    # ------------------------------------------------------------ write authority for steers (the module text)

    def note_hello(self, sent: float) -> None:
        """node.py: a hello the hub acknowledged, SENT at `sent` (this loop's clock): evidence for every attempt. Under
        the ack lock and each attempt's own, never the host's (R2-09)."""
        self._publish_ack(sent, None, None)

    def _ack_report(self, sid: str, sent: float, answer: Any) -> None:
        """A report the hub acknowledged, SENT at `sent`: evidence for exactly this session and the integer turn its
        answer names in flight (never a bool or another session's answer). Kept only for a session open here
        (S2-13)."""
        if sid not in self.host.sessions:
            with self.ack_lock:
                self.report_ack.pop(sid, None)
            return
        ok = isinstance(answer, dict) and answer.get("session_id") == sid
        n = answer.get("in_flight") if ok else None
        self._publish_ack(sent, sid, n if type(n) is int else None)

    def _publish_ack(self, sent: float, about: str | None, n: Any) -> None:
        """One acknowledgement: into the evidence and its log, then into every held attempt's record, each under its
        own lock, where it counts for that attempt only if it names it (SteerSlot.ack: a break, if it came after the
        contact ran out, is latched there for good, S2-03)."""
        with self.ack_lock:
            received = self.clock()
            if about is None:
                self.hello_ack = max(self.hello_ack, sent)
            else:
                self.report_ack[about] = (sent, n)
            self.ack_seq += 1
            self.ack_log.append((self.ack_seq, sent, received, about, n))
            if len(self.ack_log) > ACK_LOG_MAX:
                del self.ack_log[:-ACK_LOG_MAX]
            for slot in self.host.attempts():
                slot.ack(sent, self.clock, about, n)     # its receipt read under the record's lock (R4-02)

    @property
    def steer_seen(self) -> dict[str, float]:
        """(Read only, for the record's readers:) each held attempt's contact, as the send time it rests on."""
        return {slot.session_id: slot.cover_until - STEER_CONTACT_SECONDS for slot in self.host.attempts()
                if slot.cover_until is not None}

    def _steer_contact(self, sid: str, n: int, hello: float, reports: dict) -> float:
        """The send time of the latest hub-acknowledged evidence that this node holds N: a hello, or a report on the
        session whose answer named N (an integer, exactly) as its turn in flight."""
        sent, in_flight = reports.get(sid, (float("-inf"), None))
        return max(hello, sent if type(in_flight) is int and type(n) is int and in_flight == n else float("-inf"))

    def _offer_contact(self, sid: str, n: int, fetch: tuple | None, now: float) -> tuple[bool, float]:
        """(The ack lock held.) For an offer of S for N from `fetch`: whether it is FRESH (the fetch was sent while
        contact held, contact held without a break until its answer came, by receipt times, and that answer came
        within STEER_OFFER_AGE of the send: R2-07, R3-03), and the attempt's contact clock NOW (`cover_until`: the
        walk continued through what was acknowledged since; minus infinity once broken)."""
        lost = float("-inf")
        if fetch is None:
            return False, lost
        sent_at, received_at, (hello, reports), seq_sent, seq_received = fetch
        cover = self._steer_contact(sid, n, hello, reports) + STEER_CONTACT_SECONDS
        if sent_at >= cover:
            return False, lost
        if self.ack_log and self.ack_log[0][0] > seq_sent + 1 and self.ack_seq > seq_sent:
            return False, lost      # the log no longer reaches back to the fetch's send: never a gap it cannot see
        fresh = None
        for seq, sent, received, about, named in self.ack_log:
            if seq <= seq_sent:
                continue
            if fresh is None and seq > seq_received:
                fresh = received_at < cover and received_at - sent_at < STEER_OFFER_AGE
            if about is not None and not (about == sid and type(named) is int and named == n):
                continue        # a report on another session, or naming another turn: no evidence for N
            if received >= cover:
                return False, lost      # the contact ran out before this one came: a break
            cover = max(cover, sent + STEER_CONTACT_SECONDS)
        if fresh is None:
            fresh = received_at < cover and received_at - sent_at < STEER_OFFER_AGE
        return fresh and now < cover, (cover if now < cover else lost)

    def _steer_authority(self, s) -> bool:
        """The host asks this at every write of a steer's bytes, under the attempt's lock: the node is not past its
        self-stop, nor stopping. The contact itself is the attempt's own record (SteerSlot.contact_ok); the record's
        own `stopping` is set under its lock when the node begins to stop (`_Stopping`)."""
        return not self._silent() and not self.stopping.is_set()

    def _steer_capable(self, s) -> bool:
        """(Review round 2, ruling J) Asked at every write of a steer's bytes (the write gate only, never as the
        attempt's authority: a hold is not cut because a fetch failed and the features are unknown for a while): a
        Claude steer (its adapter holds N's end) is written only while the hub's current listing names `held`."""
        return not self.host.holds(s.session_id) or ("held" in self.hub_features and self.features_known)

    def _steer_listed(self, sid: str, view: dict, fetch_sent: float | None, aware: bool = True) -> None:
        """The listing's word on this session's steer: an offer (passed on, with whether authority held for it) or
        an omission (the held steer is revoked for good). `aware`: the listing names `steer` among its features;
        one that does not (an older hub, or a capability not known) only FENCES the held steer: nothing of it is
        written, no offer of its is taken, nothing is revoked (S2-05). An offer that names the held attempt's uuid
        for another N is rejected before anything is touched (R3-02)."""
        held = self.host.steer_slot(sid)
        if not aware:
            if held is not None:
                self.host.steer_fence(sid)
            return
        item = view.get("steer")
        ok = (isinstance(item, dict) and type(item.get("into")) is int and isinstance(item.get("uuid"), str)
              and 0 < len(item["uuid"]) <= 64 and isinstance(item.get("text"), str) and item["text"].strip() != "")
        if not ok:
            if held is not None:
                self.host.steer_revoke(sid, held.uuid, "omitted", held.into)
            return
        n = item["into"]
        if held is not None and held.uuid == item["uuid"] and held.into != str(n):
            self._say_held(sid, held, "another turn", logging.WARNING,
                           "session %s: its steer offered for another turn: not taken", sid)
            return
        if held is not None and held.uuid != item["uuid"]:
            self.host.steer_revoke(sid, held.uuid, "omitted", held.into)
        fetch = self.last_fetch if self.last_fetch is not None and self.last_fetch[0] == fetch_sent else None
        big = len(json.dumps(item["text"]).encode()) > STEER_MAX_BYTES
        why = err = None
        with self.ack_lock:     # no acknowledgement slips between the walk and the record's creation
            fresh, cover = self._offer_contact(sid, n, fetch, self.clock())
            try:
                why = self.host.steer_offer(sid, str(n), item["uuid"], item["text"], authorized=fresh and not big,
                                            refused="too_big" if big else None, cover_until=cover)
            except (LookupError, ValueError) as e:
                err = e
        # (R8-04) Said outside the evidence lock (slow stderr never holds up an acknowledgement's publication), and
        # once per held attempt (or offered steer) and reason, however many listings repeat it.
        if why is not None:
            now_held = self.host.steer_slot(sid)        # the attempt the host holds (the one it compared with)
            if now_held is not None:
                self._say_held(sid, now_held, why, logging.INFO, "session %s: a steer offer that is %s: not taken",
                               sid, why)
        if err is not None:
            # (R9-01) Keyed by the session, not by the uuid offered (a hub's bug could offer a fresh one each pass):
            # at most one line per BAD_OFFER_LOG_SECONDS for the session.
            now = self.clock()
            if now - self._said_bad.get(sid, float("-inf")) >= BAD_OFFER_LOG_SECONDS:
                self._said_bad[sid] = now
                log.warning("session %s: a steer offer could not be taken: %s", sid, err)

    def _say_held(self, sid: str, held: Any, why: str, level: int, msg: str, *args) -> None:
        """(R8-04, R9-01) A reason about the held attempt, said once per attempt: kept with that attempt only."""
        uuid, said = self._said_held.get(sid, (None, set()))
        if uuid != held.uuid:
            uuid, said = held.uuid, set()
            self._said_held[sid] = (uuid, said)
        if why in said:
            return
        said.add(why)
        log.log(level, msg, *args)

    def _steer_watch(self, now: float) -> None:
        """Loss is one-way: a held attempt whose contact ran out is latched lost, in its own record. Called by the loop
        and by the deadline watchdog."""
        for slot in self.host.attempts():
            slot.contact_ok(self.clock())

    # ------------------------------------------------------------ the deadlines, off the hub-I/O path

    @property
    def last_contact(self) -> float:
        """When the last successful hub call of the session loop was SENT (`_renew_contact`)."""
        return self._last_contact

    def _renew_contact(self, sent: float) -> None:
        """The one place the contact is renewed (`_call`, after a successful call). First the deadline is looked at
        with the PREVIOUS contact, at the time the call RETURNED (now: the node was unheard for all of that): had it
        passed, the latch is set before the contact moves on, whether or not the watchdog or the loop had seen it
        yet. So a report that gets through just after the deadline cannot open the gate without a listing."""
        self._silent(self.clock())
        self._last_contact = max(self._last_contact, sent)

    def _self_stop_due(self) -> float:
        """When the node's self-stop is due: `self_stop_seconds` after its last successful hub call, counted from when
        that call was SENT (`_call`), so never later than the hub's view of the contact."""
        return self.last_contact + self.self_stop_seconds

    def _silent(self, now: float | None = None) -> bool:
        """Past the self-stop deadline (the one computation the watchdog, the loop, the start gate and the contact's
        renewal share). Latched: from then on nothing starts until a listing from a fetch sent after the latest such
        moment has been applied (`_unlatch`)."""
        now = self.clock() if now is None else now
        if now >= self._self_stop_due():
            with self.latch_lock:
                self.latched_at = now if self.latched_at is None else max(self.latched_at, now)
            return True
        return False

    def _unlatch(self, fetch_sent: float, now: float) -> None:
        """After a listing from a successful fetch was applied: the latch goes if that fetch was sent at or after the
        moment it was (last) set, and the node is not past its deadline now. A fetch sent before (it straddled the
        deadline) clears nothing: the next one will."""
        if self._silent(now):
            return
        with self.latch_lock:
            if self.latched_at is not None and fetch_sent >= self.latched_at:
                self.latched_at = None

    def _may_start(self) -> bool:
        """May the host start a session's process, or write a new turn to one, now (SessionHost.may_start)? Not once
        the node is past its self-stop deadline, until a listing fetched since contact came back has been applied
        (`pass_once` reconciles it before it ticks the host): a message that waits in an inbox, for its checkout or
        for a slot, is then neither started nor written while the hub may have closed or cancelled it and released
        its hold, and it waits, failing nothing. A start just BEFORE the deadline is fine: the watchdog stops its turn
        at the deadline, and `shutdown_bound` covers it."""
        return not self._silent() and self.latched_at is None

    def _may_start_query(self) -> bool:
        """`_may_start`'s answer without its side effect: it never sets the latch (`_silent` does, at the deadline).
        For the session host's turn-correlation accounting only (SessionHost.may_start_query), which must change no
        node state main would not; the start gate itself stays `_may_start`, asked where and when main asks it."""
        return self.latched_at is None and self.clock() < self._self_stop_due()

    def _self_stop_detail(self) -> str:
        return f"the node had no hub contact for {self.self_stop_seconds:g} seconds and stopped the turn"

    def enforce_deadlines(self) -> None:
        """The node's two deadlines without the hub, whatever the loop is doing: its self-stop (SIGTERM to every
        turn's process group once `self_stop_seconds` have passed since its last successful hub call) and the
        SIGKILL after each grace. Under the loop's lock, which the loop holds only for local work, never across a
        hub call. What the self-stop reports waits in `watched` for the loop's next `_emit`. A deadline met later than
        `shutdown_bound` allows it (the watch interval plus LOCK_SLACK_SECONDS: the loop held its lock for longer,
        limit e) is said in the log, once, with how late it was."""
        with self.lock:
            if self.stopping.is_set():
                return          # shutdown stops everything itself
            now = self.clock()
            allowed = self.watch_seconds + LOCK_SLACK_SECONDS
            due = self._self_stop_due()
            if self._silent(now):
                stopped = self.host.stop_turns(self._self_stop_detail())
                if stopped and now - due > allowed:
                    log.warning("hosted sessions: the self-stop came %.1f s after its deadline: the session loop held "
                                "its lock that long (limit e)", now - due)
                self.watched += stopped     # (main's, a steer's end too: the shutdown simplification)
            for sid, at in self.host.kill_due():
                if now - at > allowed:
                    log.warning("hosted sessions: session %s: its SIGKILL came %.1f s after its deadline: the session "
                                "loop held its lock that long (limit e)", sid, now - at)
            # Then, after every deadline of main's has been met (a cut's drain must never delay them, R4-06): loss
            # is one-way, and a half-written steer frame past its limit is cut.
            self._steer_watch(now)
            self.watched_steer += self.host.steer_deadlines()

    def _watch(self) -> None:
        while not self.stopping.wait(self.watch_seconds):
            try:
                self.enforce_deadlines()
            except Exception:
                log.exception("hosted sessions: the deadline watchdog failed a pass")

    # ------------------------------------------------------------ the thread

    def run_forever(self) -> None:
        while not self.stopping.is_set():
            try:
                ok = self.pass_once()
            except Exception:
                log.exception("hosted sessions: a pass failed")
                ok = False
            if self.active():
                self.stopping.wait(TICK_SECONDS)
            elif not ok:
                self.stopping.wait(RETRY_SECONDS)   # otherwise the next pass waits at the hub (long-poll)

    def start(self) -> None:
        self.start_checkpoint_sender()      # (round 3, ruling P) before the driving thread: no inline window
        self.thread = threading.Thread(target=self.run_forever, name="hosted-sessions", daemon=True)
        self.thread.start()
        self.watch_thread = threading.Thread(target=self._watch, name="hosted-sessions-deadlines", daemon=True)
        self.watch_thread.start()

    def start_checkpoint_sender(self) -> None:
        """The checkpoint sender's own thread (ruling K)."""
        self.checkpoint_thread = threading.Thread(target=self._send_checkpoints, name="hosted-sessions-checkpoints",
                                                  daemon=True)
        self.checkpoint_thread.start()

    def signal(self) -> None:
        """The first step of a shutdown: SIGTERM to every session process, now. No waiting and no reporting, so the
        node can go on to stop its ordinary runs at once (node.run); `shutdown()` does the rest."""
        self.stopping.set()     # (R4-01: every held steer record is marked first: no steer byte after this)
        with self.lock:
            for s in self.host.sessions.values():
                if s.child is not None:
                    try:
                        s.child.terminate()
                    except Exception:
                        log.exception("could not signal session %s", s.session_id)

    def _going(self, s) -> bool:
        return s.child is not None and (s.child.poll() is None or self.host.group_alive(s.child.pid))

    def alive(self) -> bool:
        """After `signal()`: is any session process, or anything in its process group, still there?"""
        with self.lock:
            return any(self._going(s) for s in self.host.sessions.values())

    def kill(self) -> None:
        """After `signal()` and the grace period: SIGKILL for every session process group that is still there. No
        reporting; `shutdown()` then finds them gone."""
        with self.lock:
            for s in self.host.sessions.values():
                if self._going(s):
                    s.child.kill()

    def shutdown(self) -> None:
        """Stop the loop and every session process; turns in flight are reported failed. The loop may be waiting at
        the hub: it is given JOIN_SECONDS, then the host is taken from it (it checks `stopping` before touching the
        host again). From `stopping` on the loop posts nothing (`_flush`), so what it had not reported yet and what
        the shutdown adds go to the hub from here, in order, as one queue."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(JOIN_SECONDS)
        with self.lock:
            # stop_all reports the turn in flight and says nothing of a message still in a session's inbox. Such a
            # message never reached a process, so it is given back: the hub still lists it as queued, and with its
            # number no longer recorded as handed the next node process delivers it (once) instead of failing it.
            back = {sid: min(int(m.id) for m in s.inbox if m.id.isdigit()) - 1
                    for sid, s in self.host.sessions.items() if any(m.id.isdigit() for m in s.inbox)}
            for sid, n in back.items():
                self.host.sessions[sid].inbox.clear()
                self.handed[sid] = min(self.handed.get(sid, n), n)
            if back:
                self._save_handed()
            try:
                events = self.host.stop_all()
            except Exception:
                # (Ruling M) what stop_all decided before it raised is in the host's outbox: delivered below
                log.exception("hosted sessions: the shutdown of the session processes failed part way")
                events = self.host.take_outbox()
        # A flush the loop began before `stopping` may still be on its way: wait for it (a hub call is bounded by the
        # client's timeout) rather than post beside it. If it never returns, nothing is reported from here and the
        # next node process reports the lost turns.
        if not self.report_lock.acquire(timeout=REPORT_WAIT_SECONDS):
            log.warning("a report to the hub is still under way; the session shutdown is not reported")
            return
        self.report_lock.release()
        with self.lock:             # the loop is past its last pass: `pending` and `status` are shutdown's now
            self._emit(events)
            self.status = {sid: (s.state, None, bool(s.resume), self._own_id(s), ()) for sid, s in
                           self.host.sessions.items() if not s.ended and sid not in self.dropped}
            for sid, s in self.host.sessions.items():
                self._tree_now(sid, s)      # no process is left to have confirmed anything: the ceiling says so
        try:
            if not self._flush(final=True):
                log.warning("could not report the session shutdown")
        except Exception as e:
            log.warning("could not report the session shutdown: %s", e)
