"""Access a runner holds on one machine, declared by the human in that machine's config, used to route work by who can
do it rather than by who it was addressed to.

`[dispatch.runner_access]` maps a runner key (an agent name or runtime under `[dispatch.runners]`) to the access that
runner has on this machine. The only kind the board routes on today is a signed-in browser:
`"browser:https://usdossiolab.sharepoint.com"` says a run of that runner here drives a browser already signed in to that
origin (a saved profile the human signed into, or the human's own browser through an extension). The board never sets
one up and never stores a credential: the runner's own argv/config provides the browser, the declaration only says it
is there.

A node reports its declarations in hello; the hub honours one only when its own `[dispatch.nodes.<name>] access` lists
it too, so a node cannot widen what the hub routes to it. A request that binds a browser target with
execution_context "desktop" is launched only for a runner that declares `browser:<that origin>` (dispatch.py), and a
run launched that way may attest a desktop probe for exactly those origins (browser_readiness._context).
"""
from __future__ import annotations

from typing import Any

BROWSER = "browser:"


def browser(origin: str) -> str:
    return BROWSER + origin


def canonical(entry: Any, where: str) -> str:
    """`browser:<http(s) origin>` with the origin canonicalized like a bound browser target (`https://host:443`)."""
    from . import browser_readiness
    from .core import Invalid
    if not isinstance(entry, str) or not entry.startswith(BROWSER):
        raise ValueError(f"{where}: access entries look like \"browser:https://host\" ({entry!r})")
    raw = entry[len(BROWSER):]
    try:
        url, origin = browser_readiness.target(raw)
    except Invalid:
        raise ValueError(f"{where}: {raw!r} is not an http(s) origin") from None
    if not browser_readiness.is_plain_origin(origin) or urlpath(url) != "/":
        raise ValueError(f"{where}: {raw!r} must be an origin only (scheme, host and optional port)")
    return browser(origin)


def urlpath(url: str) -> str:
    from urllib.parse import urlsplit
    u = urlsplit(url)
    return (u.path or "/") + ("?" + u.query if u.query else "") + ("#" + u.fragment if u.fragment else "")


def parse(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise ValueError(f"{where} must be a non-empty list of access entries")
    return sorted({canonical(e, where) for e in value})


def parse_table(value: Any, where: str = "[dispatch.runner_access]") -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} maps a runner key to a list like [\"browser:https://host\"]")
    return {k: parse(v, f"{where} {k}") for k, v in value.items()}


def browser_origins(entries: list[str] | None) -> list[str]:
    return sorted(e[len(BROWSER):] for e in (entries or []) if e.startswith(BROWSER))


ROUTED_PREFIX = "request.access_route."   # <successor post id>: {source_post_id, recipient, from_agent, to_agent, ...}


def routed_successor(board, post_id: int) -> dict | None:
    """The access-routing record when `post_id` is itself a request the dispatcher routed by access, else None."""
    import json
    row = board.conn.execute("SELECT value FROM board_state WHERE key=?", (ROUTED_PREFIX + str(post_id),)).fetchone()
    return json.loads(row["value"]) if row else None


def route_successor(board, human, post_id: int, recipient: str, to_agent: str, origins: list[str], where: str) -> int:
    """Hand a signed-in browser request to another agent whose runner holds that access, the way a human repost does
    (decision_actions.execute 'repost'): a successor request in the same thread addressed to `to_agent`, carrying the
    same bound target, and the original blocked with the destination named. The successor's evidenced completion
    finishes the original (decision_actions.reconcile_successor). No new scope is granted: the successor points at the
    original and its existing authorization. Returns the successor post id."""
    import json
    from . import browser_readiness, db, decision_actions, requests, workstreams
    from .core import Conflict
    sid = board.human_session(human)
    with db.write_tx(board.conn):
        source, row = requests._context(board, human, sid, post_id, recipient)
        if source['sealed'] or workstreams.get_for_post(board, post_id) is not None:
            raise Conflict('sealed or managed requests are not routed by access')
        if row['state'] not in ('queued', 'blocked'):
            raise Conflict('only queued or blocked requests are routed by access')
        if routed_successor(board, post_id) is not None:
            raise Conflict('a request already routed by access is not routed again')
        decision_actions.assert_execution_authorized(board, post_id, to_agent)
        req = browser_readiness.requirement(board, post_id, recipient)
        if req is None or req['execution_context'] != 'desktop':
            raise Conflict('only signed-in browser requests are routed by access')
        held = ", ".join(origins)
        target = board.create_post(
            human, sid, thread_id=source['thread_id'], type='request', to=[to_agent], needs_response=True,
            body=(f"Routed request #{post_id} (addressed to {row['assigned_agent']}) to {to_agent}: its runner on "
                  f"{where} holds a signed-in browser for {held}, which that request needs. Read #{post_id} with "
                  f"board_read_updates(post_ids=[{post_id}]) and do exactly what it asks; no new scope or access is "
                  f"granted. When done, mark this request finished with evidence posts: that completes #{post_id}."),
            # The source's declared outputs come along, so output checks and the run directory choice still apply.
            refs=[{'kind': 'artifact', 'path': f"board:post/{post_id}"}]
                 + [{'kind': 'output', 'path': r['path']} for r in source.get('refs') or []
                    if isinstance(r, dict) and r.get('kind') == 'output'],
            browser_requirement={'recipient': to_agent, 'target_url': req['target_url'],
                                 'execution_context': 'desktop'},
            _in_transaction=True)
        reason = (f"Routed by access to {to_agent} on {where} (signed-in browser for {held}); "
                  f"successor post #{target['id']}")
        requests.progress(board, human, sid, post_id, recipient, 'blocked', reason=reason,
                          expected_version=row['version'], _in_transaction=True)
        link = {'source_post_id': post_id, 'recipient': recipient, 'source_version': row['version'] + 1,
                'successor_recipient': to_agent, 'authorization_decision_id': None, 'action_post_id': None}
        now = board.now()
        board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
                           ('request.successor.' + str(target['id']), json.dumps(link), human.name, now))
        board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
                           (ROUTED_PREFIX + str(target['id']),
                            json.dumps({'source_post_id': post_id, 'recipient': recipient,
                                        'from_agent': row['assigned_agent'], 'to_agent': to_agent,
                                        'origins': origins, 'where': where}), human.name, now))
    return target['id']
