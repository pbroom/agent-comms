"""Ownership C3-2b-1, B: restore and reserve at node start (DESIGN_NOTES "Ownership C3-2b-1").

Before any thread, hub contact or admission of work, the node loads the supervised-launch journal and installs, for
every record, a HELD reservation under its own token kind in the SHARED registry, so that neither a hosted session nor
a dispatched run is admitted into that checkout, whether or not hosting is on. Nothing is released from the journal in
this PR. An unreadable journal closes admission for the whole node (one WARNING; the node stays up and reports). An
empty or absent journal changes nothing. Supervised identities never enter the legacy numeric recovery.

These drive the real `Node` and `Groups` admission paths with the existing node tests' fakes (no process is started)."""
from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path

import pytest

from agent_comms import hostlog, node as node_mod, node_groups, shimjournal
from agent_comms.node import Node
from agent_comms.shimjournal import Journal, Record

from test_nodes import Clock, fake_os, nenv  # noqa: F401 (the node with its fake processes)
from test_dispatch import allow, human_post

BOOT = "154793AE-2D88-4E53-AB60-D82BAD21E683"


def journal_of(log_dir) -> Journal:
    return Journal(Path(log_dir) / shimjournal.JOURNAL_DIR)


def record_for(cwd, **over) -> Record:
    st = os.stat(cwd)
    fields = dict(launch_id=shimjournal.new_launch_id(), checkout_key=(st.st_dev, st.st_ino), cwd=str(cwd), boot=BOOT,
                  marker="/nonexistent/markers/x.marker", shim_pid=5151, shim_start=None)
    fields.update(over)
    return Record(**fields)


def fresh_node(nenv, **kw) -> Node:
    """A node process starting again on the same data directory (as tests/test_nodes.py's restart test builds it)."""
    args = dict(spawner=nenv.node_spawner, clock=Clock(nenv), **fake_os(nenv.node_procs))
    args.update(kw)
    nenv.node = Node(nenv.node.client, nenv.node.config, nenv.node.log_dir, **args)
    return nenv.node


def assign_a_run(nenv) -> None:
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()


# ---------------------------------------------------------------- the reservation fences both kinds of work


def test_a_journalled_checkout_refuses_a_dispatched_run_with_hosting_off(nenv):
    r = record_for(nenv.node_dir)
    journal_of(nenv.node.log_dir).commit(r)
    node = fresh_node(nenv)
    assert node.sessions is None, "hosting is off on this node"
    assert node.groups.state(r.token) == node.groups.HELD and not node.groups.may_signal(r.token)
    assign_a_run(nenv)
    for _ in range(3):
        node.tick()
    assert nenv.node_spawner.calls == [] and node.running == {}, "the run waits: its checkout is reserved"
    assert node.groups.start(nenv.node_dir, ("run", "next"), lambda: object()) == (False, None)
    assert node.groups.state(r.token) == node.groups.HELD, "nothing is released from the journal in this PR"
    assert [x.launch_id for x in journal_of(node.log_dir).load()] == [r.launch_id], "the record stays"


def test_a_journalled_checkout_refuses_a_hosted_session_and_a_run_with_hosting_on(tmp_path):
    from test_hosted_sessions import make_node, session_env, start, tick
    env = session_env(tmp_path)
    log_dir = env.tmp / "laptop-logs"
    r = record_for(env.work)
    journal_of(log_dir).commit(r)
    env.node = node = make_node(env, log_dir=log_dir)
    assert node.sessions is not None and node.groups is node.sessions.host.groups, "ONE registry, shared"
    node.hello()
    start(env, "first")
    tick(env, times=3)
    assert node.session_spawner.calls == [], "the session's process is never started in the reserved checkout"
    assert node.groups.blocked(str(env.work))
    assert node.groups.start(str(env.work), ("run", "r1"), lambda: object()) == (False, None)
    assert node.groups.state(r.token) == node.groups.HELD


def test_an_empty_or_absent_journal_changes_nothing(tmp_path):
    """No journal directory (production today: nothing writes one) and an empty one: the session starts as on main,
    and the node creates nothing for the journal."""
    from test_hosted_sessions import make_node, session_env, start, tick
    env = session_env(tmp_path)
    log_dir = env.tmp / "laptop-logs"
    assert not (log_dir / shimjournal.JOURNAL_DIR).exists()
    before = sorted(os.listdir(log_dir)) if log_dir.exists() else None
    env.node = node = make_node(env, log_dir=log_dir)
    assert (sorted(os.listdir(log_dir)) if log_dir.exists() else None) == before, "nothing is created"
    assert node.groups.entries == {} and node.groups.closed is None
    (log_dir / shimjournal.JOURNAL_DIR).mkdir(mode=0o700, parents=True)
    env.node = node = make_node(env, log_dir=log_dir)
    assert node.groups.entries == {} and node.groups.closed is None
    node.hello()
    start(env, "first")
    tick(env)
    assert len(node.session_spawner.calls) == 1, "started as on main"


def test_an_empty_journal_lets_a_dispatched_run_start_as_on_main(nenv):
    (Path(nenv.node.log_dir) / shimjournal.JOURNAL_DIR).mkdir(mode=0o700, parents=True)
    node = fresh_node(nenv)
    assign_a_run(nenv)
    node.tick()
    assert len(nenv.node_spawner.calls) == 1 and len(node.running) == 1


# ---------------------------------------------------------------- an unreadable journal closes admission


def test_an_unreadable_journal_closes_admission_for_the_whole_node_and_says_so_once(nenv, caplog):
    other = Path(nenv.node_dir).parent / "another-checkout"
    other.mkdir()
    j = journal_of(nenv.node.log_dir)
    j.commit(record_for(nenv.node_dir))
    bad = Path(j.directory) / f"{shimjournal.new_launch_id()}.json"
    bad.write_text("{cut sho")                  # (a partial record: a commit that failed, or damage; never skipped)
    caplog.set_level(logging.WARNING, logger="agent_comms")
    node = fresh_node(nenv)
    hostlog.flush(10)
    said = [rec for rec in caplog.records if rec.name == "agent_comms.node" and "journal" in rec.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.WARNING, [x.getMessage() for x in caplog.records]
    assert str(bad) in said[0].getMessage(), "(review round 1, R3) the one WARNING names the offending file's path"
    assert node.groups.closed is not None
    for cwd in (nenv.node_dir, str(other)):
        assert node.groups.blocked(cwd)
        assert node.groups.start(cwd, ("run", "x"), lambda: object()) == (False, None), "every start is refused"
    assign_a_run(nenv)                          # (hello: the node reports as usual)
    for _ in range(3):
        node.tick()                             # (no exception: it stays up, and does not crash-loop)
    assert nenv.node_spawner.calls == [] and node.running == {}
    hostlog.flush(10)
    assert len([rec for rec in caplog.records if "journal" in rec.getMessage()]) == 1, "said once"


@pytest.mark.parametrize("stray", [".DS_Store", "notes.txt"])
def test_part0_5_a_ds_store_does_not_close_admission_and_any_other_stray_file_does(nenv, stray):
    """(C3-2b-2, Part 0.5) A Finder `.DS_Store` in the journal directory (a person looked at it) is ignored: the node
    restores the record beside it HELD and admits work elsewhere. Any other stray file still closes admission for the
    whole node, as before."""
    other = Path(nenv.node_dir).parent / "another-checkout"
    other.mkdir()
    j = journal_of(nenv.node.log_dir)
    r = record_for(nenv.node_dir)
    j.commit(r)
    (Path(j.directory) / stray).write_bytes(b"\x00\x00\x00\x01Bud1")
    node = fresh_node(nenv)
    if stray == ".DS_Store":
        assert node.groups.closed is None and not node.groups.blocked(str(other))
        assert node.groups.state(r.token) == node.groups.HELD
    else:
        assert node.groups.closed is not None and node.groups.blocked(str(other))


# ---------------------------------------------------------------- the legacy numeric recovery never sees them


def test_supervised_identities_never_enter_the_legacy_numeric_recovery(nenv):
    """node-runs.json names a legacy run (pid 4242) and the journal a supervised launch (shim pid 5151): `recover()`
    probes and signals the legacy pid only; the supervised record, and its HELD reservation, are untouched."""
    log_dir = Path(nenv.node.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / node_mod.STATE_FILE).write_text(json.dumps({"r-legacy": {"pid": 4242, "proc_start": "s"}}))
    r = record_for(nenv.node_dir, shim_pid=5151)
    journal_of(log_dir).commit(r)
    probed, signalled = [], []
    node = fresh_node(nenv, probe=lambda pid, start: (probed.append(pid), "ours")[1], start_of=lambda pid: "s",
                      signal_group=lambda pid, sig: signalled.append(pid), sleep=lambda s: None,
                      session_group_alive=lambda pid: False)
    assert node.recover() == ["r-legacy"]
    assert 4242 in probed and 5151 not in probed and set(signalled) <= {4242}
    assert node.groups.state(r.token) == node.groups.HELD
    assert [x.launch_id for x in journal_of(log_dir).load()] == [r.launch_id]


# ---------------------------------------------------------------- the order, statically


def test_the_restore_is_the_first_thing_after_the_registry_exists_and_before_any_admission():
    """In `Node.__init__` the restore is the statement right after the registry is chosen (`self.groups = ...`):
    before `recover()`, the session runner's start, `hello()` and the first `tick()`, which `_run` calls only after
    `Node(...)` returns."""
    tree = ast.parse(Path(node_mod.__file__).read_text())
    init = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "__init__"
                and "self.groups" in ast.unparse(n))
    body = [ast.unparse(s) for s in init.body]
    at = next(i for i, s in enumerate(body) if s.startswith("self.groups = "))
    assert body[at + 1].startswith("closed = shimjournal.restore(self.groups, "), body[at + 1]
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run")
    calls = [ast.unparse(c.func) for c in ast.walk(run) if isinstance(c, ast.Call)]
    order = [c for c in calls if c in ("Node", "node.recover", "node.sessions.recover", "node.hello",
                                       "node.sessions.start", "node.tick")]
    assert order[0] == "Node", order


def test_restore_installs_under_the_recorded_key_and_the_directorys_key_now(tmp_path):
    """The reservation is installed under the checkout's identity as recorded AND as it is now (a checkout replaced
    since is fenced too), HELD, under the supervised token kind only."""
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    r = record_for(cwd, checkout_key=(1, 2))
    journal_of(tmp_path / "logs").commit(r)
    g = node_groups.Groups()
    assert shimjournal.restore(g, tmp_path / "logs") is None
    assert {k for k, groups in g.entries.items() if r.token in groups} == {(1, 2), node_groups.key(str(cwd))}
    assert g.blocked(str(cwd)) and r.token in g.held
    with pytest.raises(ValueError):
        g.restore_held(("run", "x"), [(1, 2)], str(cwd))
