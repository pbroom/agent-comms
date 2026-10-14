"""Installation safety checks use synthetic configuration and private temporary roots."""

import importlib.util
from pathlib import Path
import plistlib
import stat
import sys
import tomllib

import pytest


spec = importlib.util.spec_from_file_location(
    "launchagents_installer", Path(__file__).resolve().parents[1] / "scripts/install-launchagents.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


CONFIG = '''# user formatting and unrelated settings must survive
model = "gpt-example"
[model_providers.private]
base_url = "https://example.invalid/api"
credential = "synthetic-fixture-secret"
[mcp_servers."agent-comms"] # quoted table
command = "bash"
args = [
  # old launcher
  "/fixture/old/agent-comms/integrations/codex/stdio.sh",
  "--fixture-argument-with-]-inside", # bracket is data
] # keep this comment
env = { AGENT_COMMS_HOME = "/fixture/board" }
[mcp_servers.agent-comms.tools.board_post]
approval_mode = "approve"
[mcp_servers.another]
command = "unchanged"
'''


def test_codex_update_preserves_every_other_setting_and_text():
    script = Path('/fixture/durable current/integrations/codex/stdio.sh')
    result = installer.updated_codex_config(CONFIG, script)
    before, after = tomllib.loads(CONFIG), tomllib.loads(result)
    before['mcp_servers']['agent-comms']['args'][0] = str(script)
    assert after == before
    assert result.startswith(CONFIG.split('args =')[0])
    assert result.endswith(CONFIG.split('] # keep this comment', 1)[1])
    assert '# keep this comment' in result


@pytest.mark.parametrize('source', [
    CONFIG.replace('command = "bash"', 'command = "python"', 1),
    CONFIG.replace('integrations/codex/stdio.sh', 'unrelated/launcher.sh', 1),
    CONFIG.replace('"--fixture-argument-with-]-inside"',
                   '"/another/integrations/codex/stdio.sh"'),
    'model = "gpt-example"\n',
])
def test_missing_or_ambiguous_launcher_is_refused(source):
    with pytest.raises(ValueError):
        installer.updated_codex_config(source, Path('/fixture/current/integrations/codex/stdio.sh'))


def test_backup_and_updated_config_are_private_and_idempotent(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text(CONFIG)
    config.chmod(0o644)
    script = tmp_path / 'current/integrations/codex/stdio.sh'
    backup = installer.configure_codex(config, script)
    assert backup.read_text() == CONFIG
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert installer.configure_codex(config, script) is None
    assert len(list(tmp_path.glob('config.toml.before-agent-comms-*'))) == 1


def test_codex_config_symlink_is_refused(tmp_path):
    original = tmp_path / 'original'
    original.write_text(CONFIG)
    config = tmp_path / 'config.toml'
    config.symlink_to(original)
    with pytest.raises(ValueError, match='symlink'):
        installer.configure_codex(config, tmp_path / 'launcher')
    assert original.read_text() == CONFIG


def test_current_pointer_never_overwrites_directory(tmp_path):
    (tmp_path / 'current').mkdir()
    with pytest.raises(ValueError, match='non-symlink'):
        installer.set_current(tmp_path, tmp_path / 'revision')


def test_codex_config_concurrent_change_is_preserved(tmp_path, monkeypatch):
    config = tmp_path / 'config.toml'
    config.write_text(CONFIG)
    real_read = Path.read_bytes
    calls = 0
    changed = CONFIG + '\n# user edit during preparation\n'
    def read_with_concurrent_edit(path):
        nonlocal calls
        if path == config:
            calls += 1
            if calls == 2:
                path.write_text(changed)
        return real_read(path)
    monkeypatch.setattr(Path, 'read_bytes', read_with_concurrent_edit)
    with pytest.raises(ValueError, match='changed during preparation'):
        installer.configure_codex(config, tmp_path / 'current/integrations/codex/stdio.sh')
    assert config.read_text() == changed
    assert len(list(tmp_path.glob('config.toml.before-agent-comms-*'))) == 1


def test_runtime_upgrade_changes_pointer_without_overwriting_previous_snapshot(tmp_path):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    (old / 'source').write_text('reviewed old source')
    installer.set_current(tmp_path, old)
    installer.set_current(tmp_path, new)
    assert (tmp_path / 'current').resolve() == new
    assert (old / 'source').read_text() == 'reviewed old source'


def fixture_args(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(installer.Path, 'home', classmethod(lambda cls: home))
    board = tmp_path / 'board'
    board.mkdir()
    (board / 'board.toml').write_text('[server]\nhost = "127.0.0.1"\nport = 8787\n')
    config = home / 'config.toml'
    config.write_text(CONFIG)
    runtimes = tmp_path / 'runtimes'
    launchagents = tmp_path / 'launchagents'
    sha = 'a' * 40
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *a, **kw: sha)
    monkeypatch.setattr(installer.shutil, 'which', lambda command: '/fixture/uv')
    def no_process_actions(*args, **kwargs):
        raise AssertionError('Existing fixture runtime must not start any subprocess')
    monkeypatch.setattr(installer.subprocess, 'run', no_process_actions)
    args = ['install-launchagents.py', '--revision', sha, '--board-home', str(board),
            '--runtime-root', str(runtimes), '--launchagents-dir', str(launchagents),
            '--configure-codex', '--codex-config', str(config), '--runner-path', '/fixture/node/bin:/usr/bin']
    return args, board, config, runtimes, launchagents, sha


def test_dry_run_writes_nothing_and_prints_no_config_values(tmp_path, monkeypatch, capsys):
    args, _, config, runtimes, launchagents, _ = fixture_args(tmp_path, monkeypatch)
    original = config.read_bytes()
    monkeypatch.setattr(sys, 'argv', args + ['--dry-run'])
    installer.main()
    output = capsys.readouterr().out
    assert config.read_bytes() == original
    assert not runtimes.exists() and not launchagents.exists()
    assert 'synthetic-fixture-secret' not in output
    assert 'example.invalid' not in output


def test_prepared_install_never_activates_jobs_and_uses_durable_runtime(tmp_path, monkeypatch, capsys):
    args, board, config, runtimes, launchagents, sha = fixture_args(tmp_path, monkeypatch)
    runtime = runtimes / sha
    launcher = runtime / 'integrations/codex/stdio.sh'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('# fixture launcher\n')
    (runtime / '.installed-commit').write_text(sha + '\n')
    monkeypatch.setattr(sys, 'argv', args)
    installer.main()
    assert (runtimes / 'current').resolve() == runtime
    assert tomllib.loads(config.read_text())['mcp_servers']['agent-comms']['args'][0] == str(
        runtimes / 'current/integrations/codex/stdio.sh')
    for label in ('dev.agentcomms.http', 'dev.agentcomms.dispatcher'):
        job = plistlib.loads((launchagents / (label + '.plist')).read_bytes())
        assert job['ProgramArguments'][:2] == [str(runtime / '.venv/bin/python'), str(runtime / '.venv/bin/board')]
        assert job['EnvironmentVariables']['AGENT_COMMS_HOME'] == str(board)
        assert set(job['EnvironmentVariables']) == {'HOME', 'PATH', 'AGENT_COMMS_HOME', 'PYTHONUNBUFFERED'}
        assert job['KeepAlive'] == {'SuccessfulExit': False}
        assert job['AbandonProcessGroup'] is True
        assert job['EnvironmentVariables']['PATH'].startswith('/fixture/node/bin:')
        if label.endswith('http'):
            assert job['ProgramArguments'][2:] == ['serve', '--port', '8787']
    output = capsys.readouterr().out
    assert 'synthetic-fixture-secret' not in output
    assert 'No jobs were activated' in output
