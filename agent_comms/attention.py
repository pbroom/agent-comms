"""Selective source-attention closeout; never a human approval or an issue resolution."""
from __future__ import annotations

import json

from . import db
from .core import Board, Conflict, Forbidden, Invalid, Principal, iso


def close_attention(board: Board, p: Principal, session_id: int, post_id: int,
                    reason: str, evidence_post_ids: list[int]) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise Invalid("a resolution reason is required")
    reason = reason.strip()
    if len(reason.encode()) > board.s.body_max_bytes:
        raise Invalid("resolution reason exceeds size limit")
    if (not isinstance(evidence_post_ids, list) or not 1 <= len(evidence_post_ids) <= 20
            or any(type(i) is not int or i <= 0 for i in evidence_post_ids)
            or len(set(evidence_post_ids)) != len(evidence_post_ids)):
        raise Invalid("provide 1 to 20 distinct evidence post IDs")
    with db.write_tx(board.conn) as c:
        board._check_agent_write(p)
        session = board._session(p, session_id)
        post = board.get_post(p, post_id)
        thread = board._thread_row(post["thread_id"])
        if not p.is_human:
            if post["agent"] != p.name:
                raise Forbidden("agents may close only their own authored attention posts")
            if post["type"] == "decision":
                raise Forbidden("only the human can close decision attention")
            if session["project"] != thread["project"]:
                raise Forbidden("resolution session must belong to the source project")
            if thread["status"] != "open":
                raise Conflict("thread is closed")
        if not c.execute(f"SELECT 1 FROM posts p WHERE p.id=? AND {board.NEEDS_YOU_SOURCE}",
                         (post_id,)).fetchone():
            raise Conflict("post has no outstanding source attention")
        # Attention an open shared issue covers is governed by its decisions/resolution, not this endpoint.
        if c.execute(f"SELECT 1 FROM posts p WHERE p.id=? AND {board.ISSUE_GOVERNS}", (post_id,)).fetchone():
            raise Conflict("source belongs to a shared issue; use its decision/resolution flow")
        for evidence_id in evidence_post_ids:
            evidence = board.get_post(p, evidence_id)
            if evidence_id == post_id or evidence["thread_id"] != post["thread_id"]:
                raise Invalid("evidence must be another post in the source thread")
            # Audit records inherit source visibility. Reject sealed evidence even for the human
            # rather than exposing a sealed post's existence through a public source record.
            if evidence["sealed"]:
                raise Forbidden("unseal evidence before referencing it in a resolution")
        now = board.now()
        c.execute("""INSERT INTO attention_resolutions
                     (post_id,resolved_by,session_id,reason,evidence_post_ids,resolved_at)
                     VALUES (?,?,?,?,?,?)""",
                  (post_id, p.name, session_id, reason, json.dumps(evidence_post_ids), now))
        seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM posts").fetchone()[0]
        c.execute("UPDATE posts SET seq=?, revised_at=? WHERE id=?", (seq, now, post_id))
    board._notify("attention.resolved", {"post_id": post_id, "thread_id": post["thread_id"]})
    return board.get_post(p, post_id)


def resolution_out(board: Board, post_id: int) -> dict | None:
    row = board.conn.execute("SELECT * FROM attention_resolutions WHERE post_id=?", (post_id,)).fetchone()
    if row is None:
        return None
    from .autorecover import closed_automatically
    out = {"resolved_by": row["resolved_by"], "session_id": row["session_id"],
           "reason": row["reason"], "evidence_post_ids": json.loads(row["evidence_post_ids"]),
           "resolved_at": iso(row["resolved_at"])}
    if closed_automatically(board, post_id, row["resolved_at"]):
        out["automatic"] = True   # the dispatcher closed it because the stall cleared (autorecover.close_resolved)
    return out
