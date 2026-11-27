from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_comms.config import Settings, create_agent
from agent_comms import notify
from agent_comms.core import Board, Principal

PROJECT = "/work/repo"

# Every agent post that asks the human (needs_response=true, to nobody or to the human) must carry a decision_question
# (core.Board._check_asks_human_format). Tests that only need such a post use ASK: two options that only answer (no
# approval, no mechanical action).
ASK = {"question": "Which way?", "context": "",
       "options": [{"id": "yes", "label": "Go ahead", "description": "Proceed as described", "outcome": "answered"},
                   {"id": "no", "label": "Hold off", "description": "Do nothing for now", "outcome": "answered"}],
       "recommended_option_id": "yes"}


def legacy_plain(env, post_id: int, type: str | None = None) -> None:
    """Make a post look like one stored before the format was enforced: no decision_question (and optionally
    another type), as live boards still hold."""
    env.board.conn.execute("UPDATE posts SET decision_question = NULL, type = COALESCE(?, type) WHERE id = ?",
                           (type, post_id))


# A parallel run (`pytest -n auto --dist worksteal`, README "Tests") starts this test first. It runs
# scripts/session-host-e2e.py, which takes about 240 s, mostly waiting: longer than the whole rest of the suite
# on 18 workers (about 45 s), so started late it decides the wall time. Started first, with worksteal handing what
# was queued behind it to idle workers, the run ends soon after it does. Only one test goes first: a worker
# holds the test after the one it is running (pytest's `nextitem`), so a second long test placed next would wait
# out the first. Order only: every test still runs, and a serial `pytest` keeps its usual order. If the node id
# changes, nothing breaks; the test is simply not moved.
START_FIRST_IN_PARALLEL = "tests/test_hosted_sessions.py::test_the_end_to_end_script_passes_with_the_stand_in"


def pytest_collection_modifyitems(config, items):
    if not hasattr(config, "workerinput"):     # only an xdist worker has it: a serial run keeps its order
        return
    items.sort(key=lambda item: item.nodeid != START_FIRST_IN_PARALLEL)     # stable: the rest keep their order


@pytest.fixture(autouse=True)
def _hostlog_fresh():
    """Each test starts with an empty host-side log queue (agent_comms.hostlog): no record a former test's watchdog
    thread left undrained is written into this one's handlers."""
    from agent_comms import hostlog
    hostlog._reset()
    yield


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """Tests never spawn osascript; tests that exercise delivery inject their own deliverer."""
    monkeypatch.setattr(notify.MacOSDeliverer, "available", lambda self: False)


@pytest.fixture(autouse=True)
def _no_real_conversation_homes(monkeypatch, tmp_path_factory):
    """Tests never read the machine's real ~/.claude or ~/.codex: the defaults point at empty directories."""
    empty = tmp_path_factory.mktemp("no-client-homes")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(empty / "codex"))


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class Env:
    board: Board
    clock: FakeClock
    settings: Settings
    tokens: dict[str, str]
    p: dict[str, Principal] = field(default_factory=dict)
    sid: dict[str, int] = field(default_factory=dict)

    def session(self, name: str, project: str = PROJECT, worktree: str | None = None) -> int:
        return self.board.register_session(self.p[name], project, worktree)["session_id"]

    def thread(self, title: str = "t", as_: str = "human") -> int:
        return self.board.create_thread(self.p[as_], self.sid[as_], title, PROJECT)["id"]

    def post(self, as_: str, thread_id: int, body: str = "hi", type: str = "status", **kw) -> dict:
        return self.board.create_post(self.p[as_], kw.pop("session_id", self.sid[as_]), body=body, type=type,
                                      thread_id=thread_id, **kw)

    def accepted_task(self, thread_id: int, **fields) -> int:
        fields.setdefault("title", "task")
        return self.board.create_task(self.p["human"], self.sid["human"], thread_id, **fields)["id"]


def make_env(tmp_path, **overrides) -> Env:
    s = Settings(db_path=tmp_path / "board.db", agents_path=tmp_path / "agents.toml")
    for k, v in overrides.items():
        setattr(s, k, v)
    tokens = {"human": create_agent(s.agents_path, "human", "human", is_human=True)}
    for name, rt in (("claude", "claude-code"), ("codex", "codex-cli"), ("grok", "grok")):
        tokens[name] = create_agent(s.agents_path, name, rt)
    clock = FakeClock()
    board = Board(s, clock=clock)
    env = Env(board, clock, s, tokens)
    for name, tok in tokens.items():
        env.p[name] = board.authenticate(tok)
        env.sid[name] = env.session(name)
    return env


@pytest.fixture
def env(tmp_path) -> Env:
    return make_env(tmp_path)
