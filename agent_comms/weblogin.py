"""Dashboard sign-in for the human: single-use login links and cookie sessions. No schema change.

`POST /api/login-links` (human bearer token only) mints a random code, valid for 60 seconds, bound to a
same-origin `next` path. Opening `GET /login/<code>` consumes the code, creates a web session and sets an
HttpOnly, SameSite=Strict cookie, so a browser signs in with one click and never stores the token.

Both live in `board_state` rows, keyed by the SHA-256 of the secret (`web.login.<hash>`,
`web.session.<hash>`): a copy of the database cannot be replayed as a code or a cookie. A record keeps the
human's name and the hash of the human token that created it, so rotating or revoking the human token ends
every web session. A session lasts `[web] session_days` (default 30) after its last use, renewed on use at
most once an hour, and never longer than `[web] session_max_days` (default 90) after sign-in.

The session resolves to the human principal only, and only in the HTTP API (api.py): MCP and the ChatGPT
gateway accept bearer tokens and nothing else. CSRF and DNS-rebinding defences are in api.py and in
DESIGN_NOTES "Dashboard sign-in".
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from typing import Any

from . import db
from .config import hash_token
from .core import Forbidden, Invalid, NotFound, Principal, Unauthorized, iso

LINK_TTL_SECONDS = 60
RENEW_EVERY_SECONDS = 3600     # sliding renewal writes at most once an hour per session
MAX_PENDING_LINKS = 20         # unexpired, unused codes kept; older ones are dropped first
MAX_SESSIONS = 50              # signed-in browsers kept; the least recently used is dropped first
LINK_PREFIX = "web.login."
SESSION_PREFIX = "web.session."
COOKIE_PREFIX = "agent_comms_session"
PUBLIC_ID_LEN = 16             # a session's public id: the first 16 hex digits of its hash
NEXT_MAX = 2048
DAY = 86400
CODE_RE = re.compile(r"[A-Za-z0-9_-]{8,256}")   # the link contract; ours are token_urlsafe(32), 43 characters


class LinkInvalid(Unauthorized):
    """A login code that is unknown, expired, already used, or whose human token has since changed."""


def cookie_name(port: int | None) -> str:
    # Cookies are shared by every port on a host, so a board on another port (the demo, a test server) gets its
    # own cookie instead of overwriting this one.
    return f"{COOKIE_PREFIX}_{port}" if port else COOKIE_PREFIX


def check_next(value: Any) -> str:
    """A same-origin path to land on after sign-in: starts with one '/', printable ASCII only, no backslash.
    Rejects '//host', '/\\host', schemes, whitespace and control characters (browsers strip tabs and newlines
    from URLs, which can turn '/\\t/evil' into '//evil')."""
    if value is None:
        return "/"
    if not isinstance(value, str) or not value:
        raise Invalid("next must be a path starting with /")
    if len(value) > NEXT_MAX:
        raise Invalid(f"next must be at most {NEXT_MAX} characters")
    if not value.startswith("/") or value.startswith("//"):
        raise Invalid("next must be a same-origin path: one leading / and no host")
    if any(not 0x21 <= ord(ch) <= 0x7E for ch in value) or "\\" in value:
        raise Invalid("next may contain only printable ASCII without spaces or backslashes")
    return value


def lifetimes(s) -> tuple[int, int]:
    """(sliding, absolute) session lifetimes in seconds from [web]; bad values fall back to the defaults."""
    days, max_days = s.session_days, s.session_max_days
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        days = 30
    if isinstance(max_days, bool) or not isinstance(max_days, int) or max_days < 1:
        max_days = 90
    return days * DAY, max(days, max_days) * DAY


def browser_label(user_agent: str | None) -> str:
    """A short label from a fixed vocabulary (never the raw header), e.g. "Safari on macOS"."""
    ua = user_agent or ""
    browser = next((name for needle, name in (
        ("Claude/", "Claude app browser"), ("Electron/", "Embedded browser"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
        ("Chrome/", "Chrome"), ("Safari/", "Safari"), ("curl/", "curl"), ("python-", "Python")) if needle in ua),
        "Browser")
    system = next((name for needle, name in (
        ("iPhone", "iOS"), ("iPad", "iPadOS"), ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("Windows", "Windows"),
        ("Android", "Android"), ("Linux", "Linux")) if needle in ua), "")
    return f"{browser} on {system}" if system else browser


# ---------------------------------------------------------------- helpers


def _human_token_hash(c: sqlite3.Connection, name: str) -> str | None:
    row = c.execute("SELECT token_hash FROM agents WHERE name = ? AND active = 1 AND is_human = 1",
                    (name,)).fetchone()
    return row["token_hash"] if row else None


def _put(c: sqlite3.Connection, key: str, value: dict, by: str, now: float) -> None:
    c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                 updated_at = excluded.updated_at""", (key, json.dumps(value, sort_keys=True), by, now))


def _load(raw: str) -> dict | None:
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _expiry(rec: dict, s) -> float:
    """When a session ends under the current settings: its stored expiry, capped by today's [web] lifetimes."""
    sliding, absolute = lifetimes(s)
    try:
        return min(float(rec["expires_at"]), float(rec["created_at"]) + absolute, float(rec["last_seen"]) + sliding)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _prune(c: sqlite3.Connection, now: float) -> None:
    c.execute(f"""DELETE FROM board_state WHERE substr(key, 1, {len(LINK_PREFIX)}) = ?
                  AND (json_valid(value) = 0 OR json_extract(value, '$.expires_at') <= ?)""", (LINK_PREFIX, now))
    c.execute(f"""DELETE FROM board_state WHERE substr(key, 1, {len(SESSION_PREFIX)}) = ?
                  AND (json_valid(value) = 0 OR json_extract(value, '$.expires_at') <= ?)""", (SESSION_PREFIX, now))


def _cap(c: sqlite3.Connection, prefix: str, keep: int, order: str) -> None:
    c.execute(f"""DELETE FROM board_state WHERE key IN (
                    SELECT key FROM board_state WHERE substr(key, 1, {len(prefix)}) = ?
                    ORDER BY json_extract(value, '$.{order}') DESC, rowid DESC LIMIT -1 OFFSET ?)""", (prefix, keep))


# ---------------------------------------------------------------- login links


def create_link(board, p: Principal, next_path: Any = None) -> str:
    """A single-use code for `GET /login/<code>`, valid LINK_TTL_SECONDS. Human only."""
    board._require_human(p, "create sign-in links")
    target = check_next(next_path)
    code = secrets.token_urlsafe(32)
    now = board.now()
    with db.write_tx(board.conn) as c:
        token_hash = _human_token_hash(c, p.name)
        if token_hash is None:
            raise Forbidden("only the human can create sign-in links")
        _prune(c, now)
        _put(c, LINK_PREFIX + hash_token(code), {"agent": p.name, "token_hash": token_hash, "next": target,
                                                  "created_at": now, "expires_at": now + LINK_TTL_SECONDS}, p.name, now)
        _cap(c, LINK_PREFIX, MAX_PENDING_LINKS, "created_at")
    return code


def redeem_link(board, code: str, label: str) -> tuple[str, str, int]:
    """Consume a code and start a web session: (session secret for the cookie, next path, cookie max-age)."""
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        raise LinkInvalid("link expired")
    board.refresh_identities()
    now = board.now()
    secret = secrets.token_urlsafe(32)
    sliding, absolute = lifetimes(board.s)
    ok, target = False, "/"
    with db.write_tx(board.conn) as c:
        key = LINK_PREFIX + hash_token(code)
        row = c.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
        if row is not None:
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))   # single use, valid or not
            rec = _load(row["value"]) or {}
            ok = (isinstance(rec.get("expires_at"), (int, float)) and now < rec["expires_at"]
                  and rec.get("token_hash") is not None
                  and _human_token_hash(c, str(rec.get("agent"))) == rec.get("token_hash"))
        if ok:
            target = check_next(rec.get("next"))
            _prune(c, now)
            _put(c, SESSION_PREFIX + hash_token(secret), {
                "agent": rec["agent"], "token_hash": rec["token_hash"], "label": label[:60],
                "created_at": now, "last_seen": now, "expires_at": now + min(sliding, absolute)}, rec["agent"], now)
            _cap(c, SESSION_PREFIX, MAX_SESSIONS, "last_seen")
    if not ok:
        raise LinkInvalid("link expired")
    return secret, target, min(sliding, absolute)


# ---------------------------------------------------------------- sessions


def authenticate_session(board, secret: str | None) -> tuple[Principal, int | None]:
    """The human principal for a session cookie, and a new cookie max-age when the session was renewed."""
    if not secret or len(secret) > 100:
        raise Unauthorized("not signed in")
    board.refresh_identities()
    key = SESSION_PREFIX + hash_token(secret)
    row = board.conn.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
    rec = _load(row["value"]) if row else None
    if rec is None:
        raise Unauthorized("not signed in, or the sign-in was revoked")
    now = board.now()
    human = board.conn.execute(
        "SELECT name, runtime, is_human FROM agents WHERE name = ? AND active = 1 AND is_human = 1 AND token_hash = ?",
        (str(rec.get("agent")), str(rec.get("token_hash")))).fetchone()
    if human is None or now >= _expiry(rec, board.s):
        with db.write_tx(board.conn) as c:
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))
        raise Unauthorized("your sign-in expired; run `board dashboard` to sign in again")
    renewed = None
    if now - float(rec.get("last_seen", 0)) >= RENEW_EVERY_SECONDS:
        sliding, absolute = lifetimes(board.s)
        rec["last_seen"] = now
        rec["expires_at"] = min(now + sliding, float(rec["created_at"]) + absolute)
        with db.write_tx(board.conn) as c:
            # Only if it still exists: a revoke that landed since the read wins.
            c.execute("UPDATE board_state SET value = ?, updated_at = ? WHERE key = ?",
                      (json.dumps(rec, sort_keys=True), now, key))
        renewed = max(1, int(rec["expires_at"] - now))
    return Principal(human["name"], human["runtime"], True), renewed


def session_id_of(secret: str | None) -> str | None:
    return hash_token(secret)[:PUBLIC_ID_LEN] if secret else None


def list_sessions(board, p: Principal, current_secret: str | None = None) -> list[dict]:
    board._require_human(p, "list signed-in browsers")
    now, current = board.now(), session_id_of(current_secret)
    out = []
    for r in board.conn.execute(f"SELECT key, value FROM board_state WHERE substr(key, 1, {len(SESSION_PREFIX)}) = ?",
                                (SESSION_PREFIX,)):
        rec = _load(r["value"])
        if rec is None or rec.get("agent") != p.name or now >= _expiry(rec, board.s):
            continue
        sid = r["key"][len(SESSION_PREFIX):][:PUBLIC_ID_LEN]
        out.append({"id": sid, "label": str(rec.get("label") or "Browser"), "created_at": iso(rec.get("created_at")),
                    "last_seen": iso(rec.get("last_seen")), "expires_at": iso(_expiry(rec, board.s)),
                    "current": sid == current})
    out.sort(key=lambda x: x["last_seen"] or "", reverse=True)
    return out


def revoke_session(board, p: Principal, session_id: str) -> dict:
    board._require_human(p, "sign out browsers")
    if not isinstance(session_id, str) or not re.fullmatch(rf"[0-9a-f]{{{PUBLIC_ID_LEN}}}", session_id):
        raise NotFound("no such signed-in browser")
    prefix = SESSION_PREFIX + session_id
    with db.write_tx(board.conn) as c:
        n = c.execute(f"DELETE FROM board_state WHERE substr(key, 1, {len(prefix)}) = ?", (prefix,)).rowcount
    if not n:
        raise NotFound("no such signed-in browser")
    return {"revoked": session_id}


def revoke_all(board, p: Principal) -> int:
    """Sign out every browser (and drop unused sign-in links). Human only."""
    board._require_human(p, "sign out browsers")
    with db.write_tx(board.conn) as c:
        n = c.execute(f"DELETE FROM board_state WHERE substr(key, 1, {len(SESSION_PREFIX)}) = ?",
                      (SESSION_PREFIX,)).rowcount
        c.execute(f"DELETE FROM board_state WHERE substr(key, 1, {len(LINK_PREFIX)}) = ?", (LINK_PREFIX,))
    return n


def sign_out(board, secret: str | None) -> bool:
    """End the session this secret belongs to. Holding the cookie is the authority; nothing else is touched."""
    if not secret or len(secret) > 100:
        return False
    with db.write_tx(board.conn) as c:
        return c.execute("DELETE FROM board_state WHERE key = ?", (SESSION_PREFIX + hash_token(secret),)).rowcount > 0
