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
- a version VERIFIED that these processes do not report (an explicitly unverified baseline): main's but for the
  allowance `allowed` takes out of the raw bytes, exactly: the `uuid` key on the user lines written before each
  process's first `init`, and the echo fields and lifecycle rows the process wrote for them.

    PYTHONPATH=<main's tree>:<main's tree>/tests python tests/golden_schema_free.py OUT.json    # (re)record
"""
import itertools
import json
import re
import secrets
import sys
import uuid
from pathlib import Path


def record(tmp_path: Path, verified=None, allow: bool = False) -> dict:
    """`verified`: the Claude adapter's VERIFIED for this run (set on main's adapter too, which has no use for it).
    `allow`: take the allowance out of the raw stdin and transcripts before anything is normalized (`allowed`)."""
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
        return _run(tmp_path, logs, allow)
    finally:
        board.removeHandler(keep)
        board.setLevel(level)
        secrets.token_hex, uuid.uuid4 = real_hex, real_uuid4
        if had:
            claude.VERIFIED = before
        elif hasattr(claude, "VERIFIED"):
            del claude.VERIFIED


def _run(tmp_path: Path, logs: list, allow: bool) -> dict:
    import dataclasses
    from agent_comms import cli, hosted, hosted_results, hosted_spawn as hs
    from test_hosted_sessions import session_env, show, start, tick, written
    from test_hosted_tree import TREE, answer, child, tree_config

    env = session_env(tmp_path, dispatch=TREE, config=tree_config)
    out: dict = {}
    events: list = []
    host = env.node.sessions.host
    real_tick = host.tick

    def recorded_tick():
        got = real_tick()
        events.extend(dataclasses.asdict(e) for e in got)
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
    out["host_events"] = events
    out["logs"] = [x for x in logs if not x[2].startswith("HTTP Request:")]
    return normalized(out, tmp_path)


# ------------------------------------------------------------ Claude turn correlation (DESIGN_NOTES, 2026-11-24)

CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]     # what Claude Code 2.1.330's `init` lists, among others
NOTIFY = {"kind": "task-notification", "producer": "session-task"}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
# The allowance, on raw bytes (`allowed`): the uuid key a user line carries, last ...
LINE_UUID = re.compile(r', "uuid": "' + UUID + r'"\}$')
# ... the echo fields a frame carries for it, last ...
ECHO = re.compile(r', "user_message_uuid": "' + UUID + r'"|, "user_message_uuids": \[(?:"' + UUID + r'"(?:, )?)+\]')
# ... and the lifecycle rows Claude writes for it.
LIFECYCLE = '{"type": "command_lifecycle", '


def allowed(kind: str, line: str, removed: list) -> str | None:
    """A raw line with the allowance taken out (`removed` keeps every fragment taken), or None: a whole row taken."""
    if kind == "stdin":
        m = LINE_UUID.search(line) if line.startswith('{"type": "user"') else None
        if m:
            removed.append(("stdin", m.group(0)))
            return line[:m.start()] + "}"
        return line
    if line.startswith(LIFECYCLE):
        removed.append(("lifecycle", line))
        return None
    for m in ECHO.finditer(line):
        removed.append(("echo", m.group(0)))
    return ECHO.sub("", line)


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

    def line_id(proc):
        users = [json.loads(x) for x in proc.stdin.split(b"\n") if x and json.loads(x).get("type") == "user"]
        return users[-1].get("uuid") if users else None

    def u_of(sid):
        return line_id(proc_of(env, sid)[1])

    def echo(sid, list_too=True):
        u = u_of(sid)
        return ({"user_message_uuid": u} if not list_too else {"user_message_uuid": u, "user_message_uuids": [u]}
                ) if u else {}

    def life(state):
        def frame(sid):
            u = u_of(sid)
            return {"type": "command_lifecycle", "command_uuid": u, "state": state, "session_id": sid} if u else None
        return frame

    def init(sid, echo_=True):
        call, proc = proc_of(env, sid)
        ev = began(sid, *as_started(call["argv"])) | {"claude_code_version": "2.1.330", "capabilities": CAPS}
        u = line_id(proc)
        return ev | ({"user_message_uuids": [u]} if echo_ and u else {})

    def thinking(sid):
        return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50, "session_id": sid} | echo(
            sid, list_too=False)

    def assistant(text):
        return lambda sid: {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}, "parent_tool_use_id": None, "session_id": sid} | echo(sid)

    def res(sid, text, echo_=True, **kw):
        return result(sid, text=text, **kw) | (echo(sid) if echo_ else {})

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
            if ev is not None:
                proc.emit(ev)
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
    step("restart_aborted", {"type": "control_response", "response": {
        "subtype": "success", "request_id": req["request_id"], "response": {"still_queued": []}}},
        lambda s: res(s, None, subtype="error_during_execution", is_error=True) | {"terminal_reason": "aborted_tools"},
        life("cancelled"))
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
    codex_turn(env, child, "codex answer")
    seq["codex"] = show(env, cid)
    out["seq"] = seq
    ids = {sid, cid}
    out["seq_records"] = {k: json.loads(v) for k, v in env.board.conn.execute(
        "SELECT key, value FROM board_state WHERE key LIKE 'host.session.%' OR key LIKE 'host.msg.%' ORDER BY key")
        if any(i in k for i in ids)}
    out["seq_listing"] = env.laptop.get("/api/nodes/sessions", params={"tree": 4}).json()
    REMOVED.clear()

    def raw_lines(kind, data: bytes) -> list:
        lines = data.decode().split("\n")
        if allow:
            lines = [x for x in (allowed(kind, line, REMOVED) for line in lines) if x is not None]
        return lines

    out["stdin"] = [raw_lines("stdin", proc.stdin) for proc in env.node.session_spawner.children]
    sessions = env.node.log_dir / "sessions"
    out["transcripts"] = {p.name: raw_lines("transcript", p.read_bytes()) for p in sorted(sessions.glob("*.jsonl"))}

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
