"""Exact answer pickup crosses the real authenticated HTTP boundary."""
from fastapi.testclient import TestClient
from agent_comms.api import create_app
from agent_comms import requests, summary
from agent_comms.dispatch import DispatchConfig
from conftest import ASK
from test_issues_api import headers


def test_http_answer_link_is_human_only_and_snapshot_ignores_read_cursors(env):
    client = TestClient(create_app(env.board))
    tid = env.thread()
    source = env.post('codex', tid, 'Choose', type='question', needs_response=True, decision_question=ASK)
    payload = dict(thread_id=tid, type='status', body='Proceed', to=['codex'], answer_to=[source['id']])
    assert client.post('/api/posts', headers=headers(env), json=payload).status_code == 403
    response = client.post('/api/posts', headers=headers(env, 'human'), json=payload)
    assert response.status_code == 200, response.text
    answer = response.json()
    snapshot = client.get('/api/state', headers=headers(env, 'human')).json()
    assert snapshot['needs_you'] == []
    assert snapshot['threads'][0]['pickup']['waiting'][0]['post_id'] == answer['id']
    env.board.read_updates(env.p['codex'],env.sid['codex'],only='all')
    assert env.board.snapshot(env.p['human'])['threads'][0]['pickup']['waiting']
    path = f"/api/posts/{answer['id']}/request-progress"
    started = client.post(path,headers=headers(env),json=dict(recipient='codex',state='started'))
    assert started.status_code == 200, started.text
    projected = client.get('/api/state',headers=headers(env,'human')).json()['threads'][0]['pickup']
    assert not projected['waiting'] and projected['processing'][0]['assigned_session'] == env.sid['codex']


def test_summary_pickup_is_distinct_from_human_read_history(env):
    tid = env.thread()
    post = env.post('human',tid,'Work',type='request',to=['codex'])
    s = summary.human_summary(env.board,env.p['human'],DispatchConfig())
    assert s['agent_pickup'] == dict(waiting=1,overdue=0,processing=0,blocked=0)
    requests.progress(env.board,env.p['codex'],env.sid['codex'],post['id'],'codex','started')
    assert summary.human_summary(env.board,env.p['human'],DispatchConfig())['agent_pickup']['processing'] == 1
