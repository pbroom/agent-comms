#!/usr/bin/env python3
"""Prepare versioned macOS runtime and LaunchAgents; never start or stop jobs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import re
import tomllib


def job(label: str, command: list[str], board_home: Path, runtime: Path,
        runner_path: str, log: Path) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [str(runtime / ".venv/bin/python"),
                             str(runtime / ".venv/bin/board"), *command],
        "WorkingDirectory": str(board_home),
        "EnvironmentVariables": {
            "AGENT_COMMS_HOME": str(board_home),
            "HOME": str(Path.home()),
            "PATH": runner_path,
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        # A deliberate board stop exits successfully and must stay stopped.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ExitTimeOut": 60,
        # A replacement dispatcher reconciles surviving workers itself.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def tunnel_job(host: str, port: int, log: Path) -> dict:
    """On a machine that is not the hub: forward 127.0.0.1:<port> here to the hub's board on its own 127.0.0.1:<port>
    over SSH (key auth only, no prompt), so the dashboard and the menu bar app work here exactly as on the hub. The
    hub's board keeps its localhost-only checks: requests arrive there from its own loopback."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("--hub-ssh must be a plain SSH host name")
    return {
        "Label": "dev.agentcomms.tunnel",
        "ProgramArguments": ["/usr/bin/ssh", "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                             "-o", "ControlMaster=no", "-o", "ControlPath=none",
                             "-L", f"127.0.0.1:{port}:127.0.0.1:{port}", "--", host],
        "RunAtLoad": True,
        # ssh exits when the link drops (keepalives) or the port is taken; launchd brings it back.
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def array_end(source: str, start: int) -> int:
    """Find the end of a TOML array without treating quoted/commented ] as syntax."""
    if source[start:start + 1] != "[":
        raise ValueError("agent-comms args must be an array")
    depth, i, quote = 0, start, ""
    while i < len(source):
        if quote:
            if quote.startswith('"') and source[i] == "\\":
                i += 2
                continue
            if source.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
        elif source[i] == "#":
            newline = source.find("\n", i)
            if newline < 0:
                break
            i = newline
        elif source[i] in "\"'":
            quote = source[i] * 3 if source.startswith(source[i] * 3, i) else source[i]
            i += len(quote)
            continue
        elif source[i] == "[":
            depth += 1
        elif source[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("agent-comms args array is incomplete")


def updated_codex_config(source: str, script: Path) -> str:
    """Change only the existing agent-comms launcher path; retain all other settings."""
    before = tomllib.loads(source)
    server = before.get("mcp_servers", {}).get("agent-comms")
    if not isinstance(server, dict) or server.get("command") != "bash":
        raise ValueError("Existing agent-comms MCP server must use the bash stdio launcher")
    args = server.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("Existing agent-comms MCP args must be a string array")
    matches = [i for i, arg in enumerate(args)
               if Path(arg).parts[-3:] == ("integrations", "codex", "stdio.sh")]
    if len(matches) != 1:
        raise ValueError("Expected one existing integrations/codex/stdio.sh argument")
    replacement_args = args.copy()
    replacement_args[matches[0]] = str(script)
    if replacement_args == args:
        return source
    headers = list(re.finditer(r"(?m)^[ \t]*\[(?!\[).*\][ \t]*(?:#.*)?$", source))
    section = None
    for i, header in enumerate(headers):
        probe = tomllib.loads(header.group() + "\n__installer_probe__ = true\n")
        if probe.get("mcp_servers", {}).get("agent-comms") == {"__installer_probe__": True}:
            section = (header.end(), headers[i + 1].start() if i + 1 < len(headers) else len(source))
            break
    if section is None:
        raise ValueError("Expected a dedicated [mcp_servers.agent-comms] table")
    match = re.search(r"(?m)^[ \t]*(?:args|\"args\"|'args')[ \t]*=[ \t]*", source[section[0]:section[1]])
    if match is None:
        raise ValueError("Could not locate the existing agent-comms args assignment")
    start = section[0] + match.end()
    end = array_end(source, start)
    result = source[:start] + json.dumps(replacement_args, ensure_ascii=False) + source[end:]
    expected = tomllib.loads(source)
    expected["mcp_servers"]["agent-comms"]["args"] = replacement_args
    if tomllib.loads(result) != expected:
        raise ValueError("Refusing a configuration edit that changes other settings")
    return result


def configure_codex(config: Path, script: Path) -> Path | None:
    """Back up privately, validate and atomically replace only the launcher argument."""
    if config.is_symlink():
        raise ValueError("Refusing a symlink Codex configuration file")
    original = config.read_bytes()
    replacement = updated_codex_config(original.decode(), script).encode()
    if replacement == original:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = config.with_name(f"{config.name}.before-agent-comms-{stamp}")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(original)
    with tempfile.NamedTemporaryFile(dir=config.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(replacement)
    try:
        if config.read_bytes() != original or config.is_symlink():
            raise ValueError("Codex configuration changed during preparation; refusing to overwrite it")
        os.replace(temporary, config)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def set_current(runtime_root: Path, runtime: Path) -> None:
    current = runtime_root / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError("Refusing to replace a non-symlink current runtime")
    with tempfile.NamedTemporaryFile(dir=runtime_root, delete=False) as output:
        temporary = Path(output.name)
    temporary.unlink()
    try:
        temporary.symlink_to(runtime, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="Reviewed Git commit or ref to install")
    parser.add_argument("--board-home", type=Path, required=True,
                        help="Existing canonical board configuration/data directory")
    parser.add_argument("--runtime-root", type=Path,
                        default=Path.home() / "Library/Application Support/Agent Comms/runtimes")
    parser.add_argument("--launchagents-dir", type=Path,
                        default=Path.home() / "Library/LaunchAgents")
    parser.add_argument("--runner-path", default=os.environ.get("PATH", ""),
                        help="PATH for Codex, Claude, Node and browser runners; no tokens")
    parser.add_argument("--configure-codex", action="store_true",
                        help="Back up and update only the existing agent-comms MCP launcher argument")
    parser.add_argument("--codex-config", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml")
    parser.add_argument("--dry-run", action="store_true", help="Print planned files; write nothing")
    parser.add_argument("--node", metavar="NAME",
                        help="This machine is a node, not the hub: prepare `board node --name NAME` (with --hub) "
                             "instead of the HTTP board and dispatcher")
    parser.add_argument("--hub", help="With --node: the hub's gateway URL, e.g. https://server.tailnet.ts.net")
    parser.add_argument("--hub-ssh", metavar="HOST",
                        help="With --node: also forward 127.0.0.1:8787 here to the hub's board over SSH to HOST, for "
                             "the dashboard and the menu bar app")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
         f"{args.revision}^{{commit}}"], text=True).strip()
    board_home = args.board_home.expanduser().resolve()
    if not (board_home / "board.toml").is_file():
        parser.error("--board-home must contain the existing board.toml")
    host = "127.0.0.1"
    for settings_file in [board_home / "board.toml", board_home / "board.local.toml"]:
        if settings_file.is_file():
            host = tomllib.loads(settings_file.read_text()).get("server", {}).get("host", host)
    if host != "127.0.0.1":
        parser.error("The supervised HTTP board must bind to 127.0.0.1")
    paths = list(dict.fromkeys(p for p in args.runner_path.split(":") if p.startswith("/")))
    paths += [p for p in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                         "/usr/sbin", "/sbin") if p not in paths]
    runner_path = ":".join(paths)
    runtime = args.runtime_root.expanduser().resolve() / sha
    script = runtime.parent / "current/integrations/codex/stdio.sh"
    codex_config = args.codex_config.expanduser().absolute()
    if args.configure_codex:
        # Parse and validate before installing, without printing any configuration values.
        if codex_config.is_symlink():
            parser.error("Refusing a symlink Codex configuration file")
        updated_codex_config(codex_config.read_text(), script)
    launchagents = args.launchagents_dir.expanduser().resolve()
    log_dir = Path.home() / "Library/Logs/Agent Comms"
    if args.node:
        if not args.hub:
            parser.error("--node needs --hub")
        jobs = [job("dev.agentcomms.node", ["node", "--hub", args.hub, "--name", args.node], board_home, runtime,
                    runner_path, log_dir / "node.log")]
        if args.hub_ssh:
            try:
                jobs.append(tunnel_job(args.hub_ssh, 8787, log_dir / "tunnel.log"))
            except ValueError as e:
                parser.error(str(e))
    elif args.hub or args.hub_ssh:
        parser.error("--hub and --hub-ssh go with --node")
    else:
        jobs = [
            job("dev.agentcomms.http", ["serve", "--port", "8787"], board_home, runtime, runner_path,
                log_dir / "http.log"),
            job("dev.agentcomms.dispatcher", ["dispatch", "run"], board_home, runtime, runner_path,
                log_dir / "dispatcher.log"),
        ]
    if args.dry_run:
        print(f"Planned current runtime: {runtime.parent / 'current'} -> {runtime}")
        if args.configure_codex:
            print(f"Planned Codex launcher update (private backup): {codex_config}")
        for spec in jobs:
            print(f"Planned: {launchagents / (spec['Label'] + '.plist')}")
            print(plistlib.dumps(spec).decode())
        return
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is required to install the frozen runtime")
    marker = runtime / ".installed-commit"
    if runtime.exists():
        if not marker.is_file() or marker.read_text().strip() != sha:
            parser.error(f"Incomplete runtime at {runtime}; inspect/remove it before retrying")
    else:
        runtime.mkdir(parents=True, mode=0o700)
        # Archive tracked committed files only: no local tokens, data or dirty source.
        with tempfile.TemporaryDirectory(prefix="agent-comms-install-") as tmp:
            archive = Path(tmp) / "source.tar"
            subprocess.run(["git", "-C", str(repo), "archive", "--format=tar",
                            f"--output={archive}", sha], check=True)
            subprocess.run(["tar", "-xf", str(archive), "-C", str(runtime)], check=True)
        subprocess.run([uv, "sync", "--frozen", "--no-dev", "--project", str(runtime)], check=True)
        marker.write_text(sha + "\n")
    if not (runtime / "integrations/codex/stdio.sh").is_file():
        parser.error("Reviewed runtime does not contain the Codex stdio launcher")
    set_current(runtime.parent, runtime)
    if args.configure_codex:
        backup = configure_codex(codex_config, script)
        print(f"Codex launcher configured; backup: {backup}" if backup else "Codex launcher already configured")
    log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    launchagents.mkdir(parents=True, exist_ok=True)
    for spec in jobs:
        log = Path(spec["StandardOutPath"])
        if log.is_symlink():
            parser.error(f"Refusing symlink log: {log}")
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        destination = launchagents / (spec["Label"] + ".plist")
        with tempfile.NamedTemporaryFile(dir=launchagents, delete=False) as output:
            temporary = Path(output.name)
            output.write(plistlib.dumps(spec))
        os.replace(temporary, destination)
        print(f"Prepared {destination}")
    print(f"Runtime {sha}: {runtime}")
    print("No jobs were activated. Follow scripts/launchagents.md for the controlled handover.")


if __name__ == "__main__":
    main()
