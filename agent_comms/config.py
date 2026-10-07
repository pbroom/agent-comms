"""Paths, settings (board.toml, plus an optional per-machine board.local.toml) and agent tokens (agents.toml)."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RUNTIME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
LOCAL_SETTINGS = "board.local.toml"   # per-machine overrides next to board.toml; gitignored
SECTIONS = ("server", "limits", "tasks", "web")
NOT_SETTINGS = {"dispatch", "config_path"}   # Settings fields that are not [server]/[limits]/[tasks]/[web] keys


def deep_merge(base: dict, over: dict) -> dict:
    """`over` wins. Tables merge key by key, recursively; any other value (including a list) replaces wholesale."""
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


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
    # [web]: dashboard sign-in sessions (see weblogin.py). File-only: the Settings page cannot change them.
    session_days: int = 30        # sliding: a session unused this long ends; use renews it (at most hourly)
    session_max_days: int = 90    # absolute: a session ends this long after sign-in, however much it is used
    # The raw [dispatch] table (merged across layers); validated by dispatch.DispatchConfig.
    dispatch: dict = field(default_factory=dict)
    # The board.toml these settings came from (board.local.toml is beside it), or None when they were built in
    # code. Not a setting: it lets a running Board hot-reload them (Board.reload_settings) and the Settings page
    # save edits to board.local.toml.
    config_path: Path | None = field(default=None, compare=False)

    @classmethod
    def known_keys(cls) -> set[str]:
        return {f.name for f in fields(cls)} - NOT_SETTINGS

    @classmethod
    def read_layer(cls, layer: Path) -> dict:
        """One settings file, parsed, with its [server]/[limits]/[tasks]/[web] keys checked ({} when it is absent)."""
        if not layer.exists():
            return {}
        d = tomllib.loads(layer.read_text())
        known = cls.known_keys()
        for section in SECTIONS:
            table = d.get(section, {})
            if not isinstance(table, dict):
                raise ValueError(f"[{section}] must be a table in {layer}")
            for k in table:
                if k not in known:
                    raise ValueError(f"unknown setting [{section}] {k} in {layer}")
        return d

    @classmethod
    def read_layers(cls, path: Path | None = None, local: bool = True) -> dict:
        """board.toml, then board.local.toml beside it (when `local` and present), deep-merged per section.
        Each layer's [server]/[limits]/[tasks]/[web] keys are checked, so an error names the file at fault."""
        path = path or home() / "board.toml"
        data: dict = {}
        for layer in [path] + ([path.with_name(LOCAL_SETTINGS)] if local else []):
            data = deep_merge(data, cls.read_layer(layer))
        return data

    @classmethod
    def from_data(cls, data: dict) -> "Settings":
        """Settings from already-merged layers (see read_layers)."""
        s = cls()
        for section in SECTIONS:
            for k, v in data.get(section, {}).items():
                setattr(s, k, Path(v).expanduser() if k.endswith("_path") else v)
        s.dispatch = data.get("dispatch", {})
        for p in ("db_path", "agents_path"):
            val = getattr(s, p)
            if not val.is_absolute():
                setattr(s, p, home() / val)
        return s

    @classmethod
    def load(cls, path: Path | None = None, local: bool = True) -> "Settings":
        path = path or home() / "board.toml"
        s = cls.from_data(cls.read_layers(path, local))
        # Only a board that reads the overlay can hot-reload it or save edits to it.
        s.config_path = path if local else None
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
