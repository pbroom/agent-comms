"""Inbox invocations use local board tools, without changing global approvals or access."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tomllib

import pytest

from agent_comms import dispatch
from test_dispatch import denv, human_post, runs

ROOT = Path(__file__).resolve().parents[1]


def runner():
    return dispatch.DispatchConfig.load(ROOT / 'board.toml',local=False).runners['codex-cli']


def config(tmp_path,text):
    path = tmp_path / 'config.toml'
    path.write_text(text)
    return {'CODEX_HOME':str(tmp_path),'HOME':str(tmp_path)},path


def test_inbox_disables_connector_plugins_and_static_servers_but_retains_local_approvals(tmp_path):
    text = '''[features]
apps = true
plugins = true
[mcp_servers.agent-comms]
command = "bash"
args = ["/trusted/stdio.sh"]
[mcp_servers.pencil]
command = "pencil"
[mcp_servers."another.server"]
url = "https://example.invalid/mcp"
'''
    env,path = config(tmp_path,text)
    template = runner()
    narrowed = dispatch.with_codex_inbox_mcp(template,env)
    assert path.read_text() == text and narrowed != template
    assert template == runner()  # original runner remains intact for ordinary workstream launches
    assert ['--disable','apps','--disable','plugins'] == narrowed[narrowed.index('--disable'):narrowed.index('--disable')+4]
    overrides = dispatch._codex_overrides(narrowed)
    assert any(tomllib.loads(x).get('mcp_servers',{}).get('pencil',{}).get('enabled') is False for x in overrides)
    assert any(tomllib.loads(x).get('mcp_servers',{}).get('another.server',{}).get('enabled') is False for x in overrides)
    assert 'mcp_servers.agent-comms.required=true' in overrides
    assert not any('agent-comms' in x and '.enabled=false' in x for x in overrides)
    approvals = dispatch._codex_overrides(template)
    assert set(x for x in overrides if 'approval_mode=' in x) == set(approvals)
    # Each approval is (re)applied after the whole-table override, which would otherwise drop it.
    table = max(i for i,x in enumerate(overrides) if x.startswith('mcp_servers={'))
    assert set(approvals) <= set(overrides[table+1:])
    assert dispatch.codex_unapproved_tools(narrowed) == []
    assert '--sandbox' in narrowed and narrowed[narrowed.index('--sandbox')+1] == 'workspace-write'


def test_profile_and_runner_inline_servers_are_disabled_even_when_apps_were_explicitly_enabled(tmp_path):
    env,_ = config(tmp_path,'[mcp_servers.agent-comms]\ncommand="bash"\n')
    (tmp_path / 'triage.config.toml').write_text('[mcp_servers.profile_server]\ncommand="profile"\n')
    template = runner()
    template = template[:-1] + ['--profile','triage','--enable','apps','-c',
        'mcp_servers={inline_server={command="inline",enabled=true}}',template[-1]]
    narrowed = dispatch.with_codex_inbox_mcp(template,env)
    overrides = dispatch._codex_overrides(narrowed)
    assert any(tomllib.loads(x).get('mcp_servers',{}).get('profile_server',{}).get('enabled') is False for x in overrides)
    assert any(tomllib.loads(x).get('mcp_servers',{}).get('inline_server',{}).get('enabled') is False for x in overrides)
    assert narrowed.index('--disable') > narrowed.index('--enable')


@pytest.mark.parametrize('text',[
    '', '[mcp_servers.agent-comms]\ncommand="bash"\nenabled=false\n',
    '[mcp_servers.agent-comms]\nurl="https://connector.invalid/mcp"\n', 'malformed = [',
])
def test_missing_disabled_remote_or_unreadable_local_server_fails_closed(tmp_path,text):
    env,_ = config(tmp_path,text)
    with pytest.raises(ValueError,match='local|configuration'):
        dispatch.with_codex_inbox_mcp(runner(),env)


def test_launched_worker_receives_local_channel_rules_and_restricted_argv(denv,tmp_path,monkeypatch):
    e = denv
    cfg = tmp_path / 'codex-config'
    cfg.mkdir()
    config(cfg,'[mcp_servers.agent-comms]\ncommand="bash"\n[mcp_servers.other]\ncommand="other"\n')
    monkeypatch.setenv('CODEX_HOME',str(cfg))
    e.config.env['codex'] = ['CODEX_HOME']
    e.config.runners['codex'] = runner()
    e.board.create_inbox_policy(e.p['human'],scope='all_projects')
    post = human_post(e,['codex'])
    e.clock.advance(120); e.d.tick()
    assert len(e.spawner.calls) == 1
    call = e.spawner.calls[0]
    assert any(tomllib.loads(x).get('mcp_servers',{}).get('other',{}).get('enabled') is False for x in dispatch._codex_overrides(call['argv']))
    assert 'mcp_servers.agent-comms.required=true' in call['argv']
    assert 'local MCP server named agent-comms' in call['argv'][-1]
    assert (Path(call['cwd']) / 'AGENT_RULES.md').read_text() == (ROOT / 'AGENT_RULES.md').read_text()
    assert runs(e)[0]['request_ids'] == [post['id']]


def test_missing_local_channel_blocks_before_spending_inbox_budget(denv,tmp_path,monkeypatch):
    e = denv
    monkeypatch.setenv('CODEX_HOME',str(tmp_path / 'missing-codex-config'))
    e.config.env['codex'] = ['CODEX_HOME']
    e.config.runners['codex'] = runner()
    e.board.create_inbox_policy(e.p['human'],scope='all_projects',daily_launch_limit=1)
    post = human_post(e,['codex'])
    e.clock.advance(120); e.d.tick()
    assert not e.spawner.calls
    assert e.board.list_inbox_policies(e.p['human'])[0]['launches_left'] == 1
    assert e.board.get_post(e.p['codex'],post['id'])['requests'][0]['state'] == 'blocked'
    assert 'local agent-comms stdio' in runs(e)[0]['error']


@pytest.mark.parametrize('server_name',['other','other.with.dots'])
def test_native_codex_reads_isolated_route_configuration_without_starting_servers(tmp_path,server_name):
    codex = shutil.which('codex')
    if codex is None:
        pytest.skip('Codex CLI is not installed')
    # If the inventory command accidentally initializes a server this sentinel records it and fails.
    sentinel = tmp_path / 'server-started'
    server = tmp_path / 'never-start-server.sh'
    server.write_text('#!/bin/sh\ntouch "$1"\nexit 97\n')
    server.chmod(0o700)
    text = ('cli_auth_credentials_store="file"\nmcp_oauth_credentials_store="file"\n'
            '[mcp_servers.agent-comms]\ncommand=' + json.dumps(str(server)) + '\nargs=[' + json.dumps(str(sentinel)) + ']\n'
            '[mcp_servers.' + json.dumps(server_name) + ']\ncommand=' + json.dumps(str(server)) + '\nargs=[' + json.dumps(str(sentinel)) + ']\n')
    env,path = config(tmp_path,text)
    narrowed = dispatch.with_codex_inbox_mcp(runner(),env)
    restrictions = narrowed[narrowed.index('--disable'):-1]
    native_env = dict(env,PATH=os.environ.get('PATH',''),TMPDIR=str(tmp_path))
    result = subprocess.run([codex,*restrictions,'mcp','list','--json'],env=native_env,
                            capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    # Inspect configuration behavior through Codex itself; do not mirror the emitted override strings.
    inventory = {entry['name']:entry['enabled'] for entry in json.loads(result.stdout)}
    assert inventory['agent-comms'] is True
    assert inventory[server_name] is False
    assert {name for name,enabled in inventory.items() if enabled} == {'agent-comms'}
    assert not sentinel.exists()
    assert path.read_text() == text


def test_native_codex_keeps_runner_server_overrides_after_disabling_other_servers(tmp_path):
    codex = shutil.which('codex')
    if codex is None:
        pytest.skip('Codex CLI is not installed')
    text = ('cli_auth_credentials_store="file"\nmcp_oauth_credentials_store="file"\n'
            '[mcp_servers.agent-comms]\ncommand="/bin/false"\n[mcp_servers.other]\ncommand="/bin/false"\n')
    env,_ = config(tmp_path,text)
    template = runner()
    # Stands in for the runner's per-tool approvals: dotted keys set before the inbox overrides.
    template = template[:-1] + ['-c','mcp_servers.agent-comms.tool_timeout_sec=77',template[-1]]
    narrowed = dispatch.with_codex_inbox_mcp(template,env)
    flags = []
    for i,x in enumerate(narrowed[:-1]):
        if x in ('-c','--disable'):
            flags += [x,narrowed[i+1]]
    native_env = dict(env,PATH=os.environ.get('PATH',''),TMPDIR=str(tmp_path))
    result = subprocess.run([codex,*flags,'mcp','get','agent-comms','--json'],env=native_env,
                            capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    server = json.loads(result.stdout)
    assert server['enabled'] is True and server['tool_timeout_sec'] == 77
