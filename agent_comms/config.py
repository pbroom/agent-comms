"""Paths, settings (board.toml) and agent tokens (agents.toml)."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def home() -> Path:
    return Path(os.environ.get("AGENT_COMMS_HOME", REPO_ROOT)).expanduser()


def human_token_file() -> Path:
    return Path(os.environ.get("AGENT_COMMS_TOKEN_FILE", "~/.config/agent-comms/human.token")).expanduser()


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    db_path: Path = field(default_factory=lambda: home() / "data" / "board.db")
    agents_path: Path = field(default_factory=lambda: home() / "agents.toml")
    lease_ttl_minutes: int = 30
    max_agent_posts_per_thread_without_human: int = 12
    daily_post_cap_per_agent: int = 200
    body_max_bytes: int = 4096
    max_refs: int = 20
    require_human_accept: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or home() / "board.toml"
        s = cls()
        if path.exists():
            data = tomllib.loads(path.read_text())
            for section in ("server", "limits", "tasks"):
                for k, v in data.get(section, {}).items():
                    if not hasattr(s, k):
                        raise ValueError(f"unknown setting [{section}] {k} in {path}")
                    setattr(s, k, Path(v).expanduser() if k.endswith("_path") else v)
        for p in ("db_path", "agents_path"):
            val = getattr(s, p)
            if not val.is_absolute():
                setattr(s, p, home() / val)
        return s


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "ac_" + secrets.token_urlsafe(32)


@dataclass
class AgentSpec:
    name: str
    runtime: str
    token_sha256: str
    is_human: bool = False


def read_agents(path: Path) -> dict[str, AgentSpec]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text()).get("agents", {})
    out = {}
    for name, d in data.items():
        out[name] = AgentSpec(name, d["runtime"], d["token_sha256"], bool(d.get("is_human", False)))
    return out


def write_agents(path: Path, agents: dict[str, AgentSpec]) -> None:
    lines = [
        "# agent-comms identities. GITIGNORED. Only SHA-256 hashes of tokens are stored here;",
        "# the plaintext token is printed once by `board create-agent`.",
        "",
    ]
    for a in agents.values():
        lines += [f"[agents.{a.name}]", f'runtime = "{a.runtime}"', f'token_sha256 = "{a.token_sha256}"']
        if a.is_human:
            lines.append("is_human = true")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def create_agent(path: Path, name: str, runtime: str, is_human: bool = False, rotate: bool = False) -> str:
    """Add an agent (or rotate its token) and return the plaintext token."""
    if not NAME_RE.match(name):
        raise ValueError("agent name must match [a-z][a-z0-9_-]{0,31}")
    if not re.match(r"^[A-Za-z0-9._-]{1,40}$", runtime):
        raise ValueError("runtime must be a short identifier, e.g. claude-code, codex-cli")
    agents = read_agents(path)
    if name in agents and not rotate:
        raise ValueError(f"agent {name!r} already exists (use --rotate to issue a new token)")
    if is_human and any(a.is_human for a in agents.values() if a.name != name):
        raise ValueError("a human agent already exists; v1 supports exactly one human")
    token = new_token()
    agents[name] = AgentSpec(name, runtime, hash_token(token), is_human)
    write_agents(path, agents)
    return token


def load_agent_token(name: str) -> str:
    """Read ~/.config/agent-comms/<name>.token, refusing files others could read or swap."""
    import stat

    if not NAME_RE.match(name):
        raise ValueError("invalid agent name")
    path = Path(os.environ.get("AGENT_COMMS_TOKEN_DIR", "~/.config/agent-comms")).expanduser() / f"{name}.token"
    try:
        meta = path.lstat()
    except OSError as e:
        raise ValueError(f"cannot read token file {path}: {e.strerror}") from None
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
        raise ValueError(f"{path} must be a regular file owned by you")
    if stat.S_IMODE(meta.st_mode) & 0o077:
        raise ValueError(f"{path} must have mode 600 or stricter")
    token = path.read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError(f"{path} is empty or malformed")
    return token
