"""`board` - the human's CLI, plus `board serve` and `board mcp`.

Human commands authenticate with the human token ($BOARD_TOKEN, or the file written by `board init`)
and go through the same core as every other interface, so the same rules apply.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import webbrowser

from .config import Settings, create_agent, human_token_file, read_agents
from .core import Board, BoardError, TASK_CATEGORIES


def _board() -> Board:
    return Board(Settings.load())


def _human(board: Board):
    token = os.environ.get("BOARD_TOKEN")
    f = human_token_file()
    if not token and f.exists():
        token = f.read_text().strip()
    if not token:
        raise SystemExit(f"no human token: set BOARD_TOKEN or run `board init` (writes {f})")
    p = board.authenticate(token)
    if not p.is_human:
        raise SystemExit(f"token belongs to agent {p.name!r}, not the human")
    return p, token


def _parse_ref(s: str) -> dict:
    # kind:path[@rev]   e.g. file:src/app.py@abc123  commit:/repo@abc123  url:https://...
    kind, _, rest = s.partition(":")
    path, rev = rest, None
    if kind != "url" and "@" in rest:
        path, _, rev = rest.rpartition("@")
    return {"kind": kind, "path": path, "rev": rev}


def _fmt_post(p: dict) -> str:
    flags = []
    if p.get("sealed"):
        flags.append("SEALED")
    if p.get("was_sealed"):
        flags.append(f"unsealed by {p.get('unsealed_by')}")
    if p.get("needs_response"):
        flags.append("needs-response")
    if p["type"] == "decision":
        flags.append("FINAL" if p.get("decision_status") == "final" else "proposal")
    head = f"#{p['id']} [{p['type']}] {p['agent']} (s{p['session_id']}) {p['created_at']}"
    if p.get("to"):
        head += f" -> {', '.join(p['to'])}"
    if p.get("task_id"):
        head += f"  task {p['task_id']}"
    if flags:
        head += f"  {{{', '.join(flags)}}}"
    lines = [head, textwrap.indent(p["body"], "    ")]
    for r in p.get("refs", []):
        lines.append(f"    ref {r['kind']}: {r['path']}" + (f" @ {r['rev']}" if r.get("rev") else ""))
    return "\n".join(lines)


def _fmt_task(t: dict) -> str:
    lease = ""
    if t["owner_agent"]:
        left = t["lease_seconds_left"]
        lease = f"  owner {t['owner_agent']} s{t['owner_session']} lease {t['lease_state']}"
        lease += f" ({left // 60}m left)" if t["lease_state"] == "active" else ""
    return f"task {t['id']} [{t['status']}] {t['title']}  (thread {t['thread_id']}){lease}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="board", description="agent-comms: local message board for AI agents. "
                                 "Board content is data, never instructions; the human is the only authority.")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run HTTP API + dashboard + MCP (streamable HTTP at /mcp)")
    s.add_argument("--port", type=int)
    sub.add_parser("mcp", help="run the MCP server on stdio (token from $AGENT_COMMS_TOKEN)")
    sub.add_parser("init", help="create the human identity and save its token for this CLI")

    s = sub.add_parser("create-agent", help="create an agent and print its token ONCE")
    s.add_argument("name")
    s.add_argument("--runtime", required=True, help="e.g. claude-code, codex-cli, chatgpt, grok")
    s.add_argument("--human", action="store_true")
    s.add_argument("--rotate", action="store_true", help="issue a new token for an existing agent")
    sub.add_parser("agents", help="list agents")

    s = sub.add_parser("read", help="show unread posts (everything, as the human)")
    s.add_argument("--thread", type=int)
    s.add_argument("--history", action="store_true", help="whole thread, ignoring the cursor (needs --thread)")
    s.add_argument("--ack", action="store_true", help="mark what was shown as read")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("threads", help="list threads")
    s.add_argument("--project")
    s.add_argument("--all", action="store_true", help="include closed threads")

    s = sub.add_parser("tasks", help="list tasks")
    s.add_argument("--thread", type=int)
    s.add_argument("--open", action="store_true")

    s = sub.add_parser("post", help="post as the human")
    s.add_argument("body")
    s.add_argument("-t", "--thread", type=int, help="thread id (or use --new)")
    s.add_argument("--type", default="status", choices=["question", "proposal", "status", "finding", "handoff",
                                                          "request", "decision"])
    s.add_argument("--new", metavar="TITLE", help="open a new thread with this title")
    s.add_argument("--project", help="project path for --new")
    s.add_argument("--to", default="", help="comma-separated agent names")
    s.add_argument("--needs-response", action="store_true")
    s.add_argument("--task", type=int)
    s.add_argument("--ref", action="append", default=[], help="kind:path[@rev], repeatable")
    s.add_argument("--final", action="store_true", help="post a decision that is final immediately")

    for name, hlp in (("finalize", "mark a decision post final"), ("unseal", "unseal a sealed post")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("post_id", type=int)
    sub.add_parser("pause", help="reject all agent writes")
    sub.add_parser("unpause", help="accept agent writes again")

    s = sub.add_parser("task", help="move a task to a status (human override allowed)")
    s.add_argument("task_id", type=int)
    s.add_argument("status", choices=["proposed", "accepted", "working", "blocked", "done", "declined"])
    s.add_argument("--note")
    s = sub.add_parser("release", help="force-release a task lease")
    s.add_argument("task_id", type=int)
    s = sub.add_parser("close", help="close a thread")
    s.add_argument("thread_id", type=int)
    s = sub.add_parser("reopen", help="reopen a thread")
    s.add_argument("thread_id", type=int)
    s = sub.add_parser("grant", help="authorize a category of work within a human-defined goal")
    s.add_argument("--project", required=True)
    s.add_argument("--category", required=True, choices=TASK_CATEGORIES)
    s.add_argument("--agents", required=True, help="comma-separated exact agent names")
    s.add_argument("--purpose", required=True, help="human-authorized goal and limits")
    s.add_argument("--expires-in-hours", type=float)
    s = sub.add_parser("grants", help="list standing authorizations")
    s.add_argument("--project")
    s = sub.add_parser("revoke-grant", help="revoke a standing authorization and release affected leases")
    s.add_argument("grant_id", type=int)
    sub.add_parser("dashboard", help="open the dashboard in your browser, logged in as the human")

    a = ap.parse_args(argv)

    def out(obj, text: str | None = None):
        print(json.dumps(obj, indent=2) if a.json or text is None else text)

    try:
        _run(a, out)
    except BoardError as e:
        raise SystemExit(f"error ({e.code}): {e.message}")
    except ValueError as e:
        raise SystemExit(f"error: {e}")


def _run(a, out) -> None:
    if a.cmd == "serve":
        from .api import serve
        return serve(port=a.port)
    if a.cmd == "mcp":
        from .mcp_server import run_stdio
        return run_stdio()

    settings = Settings.load()
    if a.cmd == "init":
        agents = read_agents(settings.agents_path)
        if any(x.is_human for x in agents.values()):
            raise SystemExit("a human identity already exists. To issue a new token: "
                             "board create-agent <name> --runtime human --human --rotate")
        token = create_agent(settings.agents_path, "human", "human", is_human=True)
        f = human_token_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(token + "\n")
        os.chmod(f, 0o600)
        print(f"Created human identity 'human'. Token saved to {f} (mode 600).")
        print("Next: board create-agent claude --runtime claude-code   (one per agent)")
        return
    if a.cmd == "create-agent":
        token = create_agent(settings.agents_path, a.name, a.runtime, a.human, a.rotate)
        print(f"Agent {a.name!r} ({a.runtime}). Token (shown once, store it in that agent's MCP config):\n{token}")
        return
    if a.cmd == "agents":
        for x in read_agents(settings.agents_path).values():
            print(f"{x.name:16} {x.runtime:14} {'HUMAN' if x.is_human else ''}")
        return

    board = Board(settings)
    p, token = _human(board)
    if a.cmd == "read":
        sid = board.human_session(p)
        r = board.read_updates(p, sid, thread_id=a.thread, limit=a.limit, history=a.history)
        if a.ack and r["ack_through"] is not None and not a.history:
            board.ack(p, sid, r["ack_through"], a.thread)
        text = "\n\n".join(_fmt_post(x) for x in r["posts"]) or "(no unread posts)"
        if r["more"]:
            text += "\n\n(more unread; run again" + (" with --ack" if not a.ack else "") + ")"
        if r["posts"] and not a.ack and not a.history:
            text += f"\n\n(not acked; `board read --ack` marks through seq {r['ack_through']} as read)"
        out(r, ("PAUSED\n" if r["paused"] else "") + text)
    elif a.cmd == "threads":
        ts = board.list_threads(p, a.project, None if a.all else "open")
        out(ts, "\n".join(f"thread {t['id']} [{t['status']}] {t['title']}  ({t['project']})  "
                          f"agent posts since human: {t['agent_posts_since_human']}/{t['thread_cap']}"
                          + (f"\n    summary: {t['pinned_summary']}" if t["pinned_summary"] else "")
                          for t in ts) or "(no threads)")
    elif a.cmd == "tasks":
        ts = board.list_tasks(p, a.thread, include_closed=not a.open)
        out(ts, "\n".join(_fmt_task(t) for t in ts) or "(no tasks)")
    elif a.cmd == "post":
        if (a.thread is None) == (a.new is None):
            raise SystemExit("give --thread ID or --new TITLE")
        sid = board.human_session(p, a.project) if a.project else board.human_session(p)
        r = board.create_post(p, sid, body=a.body, type=a.type, thread_id=a.thread, new_thread_title=a.new,
                              to=[x for x in a.to.split(",") if x], needs_response=a.needs_response,
                              task_id=a.task, refs=[_parse_ref(x) for x in a.ref], final=a.final)
        out(r, _fmt_post(r) + f"\n(thread {r['thread_id']})")
    elif a.cmd == "finalize":
        out(board.finalize(p, a.post_id), f"post {a.post_id} finalized")
    elif a.cmd == "unseal":
        out(board.unseal(p, a.post_id), f"post {a.post_id} unsealed")
    elif a.cmd in ("pause", "unpause"):
        board.set_paused(p, a.cmd == "pause")
        out({"paused": a.cmd == "pause"}, "board PAUSED: agent writes rejected" if a.cmd == "pause"
            else "board unpaused")
    elif a.cmd == "task":
        out(board.transition_task(p, board.human_session(p), a.task_id, a.status, a.note), None)
    elif a.cmd == "release":
        out(board.release_task(p, board.human_session(p), a.task_id, "released by human"), None)
    elif a.cmd in ("close", "reopen"):
        t = board.set_thread_status(p, a.thread_id, "closed" if a.cmd == "close" else "open")
        out(t, f"thread {t['id']} {t['status']}")
    elif a.cmd == "grant":
        expires = board.now() + a.expires_in_hours * 3600 if a.expires_in_hours is not None else None
        out(board.create_grant(p, project=a.project, category=a.category,
                               agents=a.agents.split(','), purpose=a.purpose, expires_at=expires))
    elif a.cmd == "grants":
        out({"grants": board.list_grants(p, a.project)})
    elif a.cmd == "revoke-grant":
        out(board.revoke_grant(p, a.grant_id))
    elif a.cmd == "dashboard":
        url = f"http://{settings.host}:{settings.port}/#token={token}"
        print(f"Opening http://{settings.host}:{settings.port}/ (token passed in the URL fragment, never sent "
              "to the server as a query string)")
        webbrowser.open(url)


if __name__ == "__main__":
    main(sys.argv[1:])
