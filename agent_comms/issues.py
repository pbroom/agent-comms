"""Shared discussions. Explicit links provide context, never delegated authority.

Decisions retain their exact thread/project scope. They do not accept tasks, create grants,
or authorize later joins. Issue content is public board data; sealed source posts cannot link.
"""

from __future__ import annotations

import json

from . import db
from .core import Conflict, Forbidden, Invalid, LimitExceeded, NotFound, iso, _norm_path


def _row(board, issue_id):
    row = board.conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if row is None:
        raise NotFound(f"issue {issue_id} not found")
    return row


def _body(board, value, field="body", limit=None):
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{field} is required")
    value = value.strip()
    if len(value.encode()) > (limit or board.s.body_max_bytes):
        raise Invalid(f"{field} exceeds size limit")
    return value


def _question(board, value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"question", "context", "options", "recommended_option_id"}:
        raise Invalid("decision_question must contain question, context, options and recommended_option_id")
    options = value.get("options")
    if not isinstance(options, list) or len(options) != 2:
        raise Invalid("decision_question requires exactly two options")
    normalized = []
    for option in options:
        if not isinstance(option, dict) or set(option) - {"id", "label", "description", "outcome", "action"}:
            raise Invalid("invalid decision option")
        outcome = option.get("outcome", "answered")
        if outcome not in ("answered", "approved", "declined"):
            raise Invalid("invalid option outcome")
        description = option.get("description", "")
        if not isinstance(description, str):
            raise Invalid("option description must be text")
        normalized.append({"id": _body(board, option.get("id"), "option id", 100),
                           "label": _body(board, option.get("label"), "option label", 200),
                           "description": description.strip(), "outcome": outcome})
        if "action" in option:
            from .decision_actions import validate
            if outcome != "approved":
                raise Invalid('mechanical option actions require approved outcome')
            normalized[-1]['action'] = validate(option['action'])
    ids = [o["id"] for o in normalized]
    if len(set(ids)) != 2 or value.get("recommended_option_id") not in ids:
        raise Invalid("options need unique ids and a recommended_option_id matching an option")
    context = value.get("context", "")
    if not isinstance(context, str):
        raise Invalid("question context must be text")
    result = {"question": _body(board, value.get("question"), "question", 500),
              "context": context.strip(), "options": normalized,
              "recommended_option_id": value["recommended_option_id"]}
    if len(json.dumps(result).encode()) > board.s.body_max_bytes:
        raise Invalid("decision_question exceeds size limit")
    return result


def _link_target(board, p, thread_id, post_id):
    thread = board._thread_row(thread_id)
    if thread["status"] == "closed" and not p.is_human:
        raise Conflict("thread is closed")
    if post_id is not None:
        post = board.conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if post is None or post["thread_id"] != thread_id:
            raise Invalid("post must belong to the linked thread")
        if post["sealed"]:
            raise Forbidden("unseal the source post before linking it to a public issue")
    return thread


def _source_needs_human(board, post_id):
    if post_id is None:
        return False
    # Evaluate the source before inserting its first issue link. Existing issue links do
    # not erase its original request: joining another issue must preserve that attention.
    return bool(
        board.conn.execute(
            f"SELECT 1 FROM posts p WHERE p.id=? AND {board.NEEDS_YOU_SOURCE}", (post_id,)
        ).fetchone()
    )


def _covers(board, issue_question, post_id):
    """Whether an issue whose stored question is `issue_question` (JSON text or None) covers the linked post
    (db.ISSUE_COVERS). Decided once, when the link is made: refining the issue's question later does not move a post
    in or out of the issue. A thread link (no post) covers no post; it is stored as 1, which nothing reads."""
    if post_id is None:
        return True
    return bool(board.conn.execute(
        f"SELECT {db.ISSUE_COVERS} FROM posts p, (SELECT ? AS decision_question) i WHERE p.id=?",
        (issue_question, post_id)).fetchone()[0])


def _write(board, p, session_id, issue_id=None, thread_id=None):
    board._check_agent_write(p)
    board._session(p, session_id)
    if p.is_human:
        return
    count = board.conn.execute(
        """SELECT
        (SELECT COUNT(*) FROM posts WHERE agent=? AND created_at>?) +
        (SELECT COUNT(*) FROM issue_comments WHERE agent=? AND created_at>?)""",
        (p.name, board.now() - 86400, p.name, board.now() - 86400),
    ).fetchone()[0]
    if count >= board.s.daily_post_cap_per_agent:
        raise LimitExceeded("daily post cap reached; ask the human in your own chat")
    if (
        thread_id is not None
        and board._agent_posts_since_human(thread_id) >= board.s.max_agent_posts_per_thread_without_human
    ):
        raise LimitExceeded("thread post cap reached; ask the human in your own chat")
    if issue_id is not None:
        count = board.conn.execute(
            """SELECT COUNT(*) FROM issue_comments c JOIN agents a ON a.name=c.agent
            WHERE c.issue_id=? AND a.is_human=0 AND c.id>COALESCE((SELECT MAX(h.id)
            FROM issue_comments h JOIN agents ha ON ha.name=h.agent WHERE h.issue_id=? AND ha.is_human=1),0)""",
            (issue_id, issue_id),
        ).fetchone()[0]
        if count >= board.s.max_agent_posts_per_thread_without_human:
            raise LimitExceeded("issue discussion cap reached; ask the human in your own chat")


def _event(board, p, session_id, issue_id, kind, body, outcome=None, scope=None, decision=None):
    event = board.conn.execute(
        """INSERT INTO issue_comments(issue_id,session_id,agent,kind,body,outcome,scope,created_at,decision)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            issue_id,
            session_id,
            p.name,
            kind,
            body,
            outcome,
            json.dumps(scope) if scope else None,
            board.now(),
            json.dumps(decision) if decision else None,
        ),
    )
    board.conn.execute("UPDATE issues SET updated_at=? WHERE id=?", (board.now(), issue_id))
    return event.lastrowid


def get_issue(board, p, issue_id):
    out = dict(_row(board, issue_id))
    out["needs_human"] = bool(out["needs_human"])
    out["decision_question"] = json.loads(out["decision_question"]) if out["decision_question"] else None
    for key in ("created_at", "updated_at"):
        out[key] = iso(out[key])
    out["links"] = [
        dict(r)
        for r in board.conn.execute(
            """SELECT l.thread_id,l.post_id,l.needs_human,l.covers_post,t.project,t.title
        FROM issue_links l JOIN threads t ON t.id=l.thread_id WHERE l.issue_id=? ORDER BY l.id""",
            (issue_id,),
        )
    ]
    for link in out["links"]:
        link["needs_human"] = bool(link["needs_human"])
        # Whether this issue's answer answers the linked post (None for a thread link).
        link["covers_post"] = bool(link["covers_post"]) if link["post_id"] is not None else None
        if p.is_human and link['post_id'] is not None:
            source = board.get_post(p, link['post_id'])
            link['source_post'] = {'id': source['id'], 'agent': source['agent'],
                                   'approval_delivery': source['approval_delivery']}
    out["comments"], out["decisions"], out["resolutions"] = [], [], []
    out["resolution"] = None
    for r in board.conn.execute("SELECT * FROM issue_comments WHERE issue_id=? ORDER BY id", (issue_id,)):
        event = {
            "id": r["id"],
            "kind": r["kind"],
            "body": r["body"],
            "agent": r["agent"],
            "created_at": iso(r["created_at"]),
        }
        if r["kind"] == "decision":
            scope = json.loads(r["scope"])
            event.update(
                outcome=r["outcome"],
                thread_ids=[s["thread_id"] for s in scope],
                projects=sorted({s["project"] for s in scope}),
                scope=scope,
            )
            if r["decision"]:
                event.update(json.loads(r["decision"]))
            out["decisions"].append(event)
        elif r["kind"] == "resolution":
            out["resolutions"].append(event)
            out["resolution"] = event
        else:
            out["comments"].append(event)
    return out


def list_issues(board, p, project=None, status=None, query=None, thread_id=None, needs_human=None):
    sql, args = "SELECT i.id FROM issues i WHERE 1=1", []
    if needs_human is not None:
        sql += " AND i.needs_human=?"
        args.append(int(needs_human))
    if status:
        if status not in ("open", "resolved"):
            raise Invalid("status must be open or resolved")
        sql += " AND i.status=?"
        args.append(status)
    if project:
        sql += " AND EXISTS(SELECT 1 FROM issue_links l JOIN threads t ON t.id=l.thread_id WHERE l.issue_id=i.id AND t.project=?)"
        args.append(_norm_path(project))
    if thread_id is not None:
        sql += " AND EXISTS(SELECT 1 FROM issue_links l WHERE l.issue_id=i.id AND l.thread_id=?)"
        args.append(thread_id)
    if query:
        sql += """ AND (instr(lower(i.title),lower(?))>0 OR instr(lower(i.body),lower(?))>0
            OR EXISTS(SELECT 1 FROM issue_links l JOIN threads t ON t.id=l.thread_id
                WHERE l.issue_id=i.id AND (instr(lower(t.project),lower(?))>0
                    OR instr(lower(t.title),lower(?))>0)))"""
        args.extend([query, query, query, query])
    return [
        get_issue(board, p, r[0])
        for r in board.conn.execute(sql + " ORDER BY i.updated_at DESC,i.id DESC LIMIT 200", args)
    ]


def create_issue(board, p, session_id, *, title, body, thread_id, post_id=None, needs_human=True, decision_question=None):
    title, body = _body(board, title, "title", 200), _body(board, body)
    question = _question(board, decision_question)
    if question and any('action' in o for o in question['options']):
        raise Invalid('mechanical actions belong on a source post decision_question')
    if not isinstance(needs_human, bool):
        raise Invalid("needs_human must be boolean")
    with db.write_tx(board.conn) as c:
        _write(board, p, session_id, thread_id=thread_id)
        _link_target(board, p, thread_id, post_id)
        stored = json.dumps(question) if question else None
        covers = _covers(board, stored, post_id)
        # A source's own attention makes the link wait on the human only when this issue's question covers it; a
        # post asking its own question is its own Needs you item, answered on its own.
        pending = int(needs_human or (covers and _source_needs_human(board, post_id)))
        issue_id = c.execute(
            "INSERT INTO issues(title,body,needs_human,created_by,created_at,updated_at,decision_question,question_version) VALUES(?,?,?,?,?,?,?,1)",
            (title, body, pending, p.name, board.now(), board.now(), stored),
        ).lastrowid
        c.execute(
            "INSERT INTO issue_links(issue_id,thread_id,post_id,needs_human,agent,created_at,covers_post) VALUES(?,?,?,?,?,?,?)",
            (issue_id, thread_id, post_id, pending, p.name, board.now(), int(covers)),
        )
        _event(board, p, session_id, issue_id, "created", body)
    board._notify("issue.created", {"issue_id": issue_id})
    return get_issue(board, p, issue_id)


def link_issue(board, p, session_id, issue_id, thread_id, post_id=None):
    with db.write_tx(board.conn) as c:
        row = _row(board, issue_id)
        board._check_agent_write(p)
        board._session(p, session_id)
        _link_target(board, p, thread_id, post_id)
        exists = c.execute(
            "SELECT 1 FROM issue_links WHERE issue_id=? AND thread_id=? AND post_id IS ?",
            (issue_id, thread_id, post_id),
        ).fetchone()
        if not exists:
            _write(board, p, session_id, issue_id, thread_id)
            covers = _covers(board, row["decision_question"], post_id)
            pending = int(post_id is None or (covers and _source_needs_human(board, post_id)))
            c.execute(
                "INSERT INTO issue_links(issue_id,thread_id,post_id,needs_human,agent,created_at,covers_post) VALUES(?,?,?,?,?,?,?)",
                (issue_id, thread_id, post_id, pending, p.name, board.now(), int(covers)),
            )
            if pending:
                c.execute("UPDATE issues SET status='open',needs_human=1 WHERE id=?", (issue_id,))
            _event(
                board,
                p,
                session_id,
                issue_id,
                "linked",
                f"Linked thread #{thread_id}" + (f" post #{post_id}" if post_id else ""),
            )
    return get_issue(board, p, issue_id)


def comment_issue(board, p, session_id, issue_id, body, kind="comment", decision_question=None):
    if kind not in ("comment", "evidence", "proposal", "request"):
        raise Invalid("kind must be comment, evidence, proposal, or request")
    body = _body(board, body)
    if decision_question is not None and kind != "request":
        raise Invalid("only request comments can set decision_question")
    question = _question(board, decision_question)
    if question and any('action' in o for o in question['options']):
        raise Invalid('mechanical actions belong on a source post decision_question')
    with db.write_tx(board.conn) as c:
        _row(board, issue_id)
        _write(board, p, session_id, issue_id)
        _event(board, p, session_id, issue_id, kind, body)
        if kind == "request":
            c.execute("UPDATE issues SET decision_question=?,question_version=question_version+1 WHERE id=?",
                      (json.dumps(question) if question else None, issue_id))
            c.execute("UPDATE issue_links SET needs_human=1 WHERE issue_id=?", (issue_id,))
            c.execute("UPDATE issues SET status='open',needs_human=1 WHERE id=?", (issue_id,))
    return get_issue(board, p, issue_id)


def decide_issue(board, p, session_id, issue_id, body, thread_ids, outcome="answered", selected_option_id=None, expected_question_version=None, delivery_agents=None):
    board._require_human(p, "decide a shared issue")
    if selected_option_id is None:
        body = _body(board, body)
    if outcome not in ("answered", "approved", "declined"):
        raise Invalid("outcome must be answered, approved, or declined")
    if not isinstance(thread_ids, list) or not thread_ids or any(type(t) is not int for t in thread_ids):
        raise Invalid("thread_ids must explicitly select linked threads")
    if delivery_agents is None:
        delivery_agents = {}
    if not isinstance(delivery_agents, dict) or any(
        not isinstance(key, str) or not key.isascii() or not key.isdigit()
        or int(key) <= 0 or str(int(key)) != key or not isinstance(value, str)
        for key, value in delivery_agents.items()
    ):
        raise Invalid('delivery_agents must map exact source post IDs to active agents')
    answer_posts = []
    with db.write_tx(board.conn) as c:
        row = _row(board, issue_id)
        _write(board, p, session_id, issue_id)
        if expected_question_version is not None and (
            type(expected_question_version) is not int or expected_question_version != row["question_version"]
        ):
            raise Conflict("question changed; reload the issue before answering")
        question = json.loads(row["decision_question"]) if row["decision_question"] else None
        if selected_option_id is not None:
            if expected_question_version is None:
                raise Invalid("preset answers require expected_question_version")
            option = next((o for o in question["options"] if o["id"] == selected_option_id), None) if question else None
            if option is None:
                raise Invalid("selected_option_id must match a current question option")
            body = option["label"] + (" — " + option["description"] if option["description"] else "")
            outcome = option["outcome"]
        decision = {"decision_question": question, "question_version": row["question_version"],
                    "selected_option_id": selected_option_id}
        linked = {
            r["thread_id"]: r["project"]
            for r in c.execute(
                "SELECT l.thread_id,t.project FROM issue_links l JOIN threads t ON t.id=l.thread_id WHERE issue_id=?",
                (issue_id,),
            )
        }
        if not set(thread_ids) <= linked.keys():
            raise Invalid("decision scope must contain only linked threads")
        scope = [{"thread_id": t, "project": linked[t]} for t in sorted(set(thread_ids))]
        frozen_links = c.execute('''SELECT l.*,p.agent AS source_agent,a.is_human AS source_is_human FROM issue_links l
            LEFT JOIN posts p ON p.id=l.post_id LEFT JOIN agents a ON a.name=p.agent WHERE l.issue_id=?
            AND l.thread_id IN (SELECT value FROM json_each(?)) ORDER BY l.id''',(issue_id,json.dumps(thread_ids))).fetchall()
        decision['issue_link_ids'] = [link['id'] for link in frozen_links]
        eligible_sources = {str(link['post_id']) for link in frozen_links
                            if link['post_id'] is not None and not link['source_is_human'] and link['covers_post']}
        if delivery_agents and (outcome != 'approved' or not set(delivery_agents) <= eligible_sources):
            raise Invalid('delivery_agents may only select covered source posts in this approved scope')
        if delivery_agents:
            decision['delivery_agents'] = dict(delivery_agents)
        latest = c.execute("SELECT * FROM issue_comments WHERE issue_id=? AND kind='decision' ORDER BY id DESC LIMIT 1",
                           (issue_id,)).fetchone()
        if (latest is not None and latest['body']==body and latest['outcome']==outcome
                and json.loads(latest['scope'] or 'null')==scope
                and json.loads(latest['decision'] or 'null')==decision):
            return get_issue(board,p,issue_id)
        if any(board._thread_row(tid)['status'] != 'open' for tid in thread_ids):
            raise Conflict('reopen every selected thread before delivering an issue answer')
        event_id = _event(board, p, session_id, issue_id, "decision", body, outcome, scope, decision)
        for tid in sorted(set(thread_ids)):
            selected = [link for link in frozen_links if link['thread_id']==tid]
            # A separate exact source can have a separate implementer. Grouping
            # never changes which question or link the answer actually covers.
            groups = []
            if outcome == 'approved':
                from .approval_owners import delivery
                owners, remaining = {}, []
                for link in selected:
                    if str(link['post_id']) not in eligible_sources:
                        remaining.append(link)
                        continue
                    selected_owner = delivery(board, {'id': link['post_id']}, delivery_agents.get(str(link['post_id'])))
                    if selected_owner['requires_choice'] or selected_owner['recipient'] is None:
                        raise Invalid(f"post #{link['post_id']}: {selected_owner['reason']}; choose an active agent")
                    owners.setdefault(selected_owner['recipient'], []).append(link)
                groups.extend((links, owner) for owner, links in sorted(owners.items()))
                if remaining:
                    groups.append((remaining, None))
            else:
                groups.append((selected, None))
            for group, owner in groups:
                source_ids = sorted({link['post_id'] for link in group
                                     if link['post_id'] is not None and not link['source_is_human'] and link['covers_post']})
                intended = {link['source_agent'] or link['agent'] for link in group}
                recipients = [owner] if owner else [agent for agent in sorted(intended) if c.execute(
                    'SELECT 1 FROM agents WHERE name=? AND is_human=0', (agent,)).fetchone()]
                answer = board.create_post(p,session_id,thread_id=tid,type='status',
                    body=f'Human answer for issue #{issue_id} ({outcome}):\n{body}',to=recipients,
                    answer_to=source_ids or None,_answer_recipient=owner,_in_transaction=True)
                answer_posts.append(answer)
                for link in group:
                    c.execute('''INSERT INTO issue_answer_links(decision_comment_id,issue_link_id,answer_post_id,question_version)
                        VALUES (?,?,?,?)''',(event_id,link['id'],answer['id'],row['question_version']))
        c.execute(
            "UPDATE issue_links SET needs_human=0 WHERE issue_id=? AND thread_id IN (SELECT value FROM json_each(?))",
            (issue_id, json.dumps(thread_ids)),
        )
        c.execute(
            "UPDATE issues SET needs_human=EXISTS(SELECT 1 FROM issue_links WHERE issue_id=? AND needs_human=1) WHERE id=?",
            (issue_id, issue_id),
        )
    for answer in answer_posts:
        board._notify('post.created',{'post_id':answer['id'],'thread_id':answer['thread_id'],
            'agent':p.name,'to':answer['to'],'needs_response':False,'sealed':False})
    return get_issue(board, p, issue_id)


def resolve_issue(board, p, session_id, issue_id, body):
    board._require_human(p, "resolve a shared issue")
    body = _body(board, body)
    with db.write_tx(board.conn) as c:
        _row(board, issue_id)
        _write(board, p, session_id, issue_id)
        _event(board, p, session_id, issue_id, "resolution", body)
        c.execute("UPDATE issue_links SET needs_human=0 WHERE issue_id=?", (issue_id,))
        c.execute("UPDATE issues SET status='resolved',needs_human=0 WHERE id=?", (issue_id,))
    return get_issue(board, p, issue_id)


def _completed_answer_work(board, thread_id):
    """Conservative completion proof; silence and legacy attention are never evidence."""
    from . import requests
    c = board.conn
    linked = c.execute('''SELECT 1 FROM posts p WHERE p.thread_id=? AND (
        EXISTS (SELECT 1 FROM answer_links a WHERE a.answer_post_id=p.id) OR
        EXISTS (SELECT 1 FROM issue_answer_links a WHERE a.answer_post_id=p.id)) LIMIT 1''',(thread_id,)).fetchone()
    if not linked:
        return False
    if c.execute("SELECT 1 FROM tasks WHERE thread_id=? AND status NOT IN ('done','declined') LIMIT 1",(thread_id,)).fetchone():
        return False
    if c.execute(f'SELECT 1 FROM posts p WHERE p.thread_id=? AND {board.NEEDS_YOU_SOURCE} LIMIT 1',(thread_id,)).fetchone():
        return False
    if c.execute('''SELECT 1 FROM legacy_attention_answers l JOIN posts p ON p.id=l.source_post_id
        WHERE p.thread_id=? AND NOT EXISTS (SELECT 1 FROM answer_links a WHERE a.source_post_id=p.id) LIMIT 1''',
        (thread_id,)).fetchone():
        return False
    obligations = 0
    for post in c.execute('SELECT p.*,a.is_human FROM posts p JOIN agents a ON a.name=p.agent WHERE p.thread_id=?',(thread_id,)):
        rows = requests.for_post(board,post)
        if not rows and c.execute('''SELECT 1 WHERE EXISTS(SELECT 1 FROM answer_links WHERE answer_post_id=?)
            OR EXISTS(SELECT 1 FROM issue_answer_links WHERE answer_post_id=?)''',(post['id'],post['id'])).fetchone():
            return False  # no recorded intended agent: completion cannot be inferred
        if (not rows and not post['is_human'] and post['type'] in ('question','proposal','decision')
                and not c.execute('SELECT 1 FROM answer_links WHERE source_post_id=?',(post['id'],)).fetchone()
                and not c.execute('SELECT 1 FROM attention_resolutions WHERE post_id=?',(post['id'],)).fetchone()):
            return False
        for row in rows:
            obligations += 1
            if row['state'] != 'finished' or not row['evidence_post_ids']:
                return False
            for evidence_id in row['evidence_post_ids']:
                evidence = c.execute('SELECT thread_id,sealed FROM posts WHERE id=?',(evidence_id,)).fetchone()
                if evidence is None or evidence['thread_id'] != thread_id or evidence['sealed']:
                    return False
    return obligations > 0


def reconcile_completed(board, p, session_id, thread_id):
    """Called inside explicit request completion; close only fully evidenced answer work.

    This internal helper has no transport or permissions surface. The caller holds
    the write transaction after validating the actual request's completing actor.
    """
    if not board.conn.in_transaction:
        raise Invalid('completion reconciliation requires an active transaction')
    session = board._session(p,session_id)
    thread = board._thread_row(thread_id)
    result = {'closed_thread_ids':[],'resolved_issue_ids':[]}
    if session['project'] != thread['project'] or thread['status'] != 'open':
        return result
    if not _completed_answer_work(board,thread_id):
        return result
    c = board.conn
    close_candidates = {thread_id}
    for issue in c.execute('''SELECT DISTINCT i.* FROM issues i JOIN issue_links l ON l.issue_id=i.id
        WHERE l.thread_id=? AND i.status='open' ''',(thread_id,)).fetchall():
        if issue['needs_human']:
            continue
        links = c.execute('SELECT * FROM issue_links WHERE issue_id=?',(issue['id'],)).fetchall()
        if not links or any(link['needs_human'] for link in links):
            continue
        if any(board._thread_row(link['thread_id'])['project'] != session['project'] for link in links):
            continue
        if any(not c.execute('''SELECT 1 FROM issue_answer_links WHERE issue_link_id=? AND question_version=?''',
                              (link['id'],issue['question_version'])).fetchone() for link in links):
            continue
        if not all(_completed_answer_work(board,tid) for tid in {link['thread_id'] for link in links}):
            continue
        _event(board,p,session_id,issue['id'],'resolution',
               'All exact linked answer requests have explicit completion evidence; required tasks are terminal.')
        c.execute("UPDATE issues SET status='resolved',needs_human=0 WHERE id=?",(issue['id'],))
        result['resolved_issue_ids'].append(issue['id'])
        close_candidates.update(link['thread_id'] for link in links)
    for tid in sorted(close_candidates):
        if c.execute('''SELECT 1 FROM issue_links l JOIN issues i ON i.id=l.issue_id
            WHERE l.thread_id=? AND i.status!='resolved' LIMIT 1''',(tid,)).fetchone():
            continue
        if board._thread_row(tid)['status']=='open' and _completed_answer_work(board,tid):
            c.execute("UPDATE threads SET status='closed' WHERE id=?",(tid,))
            result['closed_thread_ids'].append(tid)
    return result
