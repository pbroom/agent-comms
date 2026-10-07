// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-settings.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve, 60));
const INJECTION = '<img src=x onerror=alert(1)>';

function setting(key, type, value, source, min, max) {
  const [section, name] = key.split('.');
  return { key, section, name, type, value, source, min, max, default: value, label: name.replaceAll('_', ' ') };
}

function payloads(human) {
  const state = { me: { name: human ? 'human' : 'codex', is_human: human }, paused: false,
    threads: [], sessions: [], needs_you: [], limits: {}, authorization_grants: [],
    agents: [{ name: 'human', is_human: true }, { name: 'codex', is_human: false }],
    task_categories: ['review', 'implementation', 'tests', 'documentation'] };
  const settings = {
    settings: [
      setting('limits.lease_ttl_minutes', 'int', 30, 'board.toml', 1, 1440),
      setting('limits.daily_post_cap_per_agent', 'int', 200, 'board.toml', 1, 100000),
      setting('limits.body_max_bytes', 'int', 8192, 'board.local.toml', 256, 65536),
      setting('tasks.require_human_accept', 'bool', false, 'default', null, null),
      setting('dispatch.live_minutes', 'float', 2, 'default', 1, 120),
      setting('dispatch.max_concurrent', 'int', 2, 'board.toml', 1, 20),
    ],
    files: { writable: true, local_exists: true, local_error: null, board_toml_error: null, reload_error: null },
    paused: false,
    agents: [{ name: 'human', runtime: 'human', is_human: true }, { name: 'codex', runtime: 'codex-cli', is_human: false }],
    audit: [{ at: '2027-01-15T08:00:00+00:00', by: 'human', key: 'limits.body_max_bytes', old: 4096, new: 8192,
      file: 'board.local.toml' }],
  };
  const notifications = { deliverable: false,
    rules: [{ id: 4, events: ['needs-response'], project: INJECTION, thread_id: null, idle_minutes: null }] };
  const dispatch = { status: { running: true, pid: 4242, heartbeat_seconds_ago: 3 },
    rules: [{ id: 9, thread_id: 3, agents: ['codex'], purpose: INJECTION, launches_left: 4, max_launches: 5,
      expires_at: null, state: 'active' }],
    runs: [{ run_id: 's12-codex', agent: 'codex', thread_id: 3, status: 'exited', started_at: '2027-01-15T08:00:00+00:00', exit_code: 0 }],
    runners: { 'codex-cli': ['codex', 'exec', '{prompt}'], 'claude-code': ['claude', '-p', '{prompt}', INJECTION] },
    env: { 'codex-cli': ['CODEX_HOME'] }, worktrees: { '/repo': '/repo-dispatch' }, risky_runners: {},
    threads: [{ id: 3, title: INJECTION, project: '/repo' }, { id: 5, title: 'Docs', project: '/repo' }],
    agents: [{ name: 'claude', runtime: 'claude-code' }, { name: 'codex', runtime: 'codex-cli' }] };
  return { '/api/state': state, '/api/settings': settings, '/api/admin/notifications': notifications,
    '/api/admin/dispatch': dispatch };
}

async function setup({ human = true, url = 'http://localhost/#settings' } = {}) {
  const calls = [];
  const data = payloads(human);
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', beforeParse(win) {
    win.localStorage.setItem('agent-comms-token', 'dummy');
    win.confirm = () => true;
    win.fetch = async (u, options) => {
      calls.push({ url: u, ...options });
      if (options.method === 'GET') {
        const key = u.split('?')[0];
        if (!human && key !== '/api/state') return { ok: false, statusText: 'Forbidden', json: async () => ({ message: 'only the human' }) };
        return { ok: true, json: async () => data[key] };
      }
      return { ok: true, json: async () => ({}) };
    };
  } });
  await settle();
  return { dom, calls, document: dom.window.document, win: dom.window };
}

function input(win, node, value) {
  node.value = value;
  node.dispatchEvent(new win.Event('input', { bubbles: true }));
}
function check(win, node) {
  node.checked = true;
  node.dispatchEvent(new win.Event('change', { bubbles: true }));
}

test('settings view renders for the human only, with board text as text', async () => {
  const { dom, document, calls } = await setup();
  const view = document.querySelector('#settings');
  assert.ok(view, 'human sees the settings view');
  for (const id of ['settings-board', 'settings-limits', 'settings-notifications', 'settings-dispatch',
    'settings-runners', 'settings-agents', 'settings-audit']) assert.ok(document.getElementById(id), id);
  assert.equal(view.querySelectorAll('img').length, 0);
  assert.match(view.textContent, /<img src=x onerror/);
  assert.match(document.querySelector('#settings-limits').textContent, /board\.local\.toml/);
  assert.match(document.querySelector('#settings-limits').textContent, /board\.toml/);
  assert.match(document.querySelector('#settings-board').textContent, /default/);
  assert.match(document.querySelector('#settings-agents').textContent, /codex-cli/);
  assert.doesNotMatch(document.querySelector('#settings-agents').textContent, /token_sha256|ac_/);
  assert.ok(calls.some(c => c.url === '/api/settings'));
  // the header link toggles back to the board
  document.querySelector('#settings-link').click();
  await settle();
  assert.equal(document.querySelector('#settings'), null);
  document.querySelector('#settings-link').click();
  await settle();
  assert.ok(document.querySelector('#settings'));
  dom.window.close();

  const agent = await setup({ human: false });
  assert.equal(agent.document.querySelector('#settings'), null);
  assert.equal(agent.document.querySelector('#settings-link'), null);
  assert.ok(!agent.calls.some(c => c.url.startsWith('/api/settings') || c.url.startsWith('/api/admin')));
  agent.dom.window.close();
});

test('a numeric edit sends a PUT with only the changed key', async () => {
  const { dom, win, document, calls } = await setup();
  const cap = document.querySelector('input[data-key="limits.daily_post_cap_per_agent"]');
  assert.equal(cap.value, '200');
  input(win, cap, '150');
  document.querySelector('button[data-save="limits"]').click();
  await settle();
  const put = calls.filter(c => c.method === 'PUT');
  assert.equal(put.length, 1);
  assert.equal(put[0].url, '/api/settings');
  assert.equal(put[0].headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(put[0].body), { 'limits.daily_post_cap_per_agent': 150 });
  dom.window.close();
});

test('a fractional value for a whole-number setting is not sent', async () => {
  const { dom, win, document, calls } = await setup();
  input(win, document.querySelector('input[data-key="dispatch.max_concurrent"]'), '1.5');
  document.querySelector('button[data-save="dispatch"]').click();
  await settle();
  assert.equal(calls.filter(c => c.method === 'PUT').length, 0);
  assert.match(document.querySelector('[role=alert]').textContent, /whole number/);
  dom.window.close();
});

test('runners are read-only: no inputs, and the note says where to edit them', async () => {
  const { dom, document } = await setup();
  const runners = document.querySelector('#settings-runners');
  assert.equal(runners.querySelectorAll('input, textarea, select, button, [contenteditable]').length, 0);
  assert.match(runners.textContent, /Runner commands, env and worktrees are edited in board\.local\.toml\./);
  assert.match(runners.textContent, /codex exec \{prompt\}/);
  assert.match(runners.textContent, /CODEX_HOME/);
  assert.equal(runners.querySelectorAll('img').length, 0);
  dom.window.close();
});

test('the approve form sends thread, agents, purpose, budget and expiry', async () => {
  const { dom, win, document, calls } = await setup();
  const form = document.querySelector('.approve-form');
  const select = form.querySelector('select');
  assert.deepEqual([...select.options].map(o => o.value), ['', '3', '5']);
  select.value = '5';
  select.dispatchEvent(new win.Event('change', { bubbles: true }));
  const boxes = [...form.querySelectorAll('input[type=checkbox]')];
  assert.deepEqual(boxes.map(b => b.value), ['claude', 'codex']);   // non-human agents only
  check(win, boxes[1]);
  input(win, form.querySelector('textarea'), '  Review the docs change only  ');
  input(win, form.querySelector('input[name=max_launches]'), '5');
  input(win, form.querySelector('input[name=expires_in_hours]'), '24');
  form.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const call = calls.find(c => c.url === '/api/admin/dispatch/rules');
  assert.equal(call.method, 'POST');
  assert.deepEqual(JSON.parse(call.body), { thread_id: 5, agents: ['codex'], purpose: 'Review the docs change only',
    max_launches: 5, expires_in_hours: 24 });
  dom.window.close();
});

test('approve without expiry sends null, and revoke and stop use their endpoints', async () => {
  const { dom, win, document, calls } = await setup();
  const form = document.querySelector('.approve-form');
  const select = form.querySelector('select');
  select.value = '3';
  select.dispatchEvent(new win.Event('change', { bubbles: true }));
  check(win, form.querySelector('input[type=checkbox]'));
  input(win, form.querySelector('textarea'), 'p');
  input(win, form.querySelector('input[name=max_launches]'), '1');
  form.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(JSON.parse(calls.find(c => c.url === '/api/admin/dispatch/rules').body).expires_in_hours, null);
  [...document.querySelectorAll('#settings-dispatch button')].find(b => b.textContent === 'Revoke').click();
  await settle();
  assert.equal(calls.find(c => c.url === '/api/admin/dispatch/rules/9/revoke').method, 'POST');
  [...document.querySelectorAll('#settings-dispatch button')].find(b => b.textContent === 'Stop dispatcher').click();
  await settle();
  assert.equal(calls.find(c => c.url === '/api/admin/dispatch/stop').method, 'POST');
  dom.window.close();
});

test('notification rule form and the require_human_accept toggle send the right payloads', async () => {
  const { dom, win, document, calls } = await setup();
  const form = document.querySelector('.notify-form');
  const idle = [...form.querySelectorAll('input[type=checkbox]')].find(b => b.value === 'idle-agent');
  check(win, idle);
  input(win, form.querySelector('input[type=number]'), '20');
  const thread = form.querySelector('select');
  thread.value = '3';
  thread.dispatchEvent(new win.Event('change', { bubbles: true }));
  form.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.deepEqual(JSON.parse(calls.find(c => c.url === '/api/admin/notifications' && c.method === 'POST').body), {
    events: ['needs-response', 'to-human', 'decision', 'agent-launched', 'idle-agent'], project: null, thread_id: 3,
    idle_minutes: 20 });
  [...document.querySelectorAll('#settings-notifications button')].find(b => b.textContent === 'Remove').click();
  await settle();
  assert.ok(calls.some(c => c.url === '/api/admin/notifications/4/remove' && c.method === 'POST'));
  const toggle = document.querySelector('#require-human-accept');
  check(win, toggle);
  await settle();
  const put = calls.find(c => c.method === 'PUT');
  assert.deepEqual(JSON.parse(put.body), { 'tasks.require_human_accept': true });
  dom.window.close();
});
