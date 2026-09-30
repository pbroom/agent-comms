"""Load only Codex's token without printing it, then serve MCP on stdio."""
import os
from pathlib import Path
import stat
import sys


def main():
    token = os.environ.get("AGENT_COMMS_CODEX_TOKEN", "").strip()
    if not token:
        path = Path(os.environ.get(
            "AGENT_COMMS_CODEX_TOKEN_FILE", "~/.config/agent-comms/codex.token"
        )).expanduser()
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ValueError("token file must be a regular file owned by this user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("token file must have mode 600 or stricter")
            token = path.read_text().strip()
        except (OSError, ValueError) as exc:
            print(f"agent-comms: cannot load Codex token: {exc}", file=sys.stderr)
            return 1
    if not token or any(c.isspace() for c in token):
        print("agent-comms: Codex token is empty or malformed", file=sys.stderr)
        return 1
    os.environ["AGENT_COMMS_TOKEN"] = token
    # Keep the default independent of the current project or worktree.
    os.environ.setdefault("AGENT_COMMS_HOME", str(Path.home() / "agent-comms"))
    from agent_comms.mcp_server import run_stdio
    run_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
