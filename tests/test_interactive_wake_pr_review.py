"""The PR review of the interactive wake (#91, DESIGN_NOTES "Waking an interactive session: PR review"). F1: after
the grace, a request a woken session read is held only while the board session that read it stays live, through
the real Dispatcher with a fake clock. F2: a post an unchosen waiter holds back under a limit is never offered to a
newer conversation, holds nothing, and the dispatcher can launch for it. F3b: the installer writes through a
symlinked settings file."""

import json
import os

from agent_comms import claude_hooks, dispatch, interactive_wake as iw

from conftest import PROJECT
from test_hosted_sessions import senv  # noqa: F401
from test_hosted_wake import GRACE, allow, ask, denv, launched, text_for  # noqa: F401
from test_interactive_wake import NA, NB, ONE, TWO, holds, on, rec, wait, woke

LIVE = 2 * 60          # denv's [dispatch] live_minutes


def woken_and_read(env, only="addressed"):
    """A waiter arms; the human posts a request to claude; the waiter is woken and acknowledges; the woken session
    reads the board once. Returns the post, the reading board session and when it read."""
    on(env)
    allow(env)
    wait(env)
    post = env.post("human", env.tid, "your turn", "request", to=["claude"])
    assert woke(env) == text_for(env, 1, [env.tid], authors="human")
    reader = env.session("claude")
    out = env.board.read_updates(env.p["claude"], reader, only=only)
    assert post["id"] in [d["id"] for d in out["posts"]]
    return post, reader, env.clock()


def test_f1_a_request_the_woken_session_read_goes_to_the_dispatcher_once_the_reader_is_not_live(denv):
    """The reviewer's probe. The session reads and goes away (no more calls). Inside the grace, and while the reader
    is still live, nothing is launched; the first tick after it is no longer live launches, as without the feature.
    Another conversation of the agent that keeps arming in the project changes nothing: waiting is not reading."""
    post, reader, read_at = woken_and_read(denv)
    for _ in range(2):                                   # +60 s, +120 s: within the grace, then the reader is live
        denv.clock.advance(60)
        wait(denv, conv=TWO, nonce=NB)
        denv.d.tick()
        assert launched(denv) == [] and holds(denv, post)
    assert denv.clock() - read_at == LIVE
    denv.clock.advance(60)
    wait(denv, conv=TWO, nonce=NB)
    assert not holds(denv, post), "the reader is no longer live: it holds nothing"
    denv.d.tick()
    assert launched(denv) == ["claude-fake"], "the first tick after the reader went quiet launches"
    assert str(post["id"]) in rec(denv)["claimed"]


def test_f1_after_a_full_read_with_a_cursor_ack_too(denv):
    post, reader, _ = woken_and_read(denv, only="all")
    out = denv.board.read_updates(denv.p["claude"], reader)
    if out.get("ack_through"):
        denv.board.ack(denv.p["claude"], reader, out["ack_through"])
    denv.clock.advance(LIVE + 1)
    denv.d.tick()
    assert launched(denv) == ["claude-fake"]


def test_f1_a_reader_that_stays_live_keeps_it_until_it_goes_quiet(denv):
    post, reader, _ = woken_and_read(denv)
    for _ in range(30):                                  # half an hour of calls from the session that read it
        denv.clock.advance(60)
        denv.board.heartbeat(denv.p["claude"], reader)
        denv.d.tick()
        assert holds(denv, post)
    assert launched(denv) == []
    denv.clock.advance(LIVE)
    denv.d.tick()
    assert launched(denv) == [], "seen exactly live_minutes ago: still live"
    denv.clock.advance(1)
    denv.d.tick()
    assert launched(denv) == ["claude-fake"]


def test_f1_a_live_session_of_the_agent_that_did_not_read_the_post_does_not_keep_the_hold(denv):
    """Only the board sessions that read the post count. Through a delivery policy, whose own hold also asks only
    for live sessions that read the request, the dispatcher launches beside another live session of the agent in
    the project that never read it (a dispatch rule would wait for any live session of the agent: main's rule)."""
    on(denv)
    denv.board.create_delivery_policy(denv.p["human"], agent="claude", project=PROJECT)
    wait(denv)
    tid = denv.board.create_thread(denv.p["codex"], denv.sid["codex"], "an agent's thread", PROJECT)["id"]
    post = denv.post("codex", tid, "please take this over", "handoff", to=["claude"])
    assert woke(denv) is not None
    reader = denv.session("claude")
    denv.board.read_updates(denv.p["claude"], reader, only="addressed")
    other = denv.session("claude")                       # registered in the project, live, never reads
    for _ in range(2):                                   # +60 s, +120 s: the grace, then the reader is live
        denv.clock.advance(60)
        denv.board.heartbeat(denv.p["claude"], other)
        denv.d.tick()
        assert launched(denv) == [] and holds(denv, post)
    denv.clock.advance(60)
    denv.board.heartbeat(denv.p["claude"], other)
    assert not holds(denv, post), "the reader is not live; the live session never read it"
    denv.d.tick()
    assert launched(denv) == ["claude-fake"]


# ------------------------------------------------------------ F2: a post held back by an unchosen waiter


def test_f2_a_post_an_unchosen_waiter_holds_back_is_not_offered_to_a_newer_waiter_and_holds_nothing(denv):
    """Current behaviour, pinned for its safety (the fix, merging pending posts into the chosen waiter at O1, is for
    later): A is woken for P1; P2 arrives inside the interval and waits in A's pending; B arms after P2 and is the
    chosen waiter. Neither is offered P2 while B is connected, P2 holds nothing, and the dispatcher launches."""
    on(denv)
    allow(denv)
    wait(denv)
    first = denv.post("human", denv.tid, "one", "request", to=["claude"])
    assert woke(denv) is not None
    later_post = ask(denv, denv.tid)                     # inside wake_min_interval_seconds
    assert wait(denv)["state"] == "idle"
    assert str(later_post["id"]) in rec(denv)["pending"], "held back in A's pending"
    denv.clock.advance(1)
    wait(denv, conv=TWO, nonce=NB)                       # B arms after P2: its marks start above it
    for _ in range(60):
        denv.clock.advance(5)
        for conv, nonce in ((ONE, NA), (TWO, NB)):
            out = wait(denv, conv=conv, nonce=nonce, ack=False)
            assert out["state"] == "idle", "never offered to A (not chosen) or B (above its marks)"
    assert not holds(denv, later_post) and not iw.holds(denv.board, "claude", PROJECT, later_post["id"],
                                                        denv.clock())
    denv.clock.advance(LIVE + 1)                         # no live board session of the agent left
    denv.d.tick()
    assert launched(denv) == ["claude-fake"], "the dispatcher launches (for the thread's first request)"
    item = {"post_id": later_post["id"], "thread_id": denv.tid}
    assert not denv.d._hosted_holds("claude", item, denv.clock()), "and its launch gate holds nothing back for P2"
    assert first["id"] in dispatch.list_runs(denv.board, denv.p["human"], limit=5)[0]["request_ids"]


# ------------------------------------------------------------ F3b: a symlinked settings file


def test_f3b_the_installer_writes_through_a_symlinked_settings_file(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "claude-settings.json"
    real.write_text(json.dumps({"theme": "dark"}))
    os.chmod(real, 0o640)
    link = home / ".claude" / "settings.json"
    link.symlink_to(real)
    script = "/opt/agent-comms/integrations/claude-code/wait.sh"
    board = ["--home", str(tmp_path / "board"), "--agent", "claude"]
    assert claude_hooks.main(["add", "--settings", str(link), "--script", script, *board]) == 0
    assert link.is_symlink() and os.readlink(link) == str(real), "the link is kept"
    got = json.loads(real.read_text())
    assert got["theme"] == "dark" and set(got["hooks"]) == {"Stop", "SessionStart"}, "written through to its target"
    assert (os.stat(real).st_mode & 0o777) == 0o640, "the target keeps its permissions"
    assert [p.name for p in dotfiles.iterdir()] == ["claude-settings.json"], "no temp file left beside the target"
    assert claude_hooks.main(["remove", "--settings", str(link), "--script", script]) == 0
    assert link.is_symlink() and "hooks" not in json.loads(real.read_text())
