"""A schema-free run of hosted sessions, recorded: every frame, result turn, claim, tool answer, record, listing and
CLI text it produces, normalized (ids and tokens made deterministic). Structured results promise all of it byte for
byte as on main when no schema is given, and Claude turn correlation promises the same with no steer sent, but for its
named changes (the uuid key on Claude user lines, the echo fields on the frames a process writes: `_sequences`
takes exactly those out): tests/fixtures/schema_free_main.json was recorded by THIS module run against main's code
(re-pinned to d4b92e5 for turn correlation; first recorded at a8b90cc), and test_structured_results compares this
checkout's run with it. Only APIs main has are used.

    PYTHONPATH=<main's tree>:<main's tree>/tests python tests/golden_schema_free.py OUT.json    # (re)record
"""
import itertools
import json
import re
import secrets
import sys
import uuid
from pathlib import Path


def record(tmp_path: Path) -> dict:
    counter = itertools.count(1)
    real_hex, real_uuid4 = secrets.token_hex, uuid.uuid4
    secrets.token_hex = lambda n=32: format(next(counter), "x").rjust(2 * n, "0")[-2 * n:]
    uuid.uuid4 = lambda: uuid.UUID(int=next(counter))
    try:
        return _run(tmp_path)
    finally:
        secrets.token_hex, uuid.uuid4 = real_hex, real_uuid4


def _run(tmp_path: Path) -> dict:
    from agent_comms import cli, hosted, hosted_results, hosted_spawn as hs
    from test_hosted_sessions import session_env, show, start, tick, written
    from test_hosted_tree import TREE, answer, child, tree_config

    env = session_env(tmp_path, dispatch=TREE, config=tree_config)
    out: dict = {}

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
    _sequences(env, out)
    return normalized(out, tmp_path)


# ------------------------------------------------------------ Claude turn correlation (DESIGN_NOTES, 2026-11-24)

CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]     # what Claude Code 2.1.330's `init` lists, among others
NOTIFY = {"kind": "task-notification", "producer": "session-task"}
# The one named change on the wire with no steer sent: every user line Claude gets carries a `uuid` key, last.
LINE_UUID = re.compile(r', "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"\}$')
# ... and on the transcript, the echo fields a process adds to its existing `init` and `result` frames.
ECHO_FIELDS = ("user_message_uuid", "user_message_uuids")


def _sequences(env, out: dict) -> None:
    """Every bracket sequence of the steering design's C (its checked list, branch claude/steer-loose) through the
    hub and the node, with frames as Claude Code 2.1.330 writes them (a version that is not VERIFIED: VERIFIED ships
    empty), echo fields included whenever the line written had a uuid to echo; then a timeout, a launch refusal, a
    resumed process, a restart, an `auto` send and a Codex turn. Recorded: what the hub shows after each step, the
    records, every process's stdin (the uuid key taken out of user lines: the one wire change) and every transcript
    (the echo fields taken out: the one transcript change). Main's code (d4b92e5) writes no uuid, so its processes
    echo nothing; this checkout's echo, and are still attributed by order: everything else must be main's."""
    from test_hosted_sessions import P2, P3, codex_handshake, codex_turn, show, start, tick
    from test_hosted_tree import as_started, began, proc_of
    from test_session_host import result

    def line_id(proc):
        users = [json.loads(x) for x in proc.stdin.split(b"\n") if x and json.loads(x).get("type") == "user"]
        return users[-1].get("uuid") if users else None

    def init(sid, echo=True):
        call, proc = proc_of(env, sid)
        ev = began(sid, *as_started(call["argv"])) | {"claude_code_version": "2.1.330", "capabilities": CAPS}
        u = line_id(proc)
        return ev | ({"user_message_uuids": [u]} if echo and u else {})

    def res(sid, text, echo=True, **kw):
        u = line_id(proc_of(env, sid)[1])
        return result(sid, text=text, **kw) | ({"user_message_uuid": u, "user_message_uuids": [u]} if echo and u
                                               else {})

    def foreign(sid):
        return result(sid, text="background: FINISHED") | {"origin": NOTIFY}

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
            proc.emit(f(sid) if callable(f) else f)
            tick(env)
        seq[name] = show(env, sid)

    step("owned_pair", lambda s: init(s), lambda s: res(s, "pair"))
    send(sid, "seq plain failure")
    tick(env)
    step("plain_failure", lambda s: init(s), lambda s: res(s, "broke", echo=False, subtype="error_during_execution",
                                                           is_error=True))
    for error in (False, True):
        send(sid, f"seq notification inside {error}")
        tick(env)
        step(f"notification_inside_{error}", lambda s: init(s), lambda s: note(s, error), lambda s: res(s, "after"))
    send(sid, "seq reinit")
    tick(env)
    step("reinit", lambda s: init(s), lambda s: init(s, echo=False), lambda s: res(s, "reinit"))
    send(sid, "seq unattributed first")
    tick(env)
    step("unattributed_first", lambda s: init(s, echo=False), foreign, lambda s: init(s), lambda s: res(s, "own"))
    send(sid, "seq lost echoes")
    tick(env)
    step("lost_echoes", lambda s: init(s, echo=False), lambda s: res(s, "whose", echo=False))
    for echo in (True, False):
        send(sid, f"seq no init {echo}")
        tick(env)
        step(f"no_init_{echo}", lambda s, e=echo: res(s, "no init", echo=e))
    step("notification_idle", note, lambda s: note(s, True))
    send(sid, "seq restart me")
    tick(env)
    step("restart_begun", lambda s: init(s))
    seq["restart_send"] = send(sid, "seq the restart", mode="restart")
    tick(env)
    proc = proc_of(env, sid)[1]
    [req] = [json.loads(x) for x in proc.stdin.split(b"\n") if x and json.loads(x).get("type") == "control_request"]
    step("restart_aborted", {"type": "control_response", "response": {
        "subtype": "success", "request_id": req["request_id"], "response": {"still_queued": []}}},
        lambda s: res(s, None, subtype="error_during_execution", is_error=True) | {"terminal_reason": "aborted_tools"})
    step("restart_message", lambda s: init(s), lambda s: res(s, "replaced"))
    seq["auto_send"] = send(sid, "seq auto", mode="auto")
    tick(env)
    step("auto", lambda s: init(s), lambda s: res(s, "auto"))
    send(sid, "seq timeout")
    tick(env)
    step("timeout_begun", lambda s: init(s))
    env.clock.advance(30 * 60 + 1)
    env.node.hello()            # the node is connected still (its hello renews it at the hub)
    tick(env, times=3)
    seq["timeout"] = show(env, sid)
    send(sid, "seq refused start")
    tick(env)
    proc = proc_of(env, sid)[1]
    proc.emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
               "session_id": sid, "permission_denials": [],
               "errors": [f"No conversation found with session ID: {sid}"]})
    proc.code = 1
    tick(env, times=3)
    seq["launch_refusal"] = show(env, sid)
    send(sid, "seq resumed")
    tick(env)
    step("resumed", lambda s: init(s), lambda s: res(s, "resumed"))
    # A Codex root: its attribution and wire are not changed (its stdin is recorded with the rest).
    cid = start(env, "seq codex", project=P3, agent="codex")["session_id"]
    tick(env)
    child = env.node.session_spawner.children[-1]
    codex_handshake(env, child)
    codex_turn(env, child, "codex answer")
    seq["codex"] = show(env, cid)
    out["seq"] = seq
    ids = {sid, cid}
    out["seq_records"] = {k: json.loads(v) for k, v in env.board.conn.execute(
        "SELECT key, value FROM board_state WHERE key LIKE 'host.session.%' OR key LIKE 'host.msg.%' ORDER BY key")
        if any(i in k for i in ids)}
    out["seq_listing"] = env.laptop.get("/api/nodes/sessions", params={"tree": 4}).json()
    out["stdin"] = [[_main_stdin_line(x) for x in proc.stdin.decode().split("\n")]
                    for proc in env.node.session_spawner.children]
    sessions = env.node.log_dir / "sessions"
    out["transcripts"] = {p.name: [_without_echo(x) for x in p.read_text().splitlines()]
                          for p in sorted(sessions.glob("*.jsonl"))}


def _main_stdin_line(line: str) -> str:
    """A line a process got, without the uuid key of a Claude user line (the one named wire change)."""
    return LINE_UUID.sub("}", line) if line.startswith('{"type": "user"') else line


def _without_echo(line: str) -> str:
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    if not isinstance(ev, dict) or not set(ECHO_FIELDS) & set(ev):
        return line
    return json.dumps({k: v for k, v in ev.items() if k not in ECHO_FIELDS})


def normalized(value, tmp: Path) -> dict:
    """The run's temporary directory and time stamps out (both differ by run), everything else as it is."""
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
