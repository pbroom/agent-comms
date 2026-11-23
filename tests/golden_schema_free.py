"""A schema-free run of hosted sessions, recorded: every frame, result turn, claim, tool answer, record, listing and
CLI text it produces, normalized (ids and tokens made deterministic). Structured results promise all of it byte for
byte as on main when no schema is given: tests/fixtures/schema_free_main.json was recorded by THIS module run against
main's code (a8b90cc), and test_structured_results compares this checkout's run with it. Only APIs main has are used.

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
    return normalized(out, tmp_path)


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
