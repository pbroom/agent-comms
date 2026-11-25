"""A schema-free run of hosted sessions, recorded: every frame, result turn, claim, tool answer, record, listing and
CLI text it produces, normalized (ids and tokens made deterministic). Structured results promise all of it byte for
byte as on main when no schema is given. tests/fixtures/schema_free_main.json was recorded by THIS module run against
main's code (re-pinned to d4b92e5 for Claude turn correlation; first recorded at a8b90cc), and test_structured_results
compares this checkout's run with it. Only APIs main has are used.

Claude turn correlation (DESIGN_NOTES, 2026-11-24) adds every bracket sequence through the hub and the node, with a
source-faithful stream (lifecycle rows and echo fields on init, assistant, thinking and result frames whenever the
line had a uuid, none when it had not: measured on 2.1.330), every process's raw stdin, every raw transcript, the
host's events in order and the board's log lines. It promises two cases (`record(verified=...)`):
- no version VERIFIED (production): byte for byte main's, everything, with no allowance at all;
- the VERIFIED set as shipped (2.1.330), which these processes do not report (OTHER_VERSION): main's but for the
  allowance `allowed` takes out of the raw bytes, exactly: the `uuid` key on the user lines STAGED before each
  process's first `init` (a line staged then may be written after that init, as the zero-byte case's is, still
  carrying the uuid it was staged with), and the echo fields and lifecycle rows the process wrote for them; and,
  in the logs, the one INFO line per process saying that turn correlation is off for it (`told`).

Mid-turn steering, step 2 (DESIGN_NOTES "Mid-turn steering, step 2: Codex steering, as built"; the design's K) adds
what K names for a run in which no steer is sent, and nothing else: `named` (`record(named=True)`, `step2_named`)
takes out of the RAW run, before anything is normalized, exactly these and records what it took (`NAMED`):
- the Codex wire: `clientUserMessageId` (a fresh uuid4), the last key of each `turn/start` line's params;
- the protocol: `steer` at the end of a listing's `features`, and `steer` in a Codex session record's `modes`;
- the host's events: a `steer` key whose value is None (the field of a turn's end that carries a steer's outcome).
Anything else that looks similar stays, and differs. The run also fetches the listing at protocol 5 (`listing_p5`).

    PYTHONPATH=<main's tree>:<main's tree>/tests python tests/golden_schema_free.py OUT.json    # (re)record
"""
import itertools
import json
import re
import secrets
import sys
import uuid
from pathlib import Path


def record(tmp_path: Path, verified=None, allow: bool = False, named: bool = False) -> dict:
    """`verified`: the Claude adapter's VERIFIED for this run (set on main's adapter too, which has no use for it).
    `allow`: take the allowance out of the raw stdin and transcripts before anything is normalized (`allowed`).
    `named`: take out what mid-turn steering's step 2 names for a run with no steer (`step2_named`), likewise."""
    import logging
    from agent_comms.providers import claude
    counter = itertools.count(1)
    real_hex, real_uuid4 = secrets.token_hex, uuid.uuid4
    secrets.token_hex = lambda n=32: format(next(counter), "x").rjust(2 * n, "0")[-2 * n:]
    uuid.uuid4 = lambda: uuid.UUID(int=next(counter))
    had, before = hasattr(claude, "VERIFIED"), getattr(claude, "VERIFIED", None)
    if verified is not None:
        claude.VERIFIED = frozenset(verified)
    logs: list = []

    class Keep(logging.Handler):
        def emit(self, rec):
            logs.append([rec.name, rec.levelname, rec.getMessage()])

    keep, board = Keep(logging.INFO), logging.getLogger("agent_comms")
    level = board.level
    board.addHandler(keep)
    board.setLevel(logging.INFO)
    try:
        return _run(tmp_path, logs, allow, named)
    finally:
        board.removeHandler(keep)
        board.setLevel(level)
        secrets.token_hex, uuid.uuid4 = real_hex, real_uuid4
        if had:
            claude.VERIFIED = before
        elif hasattr(claude, "VERIFIED"):
            del claude.VERIFIED


def _run(tmp_path: Path, logs: list, allow: bool, named: bool = False) -> dict:
    import dataclasses
    from agent_comms import cli, hosted, hosted_results, hosted_spawn as hs
    from test_hosted_sessions import session_env, show, start, tick, written
    from test_hosted_tree import TREE, answer, child, tree_config

    env = session_env(tmp_path, dispatch=TREE, config=tree_config)
    out: dict = {}
    events: list = []
    # The node's raw report and hello bodies, as it sends them (review round 1: the golden's limits). A report's
    # `seq` is the wall clock in ns (node_sessions._stamp): taken out here, the same on main's code and this one.
    bodies: list = []
    node_client = env.node.sessions.client
    real_request = node_client.request

    def recording(method, url, *args, **kw):
        if method == "POST" and (url == "/api/nodes/hello" or str(url).startswith("/api/nodes/sessions/")):
            body = json.loads(json.dumps(kw.get("json")))
            if isinstance(body, dict) and "seq" in body:
                body["seq"] = 0
            bodies.append([str(url), body])
        return real_request(method, url, *args, **kw)
    node_client.request = recording
    host = env.node.sessions.host
    real_tick = host.tick

    def recorded_tick():
        got = real_tick()
        events.append([dataclasses.asdict(e) for e in got])     # one list per pass: the tick boundaries kept
        return got

    host.tick = recorded_tick

    def turns(sid):
        got = []
        for call, proc in zip(env.node.session_spawner.calls, env.node.session_spawner.children):
            if sid in call["argv"]:
                got += [x["message"]["content"] for x in written(proc) if x.get("type") == "user"]
        return got

    def bound(sid):
        hs.ensure_secret(env.board)
        return hs.Ident(hosted=sid, key=hs.bind_key(env.board.conn, sid), claimed=True)

    root = start(env, "first")["session_id"]
    tick(env)
    answer(env, root, "root reply")
    kid = child(env, root, "a human's task", role="review", title="Look")["session_id"]
    tick(env)
    answer(env, kid, "the human child's reply")
    tick(env)
    out["root_turns"] = turns(root)
    out["grant"] = env.human.post("/api/spawn-grants", json={"project": hosted._session(env.board.conn, root)["project"],
                                                             "agents": ["claude"], "access": "workspace",
                                                             "hours": 1}).json()
    sp = hs.spawn(env.board, env.p["claude"], bound(root), task="an agent's task", agent="claude", request_id="r1")
    out["spawn_answer"] = sp
    out["spawn_retry"] = hs.spawn(env.board, env.p["claude"], bound(root), task="an agent's task", agent="claude",
                                  request_id="r1")
    akid = sp["child"]["id"]
    tick(env)
    out["agent_kid_turns_1"] = turns(akid)
    answer(env, akid, "the agent child's reply")
    out["claim"] = hs.taken(hosted_results.claim(env.board, root, "claude", child=akid, message=1))
    out["send_answer"] = hs.send(env.board, env.p["claude"], bound(root), akid, message="a follow-up",
                                 request_id="s1")
    out["send_retry"] = hs.send(env.board, env.p["claude"], bound(root), akid, message="a follow-up",
                                request_id="s1")
    tick(env)
    out["agent_kid_turns_2"] = turns(akid)
    answer(env, akid, "the follow-up's reply")
    tick(env, times=2)
    out["root_turns_after"] = turns(root)
    out["status"] = hs.status(env.board, env.p["claude"], bound(root))
    out["show_root"] = show(env, root)
    out["show_kid"] = show(env, kid)
    out["show_agent_kid"] = show(env, akid)
    out["message"] = env.human.get(f"/api/hosted-sessions/{akid}/messages/1").json()
    out["send_human"] = env.human.post(f"/api/hosted-sessions/{root}/messages",
                                       json={"message": "more", "request_id": "h1"}).json()
    out["send_human_retry"] = env.human.post(f"/api/hosted-sessions/{root}/messages",
                                             json={"message": "more", "request_id": "h1"}).json()
    out["child_retry"] = env.human.post(f"/api/hosted-sessions/{root}/children",
                                        json={"message": "x", "request_id": "c1"}).json()
    out["child_retry_again"] = env.human.post(f"/api/hosted-sessions/{root}/children",
                                              json={"message": "x", "request_id": "c1"}).json()
    out["list"] = env.human.get("/api/hosted-sessions", params={"all": True}).json()
    out["listing"] = env.laptop.get("/api/nodes/sessions", params={"tree": 4}).json()
    out["trees"] = env.human.get("/api/hosted-trees").json()
    out["tree_detail"] = env.human.get(f"/api/hosted-trees/{root}").json()
    out["cli_show_root"] = cli._fmt_hosted_detail(show(env, root))
    out["cli_list"] = cli._fmt_hosted_tree(env.human.get("/api/hosted-sessions").json()["sessions"])
    out["records"] = {k: json.loads(v) for k, v in env.board.conn.execute(
        "SELECT key, value FROM board_state WHERE key LIKE 'host.session.%' OR key LIKE 'host.msg.%' OR key LIKE "
        "'host.result.%' OR key LIKE 'host.outbox.%' ORDER BY key")}
    _sequences(env, out, allow)
    _host_cases(tmp_path, out, allow)
    out["host_events"] = events
    out["node_bodies"] = bodies
    out["logs"] = [x for x in logs if not x[2].startswith("HTTP Request:") and not (allow and told(x, REMOVED))]
    # The listing at protocol 5 (step 2's): with no steer, what a protocol-4 fetch gets (main's code answers it so).
    out["listing_p5"] = env.laptop.get("/api/nodes/sessions", params={"tree": 5}).json()
    # Review round 3, the parity rule: with no steer attempt live, a cancel, a shutdown and a flush overlapping it are
    # main's, event for event and report for report.
    out["zz_parity"] = _parity(tmp_path)     # (named to sort last: the ids before it keep their placeholders)
    NAMED.clear()
    if named:
        step2_named(out)
    return normalized(out, tmp_path)


TOLD = re.compile(r"Claude session \S+: this process reports Claude Code (2\.1\.329|no version); turn correlation is "
                  r"off for it \(attributed by order\)")


def told(entry: list, removed: list) -> bool:
    """The allowance in the logs: the INFO line each Claude process's first `init` gets while some version is
    VERIFIED (how the owner sees whether turn correlation is on), for a process that reports another version, or
    none. Nothing else in the logs is taken out."""
    if entry[:2] == ["agent_comms.providers.claude", "INFO"] and TOLD.fullmatch(entry[2]):
        removed.append(("log", entry[2]))
        return True
    return False
def _parity(tmp_path: Path) -> dict:
    """Three runs with no steer, each on a hub and node of its own, recording every report body the node sends (its
    wall-clock `seq` zeroed), in order, and what the sessions show after (review round 3, the parity rule):
    - a parent cancels its child's queued task while the node's watchdog has just stopped the child's turn in flight
      (R3-07's case: no cancel held here, the watchdog's end not reported yet);
    - a shutdown with several ordinary sessions, two with a turn in flight and one idle;
    - a flush under way when shutdown begins (the loop's own flush, then shutdown's)."""
    from agent_comms import hosted_spawn as hs
    from agent_comms.session_host import HostEvent
    from test_hosted_sessions import P2, P3, session_env, show, start, tick
    from test_hosted_tree import TREE, answer, tree_config

    def recorded(name):
        env = session_env(tmp_path / f"parity-{name}", dispatch=TREE, config=tree_config)
        bodies: list = []
        node_client = env.node.sessions.client
        real = node_client.request

        def recording(method, url, *args, **kw):
            if method == "POST" and str(url).startswith("/api/nodes/sessions/"):
                body = json.loads(json.dumps(kw.get("json")))
                if isinstance(body, dict) and "seq" in body:
                    body["seq"] = 0
                bodies.append([str(url), body])
            return real(method, url, *args, **kw)
        node_client.request = recording
        return env, bodies

    def bound(env, sid):
        hs.ensure_secret(env.board)
        return hs.Ident(hosted=sid, key=hs.bind_key(env.board.conn, sid), claimed=True)

    out: dict = {}
    env, bodies = recorded("cancel")
    root = start(env, "first")["session_id"]
    tick(env)
    answer(env, root, "root reply")
    project = show(env, root)["project"]
    env.human.post("/api/spawn-grants", json={"project": project, "agents": ["claude"], "access": "workspace",
                                              "hours": 1})
    kid = hs.spawn(env.board, env.p["claude"], bound(env, root), task="the task", agent="claude")["child"]["id"]
    tick(env)
    hs.send(env.board, env.p["claude"], bound(env, root), kid, message="a task behind it")
    runner = env.node.sessions
    env.clock.advance(runner.self_stop_seconds + 1)
    runner.enforce_deadlines()          # the watchdog stops the child's turn: its end waits for the loop
    hs.cancel(env.board, env.p["claude"], bound(env, root), kid)
    tick(env, times=3)
    out["cancel"] = {"bodies": bodies, "kid": show(env, kid)["messages"]}

    env, bodies = recorded("shutdown")
    a = start(env, "one")["session_id"]
    b = start(env, "two", project=P2)["session_id"]
    c = start(env, "three", project=P3)["session_id"]
    tick(env)
    answer(env, c, "three's reply")
    runner = env.node.sessions
    runner.signal()
    runner.kill()
    runner.shutdown()
    out["shutdown"] = {"bodies": bodies, "sessions": [show(env, x)["messages"] for x in (a, b, c)]}

    env, bodies = recorded("overlap")
    a = start(env, "one")["session_id"]
    b = start(env, "two", project=P2)["session_id"]
    tick(env)
    runner = env.node.sessions
    real = runner.client.request

    def stops_after_first(method, url, *args, **kw):
        if method == "POST" and str(url).startswith("/api/nodes/sessions/"):
            runner.stopping.set()           # shutdown begins while this report is on its way
        return real(method, url, *args, **kw)
    runner.client.request = stops_after_first
    runner._emit([HostEvent(a, "turn_ended", "1", ok=True, text="a's end"),
                  HostEvent(b, "turn_ended", "1", ok=True, text="b's end")])
    runner._flush()
    left = [[x, e] for x, e in runner.pending]
    runner.client.request = real
    runner.shutdown()
    out["overlap"] = {"bodies": bodies, "left_after_the_loops_flush": left,
                      "sessions": [show(env, x)["messages"] for x in (a, b)]}
    return out


# ------------------------------------------------------------ mid-turn steering, step 2 (the design's K)

NAMED: list = []        # what the last run with `named` took out: (kind, where)
CLIENT_ID = re.compile(r', "clientUserMessageId": "(' + r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
                       + r')"\}\}$')
CLIENT_IDS: set = set()
STARTS: dict = {}       # (round 6, R6-05) a stripped id -> (its turn/start's threadId, that request's id)


def _codex_line(line: str) -> str:
    """A Codex `turn/start` line without its `clientUserMessageId`, only when it is exactly that line of the adapter:
    the key last in its params, a fresh uuid4, the rest a turn/start of main's shape."""
    m = CLIENT_ID.search(line)
    if not m or m.group(1) in CLIENT_IDS:
        return line
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    params = ev.get("params") if isinstance(ev, dict) else None
    if (not isinstance(params, dict) or ev.get("method") != "turn/start" or list(ev) != ["id", "method", "params"]
            or list(params)[-1] != "clientUserMessageId" or params["clientUserMessageId"] != m.group(1)):
        return line
    stripped = line[:m.start()] + "}}"
    if json.loads(stripped) != {**ev, "params": {k: v for k, v in params.items() if k != "clientUserMessageId"}}:
        return line
    CLIENT_IDS.add(m.group(1))
    STARTS[m.group(1)] = (params.get("threadId"), ev.get("id"))
    NAMED.append(("client_id", m.group(1)))
    return stripped


def _events_named(passes: list, where: str) -> None:
    for got in passes:
        for e in got:
            if isinstance(e, dict) and "steer" in e and e["steer"] is None and list(e)[-1] == "steer":
                del e["steer"]
                NAMED.append(("host_event", where))


def cx_thread() -> str:
    import test_providers_codex as cx
    return cx.THREAD


def cx_turn() -> str:
    import test_providers_codex as cx
    return cx.TURN


ITEM = re.compile(r'"clientId": "([0-9a-f-]{36})"')


def _turns_answered(lines: list) -> dict:
    """In one transcript: each request id -> the turn ids its answers named (`result.turn.id`)."""
    out: dict = {}
    for x in lines:
        try:
            ev = json.loads(x)
        except ValueError:
            continue
        result = ev.get("result") if isinstance(ev, dict) else None
        turn = result.get("turn") if isinstance(result, dict) else None
        if isinstance(turn, dict) and isinstance(turn.get("id"), str) and "id" in ev:
            out.setdefault(ev["id"], set()).add(turn["id"])
    return out


def _item_echo(line: str, answered: dict | None = None) -> str:
    """Codex's echo of a stripped `clientUserMessageId` in a userMessage item (K: "echoed as the userMessage item's
    clientId"), back to the null main's run has, only for an id `_codex_line` took and only in such an item.
    (Round 6, R6-05:) only in the item's own `clientId` field, and only for the start it answers in this transcript:
    the item's thread is that start's, and its turn is the one this transcript's answer to that start named
    (`answered`), so two starts' ids swapped are not taken."""
    m = ITEM.search(line)
    if not m or m.group(1) not in CLIENT_IDS:
        return line
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    params = ev.get("params") if isinstance(ev, dict) else None
    item = params.get("item") if isinstance(params, dict) else None
    if not isinstance(item, dict) or ev.get("method") not in ("item/started", "item/completed") or item.get("type") != "userMessage":
        return line
    if item.get("clientId") != m.group(1):
        return line
    thread, rid = STARTS.get(m.group(1), (None, None))
    if params.get("threadId") != thread or params.get("turnId") not in (answered or {}).get(rid, ()):
        return line
    nulled = line[:m.start()] + '"clientId": null' + line[m.end():]
    if json.loads(nulled) != {**ev, "params": {**params, "item": {**item, "clientId": None}}}:
        return line
    NAMED.append(("client_id_echo", m.group(1)))
    return nulled


def _modes_named(body, where: str) -> None:
    """A report body's `modes` with `steer` first (a Codex session to a hub that steers: K)."""
    if isinstance(body, dict) and isinstance(body.get("modes"), list) and body["modes"][:1] == ["steer"]:
        body["modes"] = body["modes"][1:]
        NAMED.append(("report_modes", where))


def _codex_sessions(out: dict) -> set:
    """The session ids the run recorded as Codex sessions (`host.session.<id>` with runtime codex-cli): K's `modes`
    allowance is for those only (R2-11), so a Claude report that names `steer` is never hidden."""
    sids = set()
    for key in ("records", "seq_records"):
        for k, rec in (out.get(key) or {}).items():
            if k.startswith("host.session.") and isinstance(rec, dict) and rec.get("runtime") == "codex-cli":
                sids.add(k[len("host.session."):])
    return sids


def step2_named(out: dict) -> None:
    """Take K's named changes out of the raw run, in place (module text), each recorded in NAMED."""
    CLIENT_IDS.clear()
    STARTS.clear()
    codex = _codex_sessions(out)
    out["stdin"] = [[_codex_line(x) for x in lines] for lines in out.get("stdin", [])]
    out["transcripts"] = {k: [_item_echo(x, _turns_answered(lines)) for x in lines]
                          for k, lines in (out.get("transcripts") or {}).items()}
    for url, body in out.get("node_bodies") or []:
        if url == "/api/nodes/hello" and isinstance((body or {}).get("session_host"), dict) \
                and body["session_host"].get("tree") == 5:
            body["session_host"]["tree"] = 4        # K: `tree` 5 on fetch and hello
            NAMED.append(("hello_tree", url))
        elif url.startswith("/api/nodes/sessions/") and url[len("/api/nodes/sessions/"):] in codex:
            _modes_named(body, url)     # a recorded Codex session's report only (R2-11)
    for name, case in (out.get("host_cases") or {}).items():
        case["stdin"] = [[_codex_line(x) for x in lines] for lines in case["stdin"]]
        _events_named(case["passes"], name)
    _events_named(out.get("host_events") or [], "host_events")
    for key in ("listing", "seq_listing", "listing_p5"):
        features = (out.get(key) or {}).get("features")
        if isinstance(features, list) and features[-1:] == ["steer"]:
            features.pop()
            NAMED.append(("features", key))
    for key in ("records", "seq_records"):
        for k, rec in (out.get(key) or {}).items():
            if (k.startswith("host.session.") and isinstance(rec, dict) and rec.get("runtime") == "codex-cli"
                    and isinstance(rec.get("modes"), list) and rec["modes"][:1] == ["steer"]):
                rec["modes"] = rec["modes"][1:]
                NAMED.append(("modes", k))


# ------------------------------------------------------------ Claude turn correlation (DESIGN_NOTES, 2026-11-24)

CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]     # what Claude Code 2.1.330's `init` lists, among others
OTHER_VERSION = "2.1.329"   # what the sequences' processes report: frames shaped as 2.1.330 writes them
NOTIFY = {"kind": "task-notification", "producer": "session-task"}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
# The allowance, on raw bytes (`allowed`): the uuid key a user line carries, last ...
LINE_UUID = re.compile(r', "uuid": "' + UUID + r'"\}$')
# ... the echo fields a frame carries for it, last ...
ECHO = re.compile(r', "user_message_uuid": "' + UUID + r'"|, "user_message_uuids": \[(?:"' + UUID + r'"(?:, )?)+\]')
# ... and the lifecycle rows Claude writes for it.
LIFECYCLE = '{"type": "command_lifecycle", '


STATES = ("queued", "started", "completed", "cancelled")
# The Claude envelopes that carry echo fields for a line, and which ones (as 2.1.330 writes them).
ECHOING = {("system", "init"): ("user_message_uuids",), ("assistant", None): ("user_message_uuid", "user_message_uuids"),
           ("system", "thinking_tokens"): ("user_message_uuid",),
           ("result", None): ("user_message_uuid", "user_message_uuids")}
SEEN: set = set()       # every line uuid the allowance took, in this run (each must be fresh)


def allowed(kind: str, line: str, removed: list, expected: str | None = None) -> str | None:
    """A raw line with the allowance taken out (`removed` keeps every fragment taken), or None: a whole row taken.
    Only from a valid Claude envelope of a kind that carries it, and only for the right line: a user line
    `{"type": "user", "message": {"role": "user", "content": <str>}, "uuid": <fresh uuid4>}` (its uuid, last); a
    lifecycle row of exactly its four keys naming `expected` (the uuid of the line the frame was written for, in its
    process and turn); an init, assistant, thinking or result frame whose echo fields are exactly [`expected`]. Any
    other line, however similar, is kept as it is, so it differs from main's."""
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    if not isinstance(ev, dict):
        return line
    if kind == "stdin":
        m = LINE_UUID.search(line)
        u = ev.get("uuid")
        if (m and list(ev) == ["type", "message", "uuid"] and ev["type"] == "user" and u not in SEEN
                and ev["message"] == {"role": "user", "content": ev["message"].get("content")}
                and isinstance(ev["message"].get("content"), str) and m.group(0) == f', "uuid": "{u}"}}'):
            SEEN.add(u)
            removed.append(("stdin", u))
            return line[:m.start()] + "}"
        return line
    if expected is None:
        return line
    if ev.get("type") == "command_lifecycle":
        # the native envelope (2.1.330, the probe runs): its five keys in this order, its own frame uuid
        if (list(ev) == ["type", "command_uuid", "state", "uuid", "session_id"] and ev["command_uuid"] == expected
                and ev["state"] in STATES and isinstance(ev["session_id"], str)
                and re.fullmatch(UUID, ev["uuid"] or "") and ev["uuid"] != expected):
            removed.append(("lifecycle", expected))
            return None
        return line
    fields = ECHOING.get((ev.get("type"), ev.get("subtype") if ev.get("type") == "system" else None))
    if not fields or not set(fields) <= set(ev) or list(ev)[-len(fields):] != list(fields):
        return line
    want = {"user_message_uuid": expected, "user_message_uuids": [expected]}
    if any(ev[f] != want[f] for f in fields):
        return line
    stripped = ECHO.sub("", line)
    if json.loads(stripped) != {k: v for k, v in ev.items() if k not in fields}:
        return line
    removed.extend(("echo", expected) for _ in fields)
    return stripped


REMOVED: list = []      # what the last run with `allow` took out (for the test that the allowance is only that)


def _sequences(env, out: dict, allow: bool) -> None:
    """Every bracket sequence of the steering design's C (its checked list, branch claude/steer-loose) and rule 10's
    error cases through the hub and the node, with frames as Claude Code 2.1.330 writes them; then a timeout, a launch
    refusal, a resumed process, a restart, an `auto` send and a Codex turn. Recorded: what the hub shows after each
    step, the records, every process's raw stdin and every raw transcript. A frame carries echo fields, and a turn
    lifecycle rows, only for a line that had a uuid (measured): main's lines have none."""
    from test_hosted_sessions import P2, P3, codex_handshake, codex_turn, show, start, tick
    from test_hosted_tree import as_started, began, proc_of
    from test_session_host import result
    REMOVED.clear()
    SEEN.clear()
    EMITTED.clear()

    def line_id(proc):
        users = [json.loads(x) for x in proc.stdin.split(b"\n") if x and json.loads(x).get("type") == "user"]
        return users[-1].get("uuid") if users else None

    def u_of(sid):
        return line_id(proc_of(env, sid)[1])

    def echo(sid, list_too=True, u=None):
        u = u or u_of(sid)
        return ({"user_message_uuid": u} if not list_too else {"user_message_uuid": u, "user_message_uuids": [u]}
                ) if u else {}

    frames = iter(range(1, 10 ** 6))

    def life(state, u=None):
        """A lifecycle row as 2.1.330 writes it (its own frame uuid, made here: never from uuid4, which the
        recording makes deterministic for the board's ids). `u`: the line it is for, when it is not the latest."""
        def frame(sid):
            line = u or u_of(sid)
            if not line:
                return None
            k = next(frames)
            ev = {"type": "command_lifecycle", "command_uuid": line, "state": state,
                  "uuid": "%08x-0000-4000-8000-%012x" % (k, k), "session_id": sid}
            return ev, line
        return frame

    def init(sid, echo_=True):
        call, proc = proc_of(env, sid)
        # Another version than the one VERIFIED (2.1.330 since the switch-on), so that the production setting is the
        # case-2 baseline: a process reporting any version the adapter does not verify.
        ev = began(sid, *as_started(call["argv"])) | {"claude_code_version": OTHER_VERSION, "capabilities": CAPS}
        u = line_id(proc)
        return ev | ({"user_message_uuids": [u]} if echo_ and u else {})

    def thinking(sid):
        return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50, "session_id": sid} | echo(
            sid, list_too=False)

    def assistant(text):
        return lambda sid: {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}, "parent_tool_use_id": None, "session_id": sid} | echo(sid)

    def res(sid, text, echo_=True, u=None, **kw):
        return result(sid, text=text, **kw) | (echo(sid, u=u) if echo_ else {})

    def turn(text, *middle, end=None):
        """A whole turn as 2.1.330 writes it: lifecycle queued and started, init, thinking, the reply, (middle),
        result, lifecycle completed."""
        return [life("queued"), life("started"), init, thinking, assistant(text), *middle,
                end or (lambda s: res(s, text)), life("completed")]

    def foreign(sid, error=False):
        return result(sid, text="background: FINISHED", subtype="error_during_execution" if error else "success",
                      is_error=error) | {"origin": NOTIFY}

    def note(sid, error=False):
        return result(sid, text="", subtype="error_during_execution" if error else "success", is_error=error) | {
            "num_turns": 0, "origin": NOTIFY}

    def send(sid, text, **body):
        r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": text, **body})
        return r.json()

    seq: dict = {}
    sid = start(env, "seq owned pair", project=P2)["session_id"]
    tick(env)

    def step(name, *frames):
        proc = proc_of(env, sid)[1]
        for f in frames:
            ev = f(sid) if callable(f) else f
            ev, line = ev if isinstance(ev, tuple) else (ev, line_id(proc))     # (the frame, the line it is for)
            if ev is not None:      # (a lifecycle row exists only for a line with a uuid: the pass is taken anyway)
                proc.emit(ev)
                EMITTED.setdefault(sid, []).append((json.dumps(ev), line))
            tick(env)
        seq[name] = show(env, sid)

    def next_message(text, **body):
        got = send(sid, text, **body)
        tick(env)
        return got

    # First the sequences that keep a verified process verified, then those that end its verification (rule 10, a
    # plain result where the table falls back to order): with a version VERIFIED each step then shows its own rule.
    step("owned_pair", *turn("pair"))
    next_message("seq notification inside")
    step("notification_inside", *turn("after", note))
    next_message("seq reinit")
    step("reinit", *turn("reinit", {"type": "system", "subtype": "compact_boundary", "session_id": sid},
                         lambda s: init(s, echo_=False)))
    next_message("seq unattributed first")
    step("unattributed_first", lambda s: init(s, echo_=False), foreign, *turn("own"))
    next_message("seq no init")
    step("no_init_owned", lambda s: res(s, "no init"))
    step("notification_idle", note, lambda s: note(s, True), lambda s: foreign(s, error=True))
    next_message("seq restart me")
    step("restart_begun", life("queued"), life("started"), init)
    seq["restart_send"] = send(sid, "seq the restart", mode="restart")
    tick(env)
    proc = proc_of(env, sid)[1]
    [req] = [json.loads(x) for x in proc.stdin.split(b"\n") if x and json.loads(x).get("type") == "control_request"]
    interrupted = line_id(proc)     # captured BEFORE the abort: its passes may write the replacement's line
    step("restart_aborted", {"type": "control_response", "response": {
        "subtype": "success", "request_id": req["request_id"], "response": {"still_queued": []}}},
        lambda s: (res(s, None, u=interrupted, subtype="error_during_execution", is_error=True)
                   | {"terminal_reason": "aborted_tools"}, interrupted),
        life("cancelled", u=interrupted))
    step("restart_message", *turn("replaced"))
    seq["auto_send"] = send(sid, "seq auto", mode="auto")
    tick(env)
    step("auto", *turn("auto"))
    next_message("seq unattributed error")
    step("unattributed_error", lambda s: init(s, echo_=False), lambda s: foreign(s, error=True), *turn("own"))
    next_message("seq plain failure")
    step("plain_failure", life("queued"), life("started"), init,
         lambda s: res(s, "broke", echo_=False, subtype="error_during_execution", is_error=True))
    next_message("seq notification error inside")
    step("notification_error_inside", *turn("after", lambda s: note(s, True)))
    next_message("seq foreign error inside")
    step("foreign_error_inside", init, lambda s: foreign(s, error=True), lambda s: res(s, "late"))
    next_message("seq lost echoes")
    step("lost_echoes", lambda s: init(s, echo_=False), lambda s: res(s, "whose", echo_=False))
    next_message("seq no init plain")
    step("no_init_plain", lambda s: res(s, "no init", echo_=False))
    next_message("seq timeout")
    step("timeout_begun", life("queued"), life("started"), init)
    env.clock.advance(30 * 60 + 1)
    env.node.hello()            # the node is connected still (its hello renews it at the hub)
    tick(env, times=3)
    seq["timeout"] = show(env, sid)
    next_message("seq refused start")
    proc = proc_of(env, sid)[1]
    proc.emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
               "session_id": sid, "permission_denials": [],
               "errors": [f"No conversation found with session ID: {sid}"]})
    proc.code = 1
    tick(env, times=3)
    seq["launch_refusal"] = show(env, sid)
    next_message("seq resumed")
    step("resumed", *turn("resumed"))
    next_message("seq resumed again")
    step("resumed_again", *turn("again"))
    # A Codex root: its attribution and wire are not changed (its stdin is recorded with the rest).
    cid = start(env, "seq codex", project=P3, agent="codex")["session_id"]
    tick(env)
    child = env.node.session_spawner.children[-1]
    codex_handshake(env, child)
    # Codex's echo of the turn's message id (its userMessage item's `clientId`), as 0.171.0 writes it: the id the
    # turn/start carried, or null for one that carried none (main's).
    start_line = [json.loads(x) for x in child.stdin.split(b"\n") if x and b'"turn/start"' in x][-1]
    child.emit({"method": "item/completed", "params": {"threadId": cx_thread(), "turnId": cx_turn(), "item": {
        "type": "userMessage", "id": "item-golden", "clientId": start_line["params"].get("clientUserMessageId"),
        "content": [{"type": "text", "text": "seq codex"}]}}})
    codex_turn(env, child, "codex answer")
    seq["codex"] = show(env, cid)
    out["seq"] = seq
    ids = {sid, cid}
    out["seq_records"] = {k: json.loads(v) for k, v in env.board.conn.execute(
        "SELECT key, value FROM board_state WHERE key LIKE 'host.session.%' OR key LIKE 'host.msg.%' ORDER BY key")
        if any(i in k for i in ids)}
    out["seq_listing"] = env.laptop.get("/api/nodes/sessions", params={"tree": 4}).json()
    out["stdin"] = [raw_lines("stdin", proc.stdin, allow) for proc in env.node.session_spawner.children]
    sessions = env.node.log_dir / "sessions"
    out["transcripts"] = {p.name: raw_lines("transcript", p.read_bytes(), allow, EMITTED.get(p.name[:-6], []))
                          for p in sorted(sessions.glob("*.jsonl"))}


EMITTED: dict = {}      # session id -> [(the frame as emitted, the uuid of the line it was written for)], in order


def raw_lines(kind: str, data: bytes, allow: bool, emitted=()) -> list:
    """Raw lines, the allowance taken out when `allow`. A transcript line is paired with the frame the run emitted
    for it (the same bytes, in order), which says which line, of which process and turn, it may echo."""
    lines = data.decode().split("\n")
    if not allow:
        return lines
    out, queue = [], list(emitted)
    for line in lines:
        expected = None
        # the next emitted frame with these very bytes (a frame a stopped process never had read is skipped)
        j = next((i for i, (sent, _) in enumerate(queue) if sent == line), None)
        if j is not None:
            expected, queue = queue[j][1], queue[j + 1:]
        kept = allowed(kind, line, REMOVED, expected)
        if kept is not None:
            out.append(kept)
    return out


def _host_cases(tmp_path: Path, out: dict, allow: bool) -> None:
    """Host-level cases, pass by pass, through a SessionHost of fake processes and a fake clock (as
    tests/test_session_host.py drives one): Codex's own turn with a replacement waiter and with a changed binding
    (main's `_held`, kept as it is: a cancelled waiter's clock is inherited, a changed binding has no deadline), and
    Claude's tick boundaries: a pipe that takes nothing, a partly written line, a write timer running out, a first
    line staged into a pipe that never takes it (with an init that does not qualify, or none). Recorded:
    every pass's events, and each process's raw stdin."""
    import dataclasses
    from types import SimpleNamespace

    from agent_comms.dispatch import DispatchConfig
    from conftest import PROJECT, FakeClock
    import test_providers_codex as cx
    from test_dispatch import FakeProcs, FakeSpawner
    from test_session_host import SHIPPED, SID, FakeSessionChild, codex_host, codex_ready, init, make_host, result

    def env_for(name):
        work = tmp_path / f"host-{name}"
        work.mkdir()
        henv = SimpleNamespace(clock=FakeClock(), work=str(work), log_dir=tmp_path / f"host-{name}-logs", signals=[],
                               groups=set())
        henv.config = DispatchConfig.from_dict({"runners": {"claude-code": SHIPPED},
                                                "worktrees": {PROJECT: str(work)}, "timeout_minutes": 30,
                                                "kill_grace_seconds": 10})
        henv.spawner = FakeSpawner(child=FakeSessionChild)
        henv.procs = FakeProcs(henv.spawner)
        return henv

    cases: dict = {}

    def run(name, host, henv, script):
        passes = []
        for action in script:
            action()
            passes.append([dataclasses.asdict(e) for e in host.tick()])
        cases[name] = {"passes": passes,
                       "stdin": [raw_lines("stdin", c.stdin, allow) for c in henv.spawner.children]}

    def codex_busy(name):
        henv = env_for(name)
        host, sess = codex_host(henv)
        host.send(SID, "first", message_id="a")
        host.tick()
        child = henv.spawner.children[-1]
        codex_ready(host, child)
        host.tick()
        child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("one"), cx.completed())
        host.tick()
        child.emit(cx.own_turn("turn/started"))
        return henv, host, sess, child

    henv, host, sess, child = codex_busy("codex-replacement")
    run("codex_replacement_waiter", host, henv, [
        lambda: host.send(SID, "waits", message_id="b"), lambda: henv.clock.advance(29 * 60),
        lambda: host.cancel(SID, "b", "cancelled"), lambda: host.send(SID, "next", message_id="c"),
        lambda: henv.clock.advance(2 * 60), lambda: henv.clock.advance(30 * 60)])
    henv, host, sess, child = codex_busy("codex-binding")
    run("codex_changed_binding", host, henv, [
        lambda: setattr(sess, "bind", "a" * 64), lambda: host.send(SID, "waits", message_id="b"),
        lambda: henv.clock.advance(31 * 60), lambda: henv.clock.advance(31 * 60),
        lambda: child.emit(cx.own_turn("turn/completed", status="completed"))])

    def claude(name, child=FakeSessionChild):
        henv = env_for(name)
        henv.spawner = FakeSpawner(child=child)
        henv.procs = FakeProcs(henv.spawner)
        host = make_host(henv)
        host.open(SID, agent="claude", runtime="claude-code", project=PROJECT)
        return henv, host

    def taking(n):
        class Child(FakeSessionChild):
            def __init__(self, pid, exits_on_terminate=True):
                super().__init__(pid, exits_on_terminate)
                self.accept = n        # most bytes one write takes, from the start
        return Child

    henv, host = claude("claude-zero-byte", taking(0))
    kids = henv.spawner.children
    run("claude_zero_byte_write", host, henv, [
        lambda: host.send(SID, "next", message_id="m"), lambda: kids[-1].emit(init()),
        lambda: setattr(kids[-1], "accept", None), lambda: kids[-1].emit(result(text="its own")),
        lambda: henv.clock.advance(5)])
    henv, host = claude("claude-partial", taking(7))
    kids = henv.spawner.children
    run("claude_partial_line", host, henv, [
        lambda: host.send(SID, "a longer line", message_id="m"), lambda: kids[-1].emit(init()),
        lambda: setattr(kids[-1], "accept", None), lambda: kids[-1].emit(result(text="done"))])
    henv, host = claude("claude-write-timer")
    kids = henv.spawner.children
    run("claude_write_timer", host, henv, [
        lambda: host.send(SID, "first", message_id="m"), lambda: kids[-1].emit(init(), result()),
        lambda: (setattr(kids[-1], "accept", 0), host.send(SID, "stuck", message_id="n")),
        lambda: kids[-1].emit(init()), lambda: henv.clock.advance(30 * 60), lambda: None])
    # A process whose pipe takes nothing from its first line on (staged before any init: while some version is
    # VERIFIED, only a candidate): its init does not qualify (it reports none), then frames that would hold a verified
    # process; or it never writes an init at all. Main's write timer fails the message and stops the process; nothing
    # is restarted or requeued (review round 7, C7-01).
    henv, host = claude("claude-blocked-unqualified", taking(0))
    kids = henv.spawner.children
    run("claude_blocked_first_line_unqualified", host, henv, [
        lambda: host.send(SID, "next", message_id="m"), lambda: kids[-1].emit(init()),
        lambda: (henv.clock.advance(5 * 60), kids[-1].emit(result(text="its own"), init())),
        lambda: henv.clock.advance(25 * 60), lambda: None])
    henv, host = claude("claude-blocked-no-init", taking(0))
    kids = henv.spawner.children
    run("claude_blocked_first_line_no_init", host, henv, [
        lambda: host.send(SID, "next", message_id="m"), lambda: henv.clock.advance(10 * 60),
        lambda: henv.clock.advance(20 * 60), lambda: None])
    out["host_cases"] = cases

def normalized(value, tmp: Path) -> dict:
    """The run's temporary directory and time stamps out (both differ by run), everything else as it is."""
    if isinstance(value, dict) and "zz_parity" in value:
        # Round 3's parity runs are normalized on their own, so the placeholders of everything else are what they
        # were before they were added.
        rest = normalized({k: v for k, v in value.items() if k != "zz_parity"}, tmp)
        return rest | {"zz_parity": normalized(value["zz_parity"], tmp)}
    text = json.dumps(value, sort_keys=True)
    for t in sorted({str(tmp), str(tmp.resolve())}, key=len, reverse=True):
        text = text.replace(t, "<tmp>")
    text = re.sub(r"\"(created_at|updated_at|delivered_at|ended_at|settled_at|at|node_reported_at|expires_at|"
                  r"granted_at|since|last_at|shown_at|started_at|seen_at|reserved_at|lease_until|now|status_seq)\": [0-9.e+]+",
                  r'"\1": 0', text)
    # Only what an earlier test in the same process can shift (it consumes the deterministic counter): session ids,
    # the frame and result tokens INSIDE their marker forms, and the hash fields the listing names. Each is renamed
    # by order of first appearance to a placeholder of the SAME width, so a change of a marker's width, or of any
    # other hex in a reply or a frame, still shows (R4-05). Nothing else is touched.
    names: dict[str, str] = {}

    def same_width(value: str, kind: str) -> str:
        if value not in names:
            i = len(names) + 1
            names[value] = (f"00000000-0000-0000-0000-{i:012d}" if kind == "uuid" else f"{i:0{len(value)}d}")
        return names[value]

    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  lambda m: same_width(m.group(0), "uuid"), text)
    text = re.sub(r"<<<(task|end task|child-output|end child-output) ([0-9a-f]{16})>>>",
                  lambda m: f"<<<{m.group(1)} {same_width(m.group(2), 'token')}>>>", text)
    text = re.sub(r"\"(version|bind)\": \"([0-9a-f]{16,64})\"",
                  lambda m: f"\"{m.group(1)}\": \"{same_width(m.group(2), 'hash')}\"", text)
    # A send receipt's key hashes the sender, whose session id is one of the ids above.
    text = re.sub(r"\"([0-9a-f]{32})\": \{\"at\": ", lambda m: f"\"{same_width(m.group(1), 'hash')}\": {{\"at\": ",
                  text)
    return json.loads(text)


if __name__ == "__main__":
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    with tempfile.TemporaryDirectory() as d:
        result = record(Path(d))
    Path(sys.argv[1]).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
