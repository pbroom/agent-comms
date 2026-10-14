"""Both CLI approval modes validate before loading any user's board state."""
import json

import pytest

from agent_comms import cli, dispatch
from agent_comms.core import INBOX_PURPOSE


@pytest.fixture
def run_cli(env, monkeypatch):
    monkeypatch.setattr(dispatch.DispatchConfig, 'load', lambda *args, **kwargs: dispatch.DispatchConfig())
    monkeypatch.setattr(dispatch, 'loop_status', lambda *args: {'running': False})
    monkeypatch.setattr(cli, '_run', lambda args, out: cli._dispatch_cmd(args, out, env.board, env.p['human']))
    return cli.main


def test_global_inbox_allow_has_fixed_codex_scope_and_no_execution_grant(run_cli, env, capsys):
    run_cli(['--json', 'dispatch', 'allow', '--all-new-threads', '--agents', 'codex',
             '--daily-limit', '5', '--include-existing'])
    policy = json.loads(capsys.readouterr().out)
    assert policy['scope'] == 'all_projects' and policy['project'] is None
    assert policy['agents'] == ['codex'] and policy['purpose'] == INBOX_PURPOSE
    assert policy['daily_launch_limit'] == 5 and policy['include_existing']
    assert env.board.list_dispatch_rules(env.p['human']) == []
    assert env.board.list_grants(env.p['human']) == []


def test_global_policy_defaults_list_and_revoke_are_clear(run_cli, env, capsys):
    run_cli(['dispatch', 'allow', '--all-new-threads', '--agents', 'codex'])
    text = capsys.readouterr().out
    assert 'all projects, Codex delivery only' in text and 'thread None' not in text
    policy = env.board.list_inbox_policies(env.p['human'])[0]
    assert policy['daily_launch_limit'] == 100 and not policy['include_existing']
    run_cli(['--json', 'dispatch', 'list'])
    assert json.loads(capsys.readouterr().out)['inbox_policies'][0]['id'] == policy['id']
    run_cli(['dispatch', 'revoke', str(policy['id'])])
    text = capsys.readouterr().out
    assert 'inbox policy' in text and 'thread None' not in text
    run_cli(['--json', 'dispatch', 'list', '--all'])
    assert json.loads(capsys.readouterr().out)['inbox_policies'][0]['state'] == 'revoked'


def test_normal_workstream_cli_preserves_scope_and_budget(run_cli, env, capsys):
    tid = env.thread()
    run_cli(['--json', 'dispatch', 'allow', '--thread', str(tid), '--agents', 'codex,claude',
             '--purpose', 'Implement and review the approved fix', '--max-launches', '3',
             '--expires-in-hours', '2'])
    rule = json.loads(capsys.readouterr().out)
    assert rule['thread_id'] == tid and rule['agents'] == ['codex', 'claude']
    assert rule['max_launches'] == 3 and rule['expires_at']
    assert env.board.list_inbox_policies(env.p['human']) == []


@pytest.mark.parametrize('options', [
    ['--agents', 'codex'],
    ['--agents', 'codex', '--thread', '1'],
    ['--agents', 'codex', '--thread', '1', '--purpose', 'scope'],
    ['--agents', 'codex', '--purpose', 'scope', '--max-launches', '1'],
    ['--agents', 'codex', '--thread', '1', '--purpose', 'scope', '--max-launches', '1', '--daily-limit', '2'],
    ['--agents', 'codex', '--thread', '1', '--purpose', 'scope', '--max-launches', '1', '--include-existing'],
    ['--all-new-threads', '--agents', 'claude'],
    ['--all-new-threads', '--agents', 'codex,claude'],
    ['--all-new-threads', '--agents', 'codex,codex'],
    ['--all-new-threads', '--agents', 'codex', '--purpose', 'arbitrary execution'],
    ['--all-new-threads', '--agents', 'codex', '--thread', '1'],
    ['--all-new-threads', '--agents', 'codex', '--max-launches', '2'],
    ['--all-new-threads', '--agents', 'codex', '--expires-in-hours', '2'],
    ['--all-new-threads', '--agents', 'codex', '--daily-limit', '0'],
    ['--all-new-threads', '--agents', 'codex', '--daily-limit', '101'],
])
def test_cli_invalid_modes_fail_before_opening_board(monkeypatch, options):
    def unexpected_run(*args):
        pytest.fail('invalid approval options reached board/configuration loading')
    monkeypatch.setattr(cli, '_run', unexpected_run)
    with pytest.raises(SystemExit) as exc:
        cli.main(['dispatch', 'allow', *options])
    assert exc.value.code == 2
