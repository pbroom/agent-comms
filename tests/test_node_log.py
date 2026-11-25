"""`board node`'s log (node.log_to_stderr): this package's INFO and WARNING lines reach stderr once each, with the
time and the level; third-party INFO (httpx: a line per request, with its URL) does not; the setup is idempotent and
leaves a logging setup already made alone. Each case runs in a fresh interpreter, so its logging state is clean."""
import re
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=TESTS.parent, capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{TESTS.parent}:{TESTS}"})


def test_the_node_log_has_the_packages_info_and_warnings_once_each_with_time_and_level():
    r = run("""
import logging
from agent_comms.node import log_to_stderr
log_to_stderr()
log_to_stderr()                                     # twice: still one handler
logging.getLogger("agent_comms.providers.claude").info("an info marker")
logging.getLogger("agent_comms.session_host").warning("a warning marker")
logging.getLogger("agent_comms.session_host").debug("a debug marker")
logging.getLogger("httpx").info("HTTP Request: GET http://hub/api/nodes/assignments")
logging.getLogger("httpcore.connection").info("connect_tcp.started")
logging.getLogger().warning("a root warning")      # not the package's: Python's own last resort, as before
print(len(logging.getLogger("agent_comms").handlers), len(logging.getLogger().handlers))
""")
    assert r.returncode == 0, r.stderr
    lines = r.stderr.splitlines()
    assert [l for l in lines if "an info marker" in l] == [l for l in lines if re.fullmatch(
        STAMP + r" INFO agent_comms\.providers\.claude: an info marker", l)] and len(
        [l for l in lines if "an info marker" in l]) == 1, lines
    assert len([l for l in lines if "a warning marker" in l]) == 1 and re.fullmatch(
        STAMP + r" WARNING agent_comms\.session_host: a warning marker",
        next(l for l in lines if "a warning marker" in l)), lines
    assert not [l for l in lines if "debug marker" in l or "HTTP Request" in l or "connect_tcp" in l], lines
    assert "a root warning" in lines, "the root logger is not touched"
    assert r.stdout.split() == ["1", "0"], "one handler on the package, none added to the root"


def test_a_logging_setup_already_made_is_left_alone():
    r = run("""
import logging, sys
logging.basicConfig(level=logging.WARNING, format="embedder: %(message)s", stream=sys.stderr)
from agent_comms.node import log_to_stderr
log_to_stderr()
pkg = logging.getLogger("agent_comms")
logging.getLogger("agent_comms.node").info("an info marker")
logging.getLogger("agent_comms.node").warning("a warning marker")
print(len(pkg.handlers), pkg.propagate, pkg.level)
""")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["0", "True", "0"]
    assert r.stderr.splitlines() == ["embedder: a warning marker"]


def test_the_correlation_line_of_a_real_adapter_reaches_the_node_log():
    """Through a real Claude adapter, with VERIFIED as shipped: the owner's line for a 2.1.330 process."""
    r = run("""
from agent_comms.node import log_to_stderr
from agent_comms.providers import claude
log_to_stderr()
import test_claude_correlation as t
assert claude.VERIFIED == frozenset({"2.1.330"}), claude.VERIFIED
p = t.adapter()
t.write(p)
t.raw(p, t.vinit(version="2.1.330"))
""")
    assert r.returncode == 0, r.stderr
    said = [l for l in r.stderr.splitlines() if "turn correlation is" in l]
    assert len(said) == 1 and re.fullmatch(
        STAMP + r" INFO agent_comms\.providers\.claude: Claude session \S+: this process reports Claude Code 2\.1\.330;"
        r" turn correlation is on for it", said[0]), r.stderr
