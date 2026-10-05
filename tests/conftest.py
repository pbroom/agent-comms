from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_comms.config import Settings, create_agent
from agent_comms import notify
from agent_comms.core import Board, Principal

PROJECT = "/work/repo"


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """Tests never spawn osascript; tests that exercise delivery inject their own deliverer."""
    monkeypatch.setattr(notify.MacOSDeliverer, "available", lambda self: False)


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
