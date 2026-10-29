"""Scoped Claude runs: an opted-in project launches Claude straight into its work with scoped tools; a denied tool is
the agent's to report as a blocker (no separate tool-preflight run)."""
import pytest

from agent_comms import runner_preflight as rp, requests
from agent_comms.dispatch import DispatchConfig
from test_dispatch import denv, allow, human_post, runs  # noqa: F401  (denv is a fixture)
from conftest import PROJECT


def configure(env, project=PROJECT):
    env.d.config.runners['claude'] = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits",
                                      "--allowedTools=Bash,Read"]
    env.d.config.claude_tool_projects = [project]
    allow(env, agents=['claude'], max_launches=1)
    post = human_post(env, ['claude'])
    env.d.tick()
    run = next(r for r in env.d.running.values() if r.agent == 'claude')
    record = env.d._get(env.d.RUN_PREFIX + run.run_id)
    return post, run, record


def test_scoped_override_preserves_unrelated_runner_and_denies():
    original = ['claude', '-p', '{prompt}', '--permission-mode', 'acceptEdits', '--allowedTools=Bash',
                '--disallowedTools=Bash(git push *)']
    scoped = rp.scoped_template(original)
    assert original[-2] == '--allowedTools=Bash'
    assert '--allowedTools=Bash' not in scoped
    assert any(x.startswith('--disallowedTools=') and 'Bash(git push *)' in x for x in scoped)
    assert 'dontAsk' in scoped
    assert not any(x.startswith('--setting-sources') for x in scoped)
    assert 'Bash' not in rp.ALLOWED
    assert not any('git *' in x or 'gh *' in x for x in rp.ALLOWED)
    assert DispatchConfig.from_dict({}).claude_tool_projects == []
    with pytest.raises(ValueError):
        DispatchConfig.from_dict({'claude_tool_projects': ['relative']})
    with pytest.raises(ValueError):
        rp.scoped_template(original + ['--resume', 'old'])


def test_a_scoped_run_goes_straight_to_work(denv):
    post, run, record = configure(denv)
    [call] = denv.spawner.calls
    argv = call['argv']
    assert 'dontAsk' in argv and any(a.startswith('--allowedTools=') and 'Edit(./**)' in a for a in argv)
    assert '--session-id' not in argv and '--max-turns' not in argv
    assert rp.DENIED_TOOL_INSTRUCTION in argv[argv.index('-p') + 1]
    assert 'tool_preflight' not in record and record['status'] == 'running'
    sid = denv.board.register_session(denv.p['claude'], PROJECT, dispatch_run_id=run.run_id)['session_id']
    requests.progress(denv.board, denv.p['claude'], sid, post['id'], 'claude', 'started')
    assert requests.for_post(denv.board, denv.board.conn.execute(
        'SELECT * FROM posts WHERE id=?', (post['id'],)).fetchone())[0]['state'] == 'started'


def test_a_scoped_run_that_reports_a_denied_tool_stays_blocked(denv):
    post, run, record = configure(denv)
    sid = denv.board.register_session(denv.p['claude'], PROJECT, dispatch_run_id=run.run_id)['session_id']
    requests.progress(denv.board, denv.p['claude'], sid, post['id'], 'claude', 'blocked',
                      reason='Bash(uv run pytest *) was denied')
    run.child.code = 0
    denv.d.tick()
    row = denv.board.get_post(denv.p['human'], post['id'])['requests'][0]
    assert row['state'] == 'blocked' and 'denied' in row['reason']
    assert len(denv.spawner.calls) == 1


def test_scoped_runner_can_check_merges():
    for tool in ('Bash(git fetch origin)', 'Bash(git fetch origin main)', 'Bash(gh pr view *)', 'Bash(gh pr list *)', 'Bash(gh pr list)'):
        assert tool in rp.ALLOWED
    assert 'Bash(git fetch)' not in rp.ALLOWED
    assert not any(t.startswith(('Bash(gh pr merge', 'Bash(gh api', 'Bash(git push')) for t in rp.ALLOWED)
