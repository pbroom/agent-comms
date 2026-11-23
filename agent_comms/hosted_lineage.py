"""Session trees as the human's dashboard shows them (DESIGN_NOTES "Session lineage view"). Read-only: nothing here
writes, so a view that refreshes every few seconds changes nothing on the board.

One entry per TREE: a hosted root (a session started without a parent), a project's interactive root
(hosted_spawn.py: made when an agent conversation the human opened first started a child session; every such
conversation of that agent in that project shares it), or a session whose parent is no longer on record. Under it,
every session started below it, nested at every depth. Each session is `hosted._human_view` (what `board session
list` and `show` print) of its record AS STORED, with these additions:

- `started_by`: the human who started it, or the agent (and whether from a hosted or an interactive session) that
  started it with the spawn tools (`spawned_by` on the child's record); for an interactive root, `interactive`.
- `result`: a child's latest result as its parent's outbox holds it (hosted_results.py): outcome, where it stands,
  and why it waits (`hosted_results.waiting_because`, the server's words), never its text.
- `output`: the session's last reply, the agent's own words, cut to OUTPUT_MAX characters (`truncated`, `length`,
  both in code points). It is untrusted data: returned as a JSON string and shown by the dashboard as plain text.
- `unsettled`: what the next settling read (`board session list` or `show`: `hosted._settle`) would do to the
  record, which this view never does: end a child its node can no longer run, or fail the turn in flight (or end
  the session being closed) on a node gone silent. The view shows the record as stored and says this beside it.
- `version`: a hash of exactly what `detail` returns for the session (`_body`), so the dashboard fetches the details
  again when, and only when, they changed.

`detail` is the dashboard's details panel: the same view, the session's recent messages, the results its children
sent it, with every text capped as above. The CLI's `show` is unchanged. The dispatcher's runs, which say why a
result waits, are read ONCE per request (`_Busy`). Human only, as every hosted-session route is: agents and nodes
get nothing here. The dashboard snapshot (`GET /api/state`, the human's) carries `hosted_live` (`live_count`: the
sessions recorded as not ended) for the tab's count, so the trees themselves are fetched only while the tab is open.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from . import hosted, hosted_results, hosted_spawn, hosted_tree, nodes
from .core import Board, NotFound, Principal

OUTPUT_MAX = 4000       # code points of any agent text the view returns (`show` keeps up to 16 KB)
LOST = ("its node went silent with a turn in flight or a close pending: the next settling read (`board session "
        "list` or `show`) marks that turn failed as lost, or ends the session being closed")
ORPHAN = ("its node can no longer run it ({why}): the next settling read (`board session list` or `show`) ends it, "
          "and a turn in flight there is failed as lost")
ORPHAN_WHY = {hosted_tree.ORPHANED: "its latest hello says it hosts no child sessions",
              hosted_tree.ORPHANED_HOLD: "it does not keep a full-access session's parent up for the sessions under it"}


def _cap(text: Any) -> dict | None:
    if not isinstance(text, str) or not text:
        return None
    return {"text": text[:OUTPUT_MAX], "truncated": len(text) > OUTPUT_MAX, "length": len(text)}


def _started_by(r: dict) -> dict:
    by = r.get("spawned_by")
    if isinstance(by, dict) and isinstance(by.get("agent"), str):
        return {"kind": "agent", "name": by["agent"], "via": by.get("kind"), "session": by.get("session")}
    return {"kind": "human", "name": r.get("created_by") if isinstance(r.get("created_by"), str) else None}


class _Busy:
    """Whether a dispatched run uses a parent's checkout (hosted_results.waiting_because), from one read of the
    dispatcher's runs and claims for the whole request, answered once per checkout."""

    def __init__(self, board: Board):
        self.board, self.seen = board, {}
        try:      # what dispatch.occupied_run_dirs reads, and _checkout_busy: the same records, read here once
            self.runs = hosted.checkout_runs(board)
            self.dirs = {os.path.realpath(d["cwd"]) for d in self.runs[1] if isinstance(d.get("cwd"), str) and d["cwd"]}
        except Exception:
            self.runs = self.dirs = None    # each question then reads for itself, with its own safe side

    def __call__(self, parent: dict) -> bool:
        from . import hosted_wake
        key = (parent.get("project"), parent.get("node"), parent.get("isolation"))
        if key not in self.seen:
            self.seen[key] = (parent.get("isolation") != "worktree"
                              and hosted_wake._occupied(self.board, parent, dirs=self.dirs)) or \
                hosted._checkout_busy(self.board, parent, runs=self.runs)
        return self.seen[key]


def _result(board: Board, x: dict | None, busy: _Busy, text: bool = False) -> dict | None:
    if x is None:
        return None
    out = {k: x.get(k) for k in ("n", "child", "name", "agent", "message", "started_by", "outcome", "ok", "detail",
                                 "state", "carrier", "attempts", "why", "created_at", "updated_at")}
    out["waiting_because"] = hosted_results.waiting_because(board, board.conn, x, busy=busy)
    if text:
        out["output"] = _cap(x.get("text"))
    return out


def _unsettled(conn, r: dict, now: float) -> str | None:
    """What the next settling read (hosted._settle) would do to this record (None: nothing). Never done here."""
    why = hosted_tree.orphan_reason(conn, r)
    if why:
        return ORPHAN.format(why=ORPHAN_WHY.get(why, "its latest hello says so"))
    return LOST if hosted._lost(conn, r, now) else None


def _view(board: Board, r: dict, now: float) -> dict:
    v = hosted._human_view(board, r)
    reply, reply_for = v.pop("reply", None), v.pop("reply_for", None)
    cap = _cap(reply)
    out = v | {"kind": "hosted", "started_by": _started_by(r),
               "output": cap | {"for_message": reply_for} if cap else None,
               "closing": bool(r.get("stop")) and r.get("state") != "ended",
               "unsettled": _unsettled(board.conn, r, now)}
    out.setdefault("parent", r.get("parent") if hosted_tree.is_child(r) else None)
    out.setdefault("root", r.get("root") or r["session_id"])
    out.setdefault("depth", int(r.get("depth") or 0))
    return out


def _iroot(r: dict) -> dict:
    """An interactive root: not a hosted session (nothing on a node runs it), the anchor of its children."""
    ended = r.get("state") == "ended"
    return {"session_id": r["session_id"], "kind": "interactive", "agent": r.get("agent"), "project": r.get("project"),
            "state": "ended" if ended else "open", "error": r.get("error") if ended else None,
            "created_at": r.get("created_at"), "updated_at": r.get("updated_at"), "ended_at": r.get("ended_at"),
            "parent": None, "root": r["session_id"], "depth": 0,
            "started_by": {"kind": "interactive", "name": r.get("agent")}}


class _Reads:
    """What one request reads more than once, read once: each outbox (a parent's results, indexed by child), each
    result's view, and the dispatcher's runs (`_Busy`)."""

    def __init__(self, board: Board):
        self.board, self.busy, self.boxes, self.latest, self.views = board, _Busy(board), {}, {}, {}

    def box(self, pid: str) -> list[dict]:
        if pid not in self.boxes:
            self.boxes[pid] = hosted_results._results(self.board.conn, pid)
            self.latest[pid] = {x["child"]: x for x in self.boxes[pid] if isinstance(x.get("child"), str)}
        return self.boxes[pid]

    def latest_of(self, pid: str, child: str) -> dict | None:
        self.box(pid)
        return self.latest[pid].get(child)          # oldest first: the last one stays

    def view(self, x: dict | None, text: bool = False) -> dict | None:
        if x is None:
            return None
        key = (x.get("parent"), x.get("n"), text)
        if key not in self.views:
            self.views[key] = _result(self.board, x, self.busy, text=text)
        return self.views[key]


def _messages(conn, sid: str) -> list[dict]:
    """The session's last SHOW_MESSAGES message records, oldest first: only those are read and decoded (the key
    ends in the message's number, hosted._msg_key)."""
    prefix = f"{hosted.MSG_PREFIX}{sid}."
    lo, hi = hosted._range(prefix)
    rows = conn.execute("SELECT value FROM board_state WHERE key >= ? AND key < ? "
                        "ORDER BY CAST(substr(key, ?) AS INTEGER) DESC LIMIT ?",
                        (lo, hi, len(prefix) + 1, hosted.SHOW_MESSAGES)).fetchall()
    out = []
    for (value,) in rows:
        try:
            m = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(m, dict) and type(m.get("n")) is int:
            out.append(m)
    return sorted(out, key=lambda m: m["n"])


def _body(board: Board, sid: str, r: dict | None, iroot: dict | None, reads: _Reads, now: float) -> dict:
    """What `detail` returns for one session or interactive root, without `version`: its view, its recent messages
    (no text), the results its children sent it (the most recent, and every undelivered one with its kept text,
    capped) and, for a child, its own latest result."""
    results = reads.box(sid)
    if r is not None:
        out = _view(board, r, now)
        out["messages"] = [hosted._msg_view(m) for m in _messages(board.conn, sid)]
        out["result"] = reads.view(reads.latest_of(r["parent"], sid)) if hosted_tree.is_child(r) else None
    else:
        out = _iroot(iroot) | {"messages": [], "result": None}
    recent = {x["n"] for x in results[-hosted_results.SHOW_RESULTS:]}
    out["results"] = [reads.view(x, text=x.get("state") == "undelivered") for x in results
                      if x["n"] in recent or x.get("state") == "undelivered"]
    return out


def _version(body: dict) -> str:
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _walk(n: dict) -> list[dict]:
    return [n] + [x for k in n["children"] for x in _walk(k)]


def trees(board: Board, p: Principal, include_ended: bool = False) -> dict:
    """Every session tree; with `include_ended` false, only trees in which a session has not ended (an open
    interactive root with no live child counts as ended: it holds nothing). A shown tree carries all its members,
    the ended ones too, so the lineage stays whole. The shape is found first from the stored records; only the
    sessions of the trees shown are then viewed, and their results read."""
    board._require_human(p, "read hosted session trees")
    conn, now = board.conn, board.now()
    rows = {r["session_id"]: r for r in hosted._sessions(conn)}
    iroots = {rid: r for rid, r in hosted_spawn._records(conn, hosted_spawn.IROOT_PREFIX)
              if isinstance(r.get("session_id"), str)}
    # The shape: {id, record, children}, each session under its parent.
    shape = {sid: {"id": sid, "r": r, "children": []} for sid, r in rows.items()}
    shape |= {rid: {"id": rid, "iroot": r, "children": []} for rid, r in iroots.items()}
    anchors = [shape[rid] for rid in iroots]
    for sid, r in rows.items():
        pid = r.get("parent") if hosted_tree.is_child(r) else None
        if isinstance(pid, str) and pid in shape and pid != sid:
            shape[pid]["children"].append(shape[sid])
        else:
            shape[sid]["detached"] = isinstance(pid, str)   # its parent is no longer on record (an old record pruned)
            anchors.append(shape[sid])
    shown = []
    for a in anchors:
        members = [n for n in _walk(a) if "r" in n]
        live = sum(1 for n in members if n["r"].get("state") != "ended")
        if "iroot" in a and not a["children"]:
            continue        # an interactive root that never started a child (or whose children were pruned)
        if include_ended or live:
            shown.append((a, len(members), live))
    reads = _Reads(board)

    def build(n: dict) -> dict:
        body = _body(board, n["id"], n.get("r"), n.get("iroot"), reads, now)
        out = {k: v for k, v in body.items() if k not in ("messages", "results")} | {"version": _version(body)}
        if n.get("detached"):
            out["detached"] = True
        kids = sorted(n["children"], key=lambda k: (float((k.get("r") or {}).get("created_at") or 0),
                                                    (k.get("r") or {}).get("child_no") or 0))
        out["children"] = [build(k) for k in kids]
        return out

    out = []
    for a, total, live in shown:
        top = build(a)
        audit = hosted_spawn._tree(conn, top.get("root") or a["id"]).get("thread")  # a detached one: its root's
        out.append({"root": a["id"], "kind": top["kind"], "agent": top.get("agent"), "project": top.get("project"),
                    "node": top.get("node"), "audit_thread": audit if type(audit) is int else None,
                    "sessions": total, "live": live, "created_at": top.get("created_at"), "session": top})
    out.sort(key=lambda t: (t["live"] == 0, -float(t["created_at"] or 0)))
    return {"trees": out, "live_sessions": sum(1 for r in rows.values() if r.get("state") != "ended"),
            "now": now, "output_max": OUTPUT_MAX}


def detail(board: Board, p: Principal, session_id: Any) -> dict:
    """The dashboard's details panel for one session (or interactive root), read-only and capped (`_body`), and its
    `version`, the same as `trees` gives it."""
    board._require_human(p, "read a hosted session")
    conn, now = board.conn, board.now()
    sid = session_id if isinstance(session_id, str) and len(session_id) <= 64 else ""
    r = nodes._get(conn, hosted.SESSION_PREFIX + sid) if sid else None
    if isinstance(r, dict):
        body = _body(board, sid, r, None, _Reads(board), now)
    elif sid and hosted_spawn.is_iroot(conn, sid):
        body = _body(board, sid, None, hosted_spawn.iroot(conn, sid), _Reads(board), now)
    else:
        raise NotFound("no such hosted session")
    return body | {"version": _version(body)}


def live_count(board: Board) -> int:
    """Hosted sessions recorded as not ended, counted in SQL with the shape `hosted._sessions` reads (a JSON object
    whose session_id is a string): no record is decoded here (the dashboard snapshot's tab count)."""
    lo, hi = hosted._range(hosted.SESSION_PREFIX)
    row = board.conn.execute("SELECT COUNT(*) FROM board_state WHERE key >= ? AND key < ? AND json_valid(value) "
                             "AND json_type(value) = 'object' AND json_type(value, '$.session_id') = 'text' "
                             "AND json_extract(value, '$.state') IS NOT 'ended'", (lo, hi)).fetchone()
    return int(row[0])
