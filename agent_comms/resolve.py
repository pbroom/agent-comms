"""Resolve a "Needs you" item in one click (`POST /api/posts/{id}/resolve`, human only).

The dashboard's Needs you card offers, per item: Approve, Approve & launch <agent>, Reject (decisions only),
Not now, and Reply; for a post with a structured `decision_question`, Choose (one of its two options); for one
without, Ask for options. Each posts as the human in the item's thread, addressed to the item's author; a human
post after the item takes it out of Board.NEEDS_YOU. Finalizing a decision keeps its own route.

Guardrails (DESIGN_NOTES "Needs you actions"):
- The human's click is the approval. Agents cannot call this route; core and the route both check.
- Approve, Reject, Not now and Ask for options post fixed server-built text whose only variable parts are the post
  id (and, for the rule purpose, the thread id). Nothing an agent wrote is read or copied. Reply posts the human's
  own text.
- Choose posts `Chose option <id> ("<label>", recommended|alternative) for #N.` plus the human's optional note. The
  option id and label are the only agent-written text it copies: looked up by the id the human picked, folded onto
  one line and quoted, so they cannot pose as a separate line of the human's post. Nothing else (question, context,
  descriptions, body) is copied.
- Approve & launch approves a one-shot dispatcher rule (one launch, RULE_HOURS) for the author on this thread,
  before posting, unless an active rule for that agent on this thread already has launches left. The dispatcher's
  launch prompt stays its fixed template, with this fixed purpose.
- Only an item still in Needs you can be resolved (409 otherwise), and one resolve per post per
  RESOLVE_COOLDOWN_SECONDS, checked together in one write transaction, so a double click cannot post twice.
"""

from __future__ import annotations

from typing import Any

from . import dispatch, human_actions
from .core import Board, Conflict, Invalid, Principal

ACTIONS = ("approve", "approve_launch", "reject", "not_now", "reply", "choose", "ask_options")
NOTE_MAX_BYTES = 1024
RESOLVE_COOLDOWN_SECONDS = 10
STATE_PREFIX = "resolve.post."

APPROVE = "Approved: go ahead with #{post}."
REJECT = "Not approved: decision #{post} is rejected."
NOT_NOW = "Not now: parking #{post}."
ASK_OPTIONS = ("Please restate #{post} as a structured decision_question (a recommended option, one alternative, "
               "each with what it does and costs) so I can answer it in one click.")
CHOSE = 'Chose option {id} ("{label}", {rank}) for #{post}.'
PURPOSE = ("Carry out what post #{post} on thread {thread} asked for, which the human approved; "
           "stay within that request.")


def _one_line(text: str) -> str:
    """Agent-written option text copied into a human post: one line, no double quotes (it sits inside a quote)."""
    return " ".join(text.split()).replace('"', "'")


def _needs_you(c, post_id: int) -> bool:
    return c.execute(f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (post_id,)).fetchone() is not None


def resolve(board: Board, p: Principal, post_id: int, action: str, text: str | None,
            config: dispatch.DispatchConfig, option_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    board._require_human(p, "resolve a post that needs you")
    if action not in ACTIONS:
        raise Invalid(f"action must be one of {ACTIONS}")
    if option_id is not None and action != "choose":
        raise Invalid("option_id is only for action 'choose'")
    if note is not None:
        if action != "choose":
            raise Invalid("note is only for action 'choose'; use 'reply' for your own text")
        if not isinstance(note, str):
            raise Invalid("note must be text")
        note = note.strip() or None
        if note and len(note.encode()) > NOTE_MAX_BYTES:
            raise Invalid(f"note exceeds {NOTE_MAX_BYTES} bytes")
    if action == "reply":
        if not isinstance(text, str) or not text.strip():
            raise Invalid("a reply needs text")
        text = text.strip()
        if len(text.encode()) > board.s.body_max_bytes:
            raise Invalid(f"reply exceeds {board.s.body_max_bytes} bytes")
    elif text is not None:
        raise Invalid("text is only for action 'reply'; the other actions post fixed text")
    item = board.get_post(p, post_id)                                     # 404 when it does not exist
    author = board.conn.execute("SELECT is_human, active FROM agents WHERE name = ?", (item["agent"],)).fetchone()
    to_author = [] if author is None or author["is_human"] or not author["active"] else [item["agent"]]
    if action == "reject" and item["type"] != "decision":
        raise Invalid("only decision posts can be rejected; use Not now or Reply")
    question, option = item.get("decision_question"), None
    if action == "choose":
        if not question:
            raise Invalid(f"post #{post_id} has no structured options; use Approve, Not now or Reply")
        if not isinstance(option_id, str):
            raise Invalid("choose needs option_id")
        option = next((o for o in question["options"] if o["id"] == option_id), None)
        if option is None:
            raise Invalid(f"option_id must be one of post #{post_id}'s options")
    if action == "ask_options":
        if question:
            raise Invalid(f"post #{post_id} already has structured options; choose one or reply")
        if not to_author:
            raise Invalid(f"post #{post_id} was not written by an active agent, so there is no one to ask")
    if action == "approve_launch":
        if not to_author:
            raise Invalid(f"post #{post_id} was not written by an active agent, so there is no one to launch")
        if board._thread_row(item["thread_id"])["status"] != "open":
            raise Conflict(f"thread {item['thread_id']} is closed; reopen it first")

    def still_needs_you(c) -> None:
        if not _needs_you(c, post_id):
            raise Conflict(f"post #{post_id} no longer needs you (it was already handled)")
        # Housekeeping: stamps older than the cooldown are no longer needed.
        c.execute("DELETE FROM board_state WHERE key LIKE ? AND updated_at < ?",
                  (STATE_PREFIX + "%", board.now() - RESOLVE_COOLDOWN_SECONDS))

    key = STATE_PREFIX + str(post_id)
    human_actions.reserve_cooldown(
        board, p, key, RESOLVE_COOLDOWN_SECONDS,
        lambda wait: f"post #{post_id} was just resolved; try again in {wait} s", check=still_needs_you)
    needs_response = False
    if action == "reply":
        body, type_ = text, ("question" if text.endswith("?") else "status")
    elif action == "choose":
        rank = "recommended" if option["id"] == question["recommended_option_id"] else "alternative"
        body = CHOSE.format(id=_one_line(option["id"]), label=_one_line(option["label"]), rank=rank, post=post_id)
        body, type_ = body + (f"\nNote: {note}" if note else ""), "status"
    elif action == "ask_options":
        body, type_, needs_response = ASK_OPTIONS.format(post=post_id), "request", True
    else:
        body = {"approve": APPROVE, "approve_launch": APPROVE, "reject": REJECT, "not_now": NOT_NOW}[action]
        body, type_ = body.format(post=post_id), "status"
    launch = to_author if action == "approve_launch" else None
    try:
        post, rule = human_actions.post_as_human(
            board, p, thread_id=item["thread_id"], body=body, type=type_, to=to_author, needs_response=needs_response,
            launch=launch, purpose=PURPOSE.format(post=post_id, thread=item["thread_id"]))
    except Exception:
        human_actions.release_cooldown(board, key)
        raise
    out: dict[str, Any] = {"action": action, "post_id": post["id"], "resolved_post_id": post_id,
                           "thread_id": item["thread_id"], "to": to_author}
    if action == "choose":
        out["option_id"] = option["id"]
    if action == "approve_launch":
        o = human_actions.launch_outlook(board, config, to_author)
        out |= {"agent": to_author[0], "rule_id": rule["id"] if rule else None,
                "dispatcher_running": o["dispatcher_running"], "paused": o["paused"],
                "live": bool(o["live_agents"]), "no_runner": bool(o["no_runner"])}
    return out
