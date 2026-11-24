"""A stand-in for `codex app-server` on stdio: no model, no network, nothing of the user's.

It speaks the part of the app-server protocol the session host reads, with the lines codex-cli 0.171.0 wrote in the
spike (DESIGN_NOTES "Session host: Codex adapter"): one JSON message per line, no `jsonrpc` field; `initialize` first
(anything before it is refused "Not initialized"); `thread/start` issues a thread id (a UUID) and `thread/resume`
finds a thread only once a turn has started in it ("no rollout found for thread id ..." otherwise); a turn is
`turn/start` -> its reply with the turn id, `turn/started`, `item/completed` per item, `turn/completed`. The tests and
scripts/session-host-e2e.py install it as an executable named `codex` (the adapter goes by that name).

Threads are kept in files, so another process can resume them: in STAND_IN_CODEX_HOME, else in `stand-in-codex/`
beside the executable (never the user's home). `argv.jsonl` there gets each start's arguments and `requests.jsonl`
each `thread/start` and `thread/resume` as received.

Every turn also writes what a sub-agent's thread would (another `threadId`: its own turn, a `final_answer` of its
own, its own `turn/completed`) and a `commentary` message, which the adapter must not take for the turn's reply.
What a turn does is in its text:
- `sleep N ...`: the turn takes N seconds (and can be interrupted with `turn/interrupt`).
- `fail ...`: the turn ends `failed` with an error.
- `reject ...`: `turn/start` is answered with a JSON-RPC error: no turn is started at all.
- `crash ...`: the process exits (code 3) at once, before any answer or event for the turn.
- `deny ...`: the server asks to run a command (`item/commandExecution/requestApproval`), waits for the answer and
  reports the command `declined` unless it was accepted.
- `mcp ...`: an MCP tool call fails because it would need approval (as under approvalPolicy never); `mcp SERVER/TOOL
  ...` names the tool (default agent-comms/board_post).
- `big ...`: the reply is 40 KB.
- `recall ...`: the reply names the first message of the THREAD, whichever process took it.
- `write NAME`: the turn tries to create the file NAME in the thread's `cwd`. Under the `read-only` sandbox nothing
  is written and, as with Codex, no item says so; under `workspace-write` or `danger-full-access` the file is
  written. The reply says which: `WROTE NAME` or `WRITE REFUSED BY THE SANDBOX`.
- `hold PATH ...` (at the start of a line): the turn lasts until the file PATH exists (at most 120 seconds; it can be
  interrupted too).
  With `refuse-interrupt` in the text its `turn/interrupt` is answered with an error and the turn goes on; with
  `ignore-interrupt` it is answered `{}` and the turn goes on all the same; with `self-interrupt` the turn, once it
  is over, ends `interrupted` by itself (Codex has causes of its own for that).
The reply is otherwise `turn <k of this thread>: <TEXT IN CAPITALS>`: k goes on counting after a resume.
A restart, as codex-cli 0.171.0 was measured to do it (DESIGN_NOTES "Restarting a running turn"): `turn/interrupt`
ends the active turn `interrupted`; a `turn/start` right behind it starts a new turn.
Steering, as 0.171.0 was measured to do it (DESIGN_NOTES "Mid-turn steering", part B, gate M5): a `turn/start`'s
`clientUserMessageId` comes back as the `clientId` of the turn's `userMessage` item (item/started, item/completed);
a `turn/start` while a turn is active joins it (answered with THAT turn; its userMessage item, with its clientId,
comes in it). `turn/steer {threadId, expectedTurnId, input}` is answered `{turnId}` while that turn is active (its
text is read: the reply adds `; STEERED: <TEXT IN CAPITALS>`, and a userMessage item with no clientId comes in the
turn), and refused -32600 otherwise ("no active turn to steer", "expected active turn id `X` but found `Y`",
"expectedTurnId must not be empty"). Knobs, in the TURN's text:
- `refuse-steer`: every steer is refused ("no active turn to steer"), as Codex does once the turn has ended;
- `internal-error-steer`: a steer is answered with an internal error (-32603: Core lost its routing reply);
- `no-steer-answer`: a steer is never answered (its text is taken all the same);
- `autonomous-turn`: once the turn is over, the thread runs a turn of its own (no message's), 1.5 s long;
- `join-own`: the turn/start is a join: the thread has just begun a turn of its own, and the turn/start is answered
  with that turn, whose userMessage item then carries the clientId; the turn runs as that one;
- `late-item` (with `hold PATH`): the turn's own userMessage item comes only once the file PATH.item exists (so a
  steer waits for its proof meanwhile).
"""
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path

# `hold PATH` at the start of a line of the turn's text (a child's task arrives inside the server's frame)
HOLD = re.compile(r"(?:^|\n)hold (/\S+)")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class Server:
    def __init__(self, store: Path):
        self.store = store
        self.lines: queue.Queue = queue.Queue()
        self.initialized = False
        self.threads: dict[str, dict] = {}      # loaded in this process
        self.active: dict | None = None         # the turn in flight: {"thread", "id", "interrupted"}
        self.asked = 0                          # ids of the server's own requests start at 0, as Codex's do
        self.places: dict[str, tuple] = {}      # thread id -> (sandbox, cwd) as this process was given them
        self.answers: dict = {}

    # ------------------------------------------------------------ wire

    def write(self, message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def notify(self, method: str, **params) -> None:
        self.write({"method": method, "params": params})

    def error(self, rid, message: str) -> None:
        self.write({"id": rid, "error": {"code": -32600, "message": message}})

    def read_stdin(self) -> None:
        for line in sys.stdin:
            self.lines.put(line)
        self.lines.put(None)

    def next_line(self, timeout: float | None = None):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return ""
        if line is None:
            sys.exit(0)         # stdin closed: Codex exits 0 too
        return line

    # ------------------------------------------------------------ threads on disk

    def path(self, tid: str) -> Path:
        return self.store / "threads" / f"{tid}.json"

    def save(self, tid: str) -> None:
        self.path(tid).parent.mkdir(parents=True, exist_ok=True)
        self.path(tid).write_text(json.dumps(self.threads[tid]))

    def record(self, name: str, data) -> None:
        with open(self.store / name, "a") as f:
            f.write(json.dumps(data) + "\n")

    # ------------------------------------------------------------ requests

    def handle(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stderr.write("Failed to deserialize JSONRPCMessage\n")
            return
        if not isinstance(msg, dict):
            return
        rid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method is None:
            if rid is not None:
                self.answers[rid] = msg     # an answer to a request of the server's
            return
        if rid is None:
            return                          # a notification (`initialized`)
        if method == "initialize":
            if self.initialized:
                return self.error(rid, "Already initialized")
            self.initialized = True
            return self.write({"id": rid, "result": {"userAgent": "stand-in-codex/0", "codexHome": str(self.store),
                                                     "platformFamily": "unix", "platformOs": "stand-in"}})
        if not self.initialized:
            return self.error(rid, "Not initialized")
        if method in ("thread/start", "thread/resume"):
            self.record("requests.jsonl", {"method": method, "params": params})
        if method == "thread/start":
            tid = str(uuid.uuid4())
            self.threads[tid] = {"turns": 0, "first": None}
            self.places[tid] = (params.get("sandbox"), params.get("cwd") or os.getcwd())
            self.write({"id": rid, "result": self.thread_view(tid, params)})
            return self.notify("thread/started", thread={"id": tid, "status": {"type": "idle"}})
        if method == "thread/resume":
            tid = params.get("threadId")
            if not isinstance(tid, str) or not UUID.fullmatch(tid):
                return self.error(rid, "invalid session id: invalid character")
            if tid not in self.threads:
                if not self.path(tid).exists():
                    return self.error(rid, f"no rollout found for thread id {tid}")
                self.threads[tid] = json.loads(self.path(tid).read_text())
            self.places[tid] = (params.get("sandbox"), params.get("cwd") or os.getcwd())
            return self.write({"id": rid, "result": self.thread_view(tid, params)})
        if method == "turn/start":
            tid = params.get("threadId")
            if tid not in self.threads:
                return self.error(rid, f"thread not found: {tid}")
            text = "".join(x.get("text", "") for x in params.get("input", []) if isinstance(x, dict))
            if self.active is not None and self.active["thread"] == tid:
                # A turn/start while a turn is active joins it: Codex answers with the active turn, and its input
                # comes in that turn (its userMessage item, with its clientId), read like a steer's.
                self.write({"id": rid, "result": {"turn": self.turn_view(self.active["id"], "inProgress")}})
                self.active["steered"].append(text)
                return self.user_item(tid, self.active["id"], text, params.get("clientUserMessageId"))
            if text.startswith("reject"):
                return self.error(rid, "stand-in: turn/start refused")
            if text.startswith("crash"):
                sys.stdout.flush()
                os._exit(3)
            return self.turn(rid, tid, text, params.get("clientUserMessageId"))
        if method == "turn/steer":
            self.record("steers.jsonl", {"params": params})
            expected = params.get("expectedTurnId")
            if not isinstance(expected, str) or not expected:
                return self.error(rid, "expectedTurnId must not be empty")
            if self.active is None or self.active["thread"] != params.get("threadId"):
                return self.error(rid, "no active turn to steer")
            if expected != self.active["id"]:
                return self.error(rid, f"expected active turn id `{expected}` but found `{self.active['id']}`")
            if "refuse-steer" in self.active["text"]:
                return self.error(rid, "no active turn to steer")
            if "internal-error-steer" in self.active["text"]:
                return self.write({"id": rid, "error": {"code": -32603, "message": "internal error: the steer's "
                                                                                    "routing reply was lost"}})
            text = "".join(x.get("text", "") for x in params.get("input", []) if isinstance(x, dict))
            if "no-steer-answer" not in self.active["text"]:
                self.write({"id": rid, "result": {"turnId": self.active["id"]}})
            self.active["steered"].append(text)
            return self.user_item(self.active["thread"], self.active["id"], text, None)
        if method == "turn/interrupt":
            if self.active is None or params.get("turnId") != self.active["id"]:
                return self.error(rid, "no active turn to interrupt")
            if "refuse-interrupt" in self.active["text"]:
                return self.error(rid, "stand-in: the interrupt was refused")
            if "ignore-interrupt" not in self.active["text"]:
                self.active["interrupted"] = True
            return self.write({"id": rid, "result": {}})
        self.error(rid, f"Invalid request: unknown variant `{method}`")

    def thread_view(self, tid: str, params: dict) -> dict:
        return {"thread": {"id": tid, "sessionId": tid, "status": {"type": "idle"}, "turns": [], "ephemeral": False},
                "model": params.get("model") or "stand-in-default", "modelProvider": "stand-in",
                "cwd": params.get("cwd") or os.getcwd(), "approvalPolicy": params.get("approvalPolicy") or "on-request",
                "sandbox": {"type": {"workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}
                            .get(params.get("sandbox"), "readOnly")}}

    @staticmethod
    def turn_view(turn_id: str, status: str, items=(), error=None) -> dict:
        return {"id": turn_id, "rootTurnId": turn_id, "items": list(items), "status": status, "error": error,
                "itemsView": "summary" if items else "notLoaded", "durationMs": None if status == "inProgress" else 5}

    # ------------------------------------------------------------ one turn

    def pump(self, seconds: float, until=lambda: False, each=None) -> None:
        """Go on answering requests for this long (or until `until()`): an interrupt arrives while a turn runs.
        `each()`: called on every round (the `late-item` knob)."""
        deadline = time.monotonic() + seconds
        while not until() and not self.active["interrupted"]:
            if each is not None:
                each()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            line = self.next_line(timeout=min(left, 0.05))
            if line:
                self.handle(line)

    def user_item(self, tid: str, turn_id: str, text: str, client_id) -> None:
        """The userMessage item of input that a turn took (its own, a join's, a steer's), as 0.171.0 writes it."""
        item = {"type": "userMessage", "id": str(uuid.uuid4()), "clientId": client_id if isinstance(client_id, str)
                else None, "content": [{"type": "text", "text": text}]}
        self.notify("item/started", item=item, threadId=tid, turnId=turn_id)
        self.notify("item/completed", threadId=tid, turnId=turn_id, completedAtMs=0, item=item)

    def own_turn(self, tid: str, seconds: float) -> None:
        """A turn the thread runs by itself (no message's): started, a while, completed."""
        turn_id = str(uuid.uuid4())
        self.active = {"thread": tid, "id": turn_id, "interrupted": False, "text": "", "steered": []}
        self.notify("thread/status/changed", threadId=tid, status={"type": "active", "activeFlags": []})
        self.notify("turn/started", threadId=tid, turn=self.turn_view(turn_id, "inProgress"))
        self.pump(seconds)
        self.active = None
        self.notify("thread/status/changed", threadId=tid, status={"type": "idle"})
        self.notify("turn/completed", threadId=tid, turn=self.turn_view(turn_id, "completed"))

    def message(self, tid: str, turn_id: str, text: str, phase: str) -> dict:
        item = {"type": "agentMessage", "id": "msg_" + uuid.uuid4().hex, "text": text, "phase": phase}
        self.notify("item/completed", item=item, threadId=tid, turnId=turn_id, completedAtMs=0)
        return item

    def turn(self, rid, tid: str, text: str, client_id=None) -> None:
        thread = self.threads[tid]
        thread["turns"] += 1
        if thread["first"] is None:
            thread["first"] = text
        self.save(tid)          # from its first turn on a thread can be resumed
        turn_id = str(uuid.uuid4())
        self.active = {"thread": tid, "id": turn_id, "interrupted": False, "text": text, "steered": []}
        if "join-own" in text:
            # The thread has just begun a turn of its own; the turn/start joins it (answered with that turn).
            self.notify("thread/status/changed", threadId=tid, status={"type": "active", "activeFlags": []})
            self.notify("turn/started", threadId=tid, turn=self.turn_view(turn_id, "inProgress"))
            self.write({"id": rid, "result": {"turn": self.turn_view(turn_id, "inProgress")}})
        else:
            self.write({"id": rid, "result": {"turn": self.turn_view(turn_id, "inProgress")}})
            self.notify("thread/status/changed", threadId=tid, status={"type": "active", "activeFlags": []})
            self.notify("turn/started", threadId=tid, turn=self.turn_view(turn_id, "inProgress"))
        late = HOLD.search(text) if "late-item" in text else None
        if late is None:
            self.user_item(tid, turn_id, text, client_id)
        pending = [late.group(1) + ".item"] if late is not None else []

        def item_when_due():
            if pending and os.path.exists(pending[0]):
                pending.clear()
                self.user_item(tid, turn_id, text, client_id)
        # A sub-agent's thread, on the same connection: none of this is the session's own turn.
        child, child_turn = str(uuid.uuid4()), str(uuid.uuid4())
        self.notify("turn/started", threadId=child, turn=self.turn_view(child_turn, "inProgress"))
        decoy = self.message(child, child_turn, "A SUB-AGENT'S ANSWER, NOT THIS TURN'S", "final_answer")
        self.notify("turn/completed", threadId=child, turn=self.turn_view(child_turn, "completed", [decoy]))
        self.message(tid, turn_id, "Working on it.", "commentary")

        pause = re.match(r"sleep (\d+(?:\.\d+)?)\b", text)
        if pause:
            self.pump(float(pause.group(1)))
        held = HOLD.search(text)
        if held:
            self.pump(120, until=lambda: os.path.exists(held.group(1)), each=item_when_due)
        if text.startswith("deny") and not self.active["interrupted"]:
            ask, item_id = self.asked, "exec-" + str(uuid.uuid4())
            self.asked += 1
            self.notify("thread/status/changed", threadId=tid, status={"type": "active",
                                                                        "activeFlags": ["waitingOnApproval"]})
            self.write({"id": ask, "method": "item/commandExecution/requestApproval", "params": {
                "kind": "command", "threadId": tid, "turnId": turn_id, "itemId": item_id,
                "command": "/bin/zsh -lc 'touch /stand-in/secret/path.txt'", "cwd": os.getcwd(),
                "availableDecisions": ["accept", "cancel"]}})
            self.pump(10, until=lambda: ask in self.answers)
            answer = self.answers.pop(ask, {})
            decision = (answer.get("result") or {}).get("decision") if isinstance(answer.get("result"), dict) else None
            self.notify("serverRequest/resolved", threadId=tid, requestId=ask)
            self.notify("item/completed", threadId=tid, turnId=turn_id, completedAtMs=0, item={
                "type": "commandExecution", "id": item_id, "command": "/bin/zsh -lc 'touch /stand-in/secret/path.txt'",
                "status": "completed" if decision == "accept" else "declined", "exitCode": None})
        if text.startswith("mcp"):
            named = re.match(r"mcp ([A-Za-z0-9_-]+)/([A-Za-z0-9_]+)\b", text)    # `mcp SERVER/TOOL ...`: that one
            server, tool = named.groups() if named else ("agent-comms", "board_post")
            self.notify("item/completed", threadId=tid, turnId=turn_id, completedAtMs=0, item={
                "type": "mcpToolCall", "id": "exec-" + str(uuid.uuid4()), "server": server, "tool": tool,
                "status": "failed", "arguments": {"body": "STAND-IN-TOOL-ARGUMENT"}, "result": None,
                "error": {"message": "MCP tool call requires approval, but approval policy is never"}})

        if self.active["interrupted"] or "self-interrupt" in text:
            done = self.turn_view(turn_id, "interrupted")
        elif text.startswith("fail"):
            error = {"message": "stand-in: the turn failed", "codexErrorInfo": "other", "additionalDetails": None}
            self.notify("thread/status/changed", threadId=tid, status={"type": "systemError"})
            self.notify("error", error=error, willRetry=False, threadId=tid, turnId=turn_id)
            done = self.turn_view(turn_id, "failed", error=error)
        else:
            k = thread["turns"]
            reply = ("x" * 40_000 if text.startswith("big")
                     else f"turn {k}: FIRST WAS {thread['first'].upper()}" if text.startswith("recall")
                     else data_reply(text) or f"turn {k}: {text.upper()}")
            reply += "".join("; STEERED: " + s.upper() for s in self.active["steered"])
            wrote = re.match(r"write ([A-Za-z0-9._-]+)$", text)
            if wrote:
                sandbox, cwd = self.places.get(tid, (None, os.getcwd()))
                if sandbox in ("workspace-write", "danger-full-access"):
                    (Path(cwd) / wrote.group(1)).write_text("written by the stand-in\n")
                    reply = f"turn {k}: WROTE {wrote.group(1)}"
                else:
                    reply = f"turn {k}: WRITE REFUSED BY THE SANDBOX"
            done = self.turn_view(turn_id, "completed", [self.message(tid, turn_id, reply, "final_answer")])
        self.active = None
        self.notify("thread/status/changed", threadId=tid, status={"type": "idle"})
        self.notify("turn/completed", threadId=tid, turn=done)
        if "autonomous-turn" in text:
            self.own_turn(tid, 1.5)


# A turn that asks for a result as data (DESIGN_NOTES "Structured results": the frame's `<<<output-schema T>>>` line)
# and names `data-valid`, `data-invalid`, `data-two` or `data-none` in its text: the reply ends as the frame asks
# (or deliberately does not), with the review data.
REVIEW_DATA = {"verdict": "changes", "findings": [{"file": "src/pager.py", "line": 3, "note": "off by one"}]}


def data_reply(text: str):
    asked = re.search(r"<<<output-schema ([0-9a-f]{16})>>>", text)
    want = re.search(r"\bdata-(valid|invalid|two|none)\b", text)
    if not asked or not want:
        return None
    t, kind = asked.group(1), want.group(1)
    good = json.dumps(REVIEW_DATA)
    bad = json.dumps({"verdict": "maybe", "findings": []})
    one = f"<<<result-data {t}>>>\n{{body}}\n<<<end result-data {t}>>>"
    return {"valid": "Reviewed: one bug.\n" + one.format(body=good),
            "invalid": "Reviewed.\n" + one.format(body=bad),
            "two": "Draft:\n" + one.format(body=good) + "\nFinal:\n" + one.format(body=good),
            "none": "Reviewed, but no block."}[kind]


def main() -> None:
    args = sys.argv[1:]
    store = Path(os.environ.get("STAND_IN_CODEX_HOME") or Path(sys.argv[0]).resolve().parent / "stand-in-codex")
    store.mkdir(parents=True, exist_ok=True)
    server = Server(store)
    server.record("argv.jsonl", args)
    if args[:1] != ["app-server"]:
        sys.stderr.write("stand-in codex: only `app-server` is played\n")
        sys.exit(2)
    sys.stderr.write("stand-in codex started\n")
    threading.Thread(target=server.read_stdin, daemon=True).start()
    while True:
        server.handle(server.next_line())


if __name__ == "__main__":
    main()
