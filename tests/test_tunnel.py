"""Command construction only: these tests never launch a tunnel or contact an API."""
import importlib.util
from pathlib import Path
import sys

import pytest

root = Path(__file__).parents[1] / "integrations/chatgpt"
sys.path.insert(0, str(root))
try:
    spec = importlib.util.spec_from_file_location("board_tunnel", root / "tunnel.py")
    tunnel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tunnel)
finally:
    sys.path.pop(0)

TUNNEL_ID = "tunnel_" + "a" * 32


def build(env, mode="mcp", transport="openai"):
    return tunnel.build_tunnel_command(transport, mode, "/bin/tunnel-client", env, "board-secret", "/private/health.url")


def test_private_command_has_only_env_secret_references():
    argv, env = build({"CONTROL_PLANE_API_KEY": "runtime-secret", "CONTROL_PLANE_TUNNEL_ID": TUNNEL_ID,
                       "TUNNEL_CLIENT_CONFIG": "/unexpected/config", "MCP_COMMAND": "unsafe",
                       "LOG_HTTP_RAW_UNSAFE": "true", "HTTP_PROXY": "https://unexpected", "OPENAI_ADMIN_KEY": "admin-secret"})
    assert argv[1] == "run"
    assert argv[argv.index("--mcp.server-url") + 1] == "http://127.0.0.1:8789/mcp"
    assert argv[argv.index("--mcp.extra-headers") + 1] == "Authorization: env:AGENT_COMMS_CHATGPT_AUTHORIZATION"
    assert argv[argv.index("--control-plane.api-key") + 1] == "env:CONTROL_PLANE_API_KEY"
    assert argv[argv.index("--health.listen-addr") + 1] == "127.0.0.1:0"
    assert "runtime-secret" not in repr(argv) and "board-secret" not in repr(argv)
    assert env == {"CONTROL_PLANE_API_KEY": "runtime-secret", "AGENT_COMMS_CHATGPT_AUTHORIZATION": "Bearer board-secret"}


def test_environment_aliases_supported():
    argv, env = build({"OPENAI_API_KEY": "runtime-secret", "OPENAI_TUNNEL_ID": TUNNEL_ID})
    assert TUNNEL_ID in argv
    assert env["CONTROL_PLANE_API_KEY"] == "runtime-secret"


@pytest.mark.parametrize("env", [{}, {"CONTROL_PLANE_API_KEY": "key"},
    {"CONTROL_PLANE_TUNNEL_ID": TUNNEL_ID}, {"CONTROL_PLANE_API_KEY": "key", "CONTROL_PLANE_TUNNEL_ID": "bad"}])
def test_missing_credentials_fail_closed(env):
    with pytest.raises(ValueError):
        build(env)


def test_actions_never_silently_falls_back():
    with pytest.raises(ValueError, match="MCP only"):
        build({"OPENAI_API_KEY": "key", "OPENAI_TUNNEL_ID": TUNNEL_ID}, mode="actions")


def test_cloudflare_requires_explicit_selection_and_receives_no_credentials():
    argv, env = build({"OPENAI_API_KEY": "secret", "AGENT_COMMS_CHATGPT_TOKEN": "secret"}, mode="actions", transport="cloudflare")
    assert argv[1:4] == ["tunnel", "--url", "http://127.0.0.1:8789"]
    assert not env


@pytest.mark.parametrize("url", ["https://evil.test", "http://localhost:9999", "http://127.0.0.1", "http://user@127.0.0.1:9999"])
def test_readiness_never_contacts_arbitrary_host(url):
    with pytest.raises(ValueError):
        tunnel.readiness_url(url)


def test_readiness_uses_child_loopback_port():
    assert tunnel.readiness_url("http://127.0.0.1:54321/ui\n") == "http://127.0.0.1:54321/readyz"


def test_optional_private_dotenv_loads_only_api_key(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text('OTHER_SECRET=ignored\nOPENAI_API_KEY="dummy-key"\n')
    path.chmod(0o600)
    assert tunnel.launch_environment({}, path) == {"OPENAI_API_KEY": "dummy-key"}
    assert tunnel.launch_environment({"OPENAI_API_KEY": "from-env"}, path)["OPENAI_API_KEY"] == "from-env"
    assert tunnel.launch_environment({"CONTROL_PLANE_API_KEY": "preferred"}, path) == {"CONTROL_PLANE_API_KEY": "preferred"}


def test_missing_dotenv_is_optional(tmp_path):
    assert tunnel.launch_environment({}, tmp_path / "missing") == {}


def test_dotenv_rejects_symlink_and_public_permissions(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text("OPENAI_API_KEY=dummy")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        tunnel.launch_environment({}, path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="safely"):
        tunnel.launch_environment({}, link)


@pytest.mark.parametrize("contents", ["OPENAI_API_KEY=$(echo-danger)", "OPENAI_API_KEY=`echo-danger`", "OPENAI_API_KEY=one\nOPENAI_API_KEY=two"])
def test_dotenv_never_evaluates_shell_or_accepts_ambiguous_key(tmp_path, contents):
    path = tmp_path / ".env.local"
    path.write_text(contents)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        tunnel.launch_environment({}, path)
