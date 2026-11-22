"""A read-only check that an agent's token authenticates against the board (install.sh --wake, for Claude Code and
for Codex; review round 1 of the Codex wake, S4).

`python -I -m agent_comms.token_check --agent NAME`  exit 0, or 1 with one line on stderr.

The installers used to run `board brief` for this, which opens the local board database, initialises its schema and
rewrites the agent registry's mirror: an unchanged reinstall then wrote outside the file the installer edits. This
reads only: the board's settings and agents.toml, and the token file (`~/.config/agent-comms/<agent>.token`, or the
file given, with the same checks as `board wait` makes). When the board's registry is here it decides, and the token
is never sent; otherwise the token is sent only after it is established locally as an agent-comms token (`check`),
to the hub's `GET /api/whoami`, which must answer with that agent.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


TOKEN_RE = re.compile(r"ac_[A-Za-z0-9_-]{43}")    # config.new_token: "ac_" + secrets.token_urlsafe(32)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def check(agent: str, token_file=None, environ=None) -> str | None:
    """None when `agent`'s token (from `token_file` when given, else the agent's own token file) authenticates as
    `agent` (not the human), else why not.

    Nothing is sent anywhere until the file is established, locally, as an agent-comms token (review round 3, S11):
    a path misconfigured to an unrelated private file must not hand its contents to a hub. So:
    - where this machine has the board's agents.toml with that agent, the token's SHA-256 is compared with it there,
      and the token is never sent;
    - otherwise (a machine that only reaches a hub), the file must be `<agent>.token` in the agent-comms token
      directory (the same directory by identity, not by spelling), a regular file of yours with mode 600 or stricter,
      holding exactly one token of the form this project mints (TOKEN_RE). Only then is it sent, and only to the hub
      URL of the same settings (`environ` {} for the board's settings alone, without AGENT_COMMS_HUB from here).
    A message never includes the file's contents, and never anything taken from a response that was not checked
    first (round 2, S10)."""
    from . import remote
    from .config import NAME_RE, Settings, agent_token_file, hash_token, read_agents, read_token_file, token_dir
    if not NAME_RE.match(agent):
        return "not an agent name"
    path = Path(token_file) if token_file is not None else agent_token_file(agent)
    try:
        settings = Settings.load()
        hub = remote.hub_url(settings, environ)
    except (OSError, ValueError) as e:
        return f"cannot read the board's settings: {e}"
    try:
        spec = read_agents(settings.agents_path).get(agent)
    except (OSError, ValueError, KeyError) as e:
        return f"cannot read {settings.agents_path}: {e}"
    if spec is None and not hub and not Path(settings.agents_path).exists():
        return f"nothing can verify the token: the board's settings name no hub and there is no agent registry at " \
               f"{settings.agents_path} (set AGENT_COMMS_HOME to the board, or [server] hub on a machine that only " \
               f"reaches one)"
    if spec is not None or not hub:
        # The board's own registry is here: decide here, and send nothing.
        if spec is None or spec.is_human:
            return f"no agent '{agent}' in {settings.agents_path}"
        try:
            token = read_token_file(path)
        except ValueError as e:
            return str(e)
        if spec.token_sha256 != hash_token(token):
            return f"that token is not '{agent}''s token on this board"
        return None
    # Only a hub can tell: the file must first be an agent-comms token by every local sign.
    if path.name != f"{agent}.token" or not _same_dir(path.parent, token_dir()):
        return f"the token file is not {agent}.token in the agent-comms token directory ({token_dir()}); with the " \
               f"board on another machine it is not sent to the hub"
    try:
        token = read_token_file(path)
    except ValueError as e:
        return str(e)
    if not TOKEN_RE.fullmatch(token):
        return "the token file does not hold an agent-comms token; it is not sent to the hub"
    import httpx
    try:
        r = httpx.get(hub.rstrip("/") + "/api/whoami", headers={"Authorization": "Bearer " + token},
                      timeout=10, trust_env=False)
    except httpx.HTTPError as e:
        return f"cannot reach the hub {hub}: {type(e).__name__}"
    if r.status_code != 200:
        return f"the hub {hub} does not accept the '{agent}' token (HTTP {r.status_code})"
    try:
        who = r.json()
    except ValueError:
        who = None
    name = who.get("name") if isinstance(who, dict) else None
    if not (isinstance(name, str) and NAME_RE.match(name) and who.get("is_human") is False):
        return f"the hub {hub} answered who-am-i with something that is not an agent identity"
    if name != agent:
        return f"the '{agent}' token signs in at the hub as another agent, not as '{agent}'"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="token_check")
    ap.add_argument("--agent", required=True)
    why = check(ap.parse_args(argv).agent)
    if why:
        print(f"Not changed: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
