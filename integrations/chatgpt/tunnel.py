"""Foreground supervisor; stop uses a private Unix socket, never a stored PID."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

from gateway import load_token


def launch_environment(environ, dotenv_path):
    """Read only an optional private OPENAI_API_KEY assignment, without shell evaluation."""
    env = dict(environ)
    if env.get("CONTROL_PLANE_API_KEY") or env.get("OPENAI_API_KEY"):
        return env
    try:
        fd = os.open(dotenv_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return env
    except OSError:
        raise ValueError("Cannot safely open .env.local")
    with os.fdopen(fd) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError(".env.local must be a regular file owned by you with mode 0600")
        values = []
        for line in stream:
            match = re.fullmatch(r"\s*OPENAI_API_KEY\s*=\s*(.*?)\s*", line)
            if match:
                value = match.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                if not value or any(character.isspace() for character in value) or any(character in value for character in "$`\\"):
                    raise ValueError("Invalid OPENAI_API_KEY assignment in .env.local")
                values.append(value)
        if len(values) > 1:
            raise ValueError("Duplicate OPENAI_API_KEY assignments in .env.local")
        if values:
            env["OPENAI_API_KEY"] = values[0]
    return env


def build_tunnel_command(transport, mode, executable, environ, token, health_file):
    """Build argv without secret literals; ignore inherited tunnel/proxy overrides."""
    if transport not in {"openai", "cloudflare"} or mode not in {"mcp", "actions"}:
        raise ValueError("Unknown transport or mode")
    env = {key: environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in environ}
    if transport == "cloudflare":
        return [executable, "tunnel", "--url", "http://127.0.0.1:8789", "--no-autoupdate"], env
    if mode != "mcp":
        raise ValueError("The private OpenAI transport supports MCP only; actions requires explicit --transport cloudflare")
    key = environ.get("CONTROL_PLANE_API_KEY") or environ.get("OPENAI_API_KEY")
    tunnel_id = environ.get("CONTROL_PLANE_TUNNEL_ID") or environ.get("OPENAI_TUNNEL_ID")
    if not key:
        raise ValueError("Set CONTROL_PLANE_API_KEY or OPENAI_API_KEY in the launch environment")
    if not tunnel_id or not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id):
        raise ValueError("Set CONTROL_PLANE_TUNNEL_ID or OPENAI_TUNNEL_ID to a valid tunnel ID")
    env["CONTROL_PLANE_API_KEY"] = key
    env["AGENT_COMMS_CHATGPT_AUTHORIZATION"] = "Bearer " + token
    return [executable, "run", "--control-plane.tunnel-id", tunnel_id,
            "--control-plane.api-key", "env:CONTROL_PLANE_API_KEY",
            "--mcp.server-url", "http://127.0.0.1:8789/mcp",
            "--mcp.extra-headers", "Authorization: env:AGENT_COMMS_CHATGPT_AUTHORIZATION",
            "--health.listen-addr", "127.0.0.1:0", "--health.url-file", str(health_file)], env


def readiness_url(value):
    """Only inspect the child-generated loopback health listener."""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password:
        raise ValueError("Invalid tunnel health URL")
    return f"http://127.0.0.1:{parsed.port}/readyz"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "stop"])
    parser.add_argument("--transport", choices=["openai", "cloudflare"], default="openai")
    parser.add_argument("--mode", choices=["mcp", "actions"], default="mcp")
    args = parser.parse_args()
    state = Path.home() / ".local/state/agent-comms-chatgpt"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise SystemExit("Tunnel state directory must be owned by you with mode 0700")
    address = str(state / "control.sock")
    if args.command == "stop":
        with socket.socket(socket.AF_UNIX) as client:
            try:
                client.connect(address)
                client.sendall(b"stop")
            except OSError:
                raise SystemExit("No running tunnel supervisor. No processes were signalled.")
        print("Tunnel stop requested.")
        return
    binary = "tunnel-client" if args.transport == "openai" else "cloudflared"
    executable = os.environ.get("TUNNEL_CLIENT_BIN") if args.transport == "openai" else None
    executable = executable or shutil.which(binary)
    if executable and (not Path(executable).is_file() or not os.access(executable, os.X_OK)):
        raise SystemExit("Configured tunnel executable is missing or not executable")
    if not executable:
        raise SystemExit(f"{binary} is required for the selected transport. No fallback was attempted.")
    health_file = state / "health.url"
    try:
        launch_env = launch_environment(os.environ, Path(__file__).resolve().parents[2] / ".env.local") if args.transport == "openai" else dict(os.environ)
        # Fail before reading the board token or contacting any service.
        build_tunnel_command(args.transport, args.mode, executable, launch_env, "", health_file)
    except ValueError as error:
        raise SystemExit(str(error))
    token = load_token()
    command, tunnel_env = build_tunnel_command(args.transport, args.mode, executable, launch_env, token, health_file)
    import httpx
    try:
        identity = httpx.get("http://127.0.0.1:8787/api/whoami", headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=5).json()
    except (httpx.HTTPError, ValueError):
        raise SystemExit("Start the board on 127.0.0.1:8787 before starting the tunnel.")
    if identity.get("name") != "chatgpt" or identity.get("is_human") is not False:
        raise SystemExit("The configured token must belong to the non-human chatgpt agent.")
    children = []
    stop = False
    def on_stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    with socket.socket(socket.AF_UNIX) as control:
        try:
            control.bind(address)
        except OSError:
            raise SystemExit("Supervisor socket exists. Run stop first; if stale, remove control.sock manually. No PID was killed.")
        os.chmod(address, 0o600)
        control.listen(1)
        control.settimeout(0.5)
        try:
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
            env["AGENT_COMMS_CHATGPT_TOKEN"] = token
            # Reserve the listener ourselves and hand its descriptor to our child.
            # An existing service on 8789 therefore cannot accidentally be exposed.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 8789))
                listener.listen(128)
                children.append(subprocess.Popen([
                    sys.executable, str(Path(__file__).with_name("gateway.py")),
                    "--mode", args.mode, "--fd", str(listener.fileno()),
                ], env=env, pass_fds=(listener.fileno(),)))
            for _ in range(40):
                if children[0].poll() is not None:
                    raise SystemExit("Gateway could not start; no tunnel opened.")
                try:
                    response = httpx.get("http://127.0.0.1:8789/mcp", trust_env=False, timeout=0.2)
                    if response.status_code == 401:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise SystemExit("Gateway did not become ready; no tunnel opened.")
            health_file.unlink(missing_ok=True)
            children.append(subprocess.Popen(command, env=tunnel_env))
            print(f"{args.transport} tunnel process launched; remote readiness not yet verified. Ctrl-C or scripts/chatgpt-tunnel.sh stop tears it down.", flush=True)
            ready = False
            while not stop and all(child.poll() is None for child in children):
                if args.transport == "openai" and not ready and health_file.is_file():
                    try:
                        ready = httpx.get(readiness_url(health_file.read_text()), trust_env=False, timeout=0.5).status_code == 200
                        if ready:
                            print("Tunnel /readyz passed. ChatGPT attachment and board tool calls still require verification.", flush=True)
                    except (httpx.HTTPError, ValueError, OSError):
                        pass
                try:
                    connection, _ = control.accept()
                    with connection:
                        connection.settimeout(1)
                        stop = connection.recv(16) == b"stop"
                except (socket.timeout, TimeoutError):
                    pass
            if not stop:
                raise SystemExit("A supervised child exited; tearing down the tunnel and gateway.")
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for child in reversed(children):
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            Path(address).unlink(missing_ok=True)
            health_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
