// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-settings.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 60) => new Promise(resolve => setTimeout(resolve, ms));
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
      setting('tasks.auto_recover_stalled_work', 'bool', true, 'default', null, null),
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
  const browsers = { session_days: 30, session_max_days: 90, sessions: [
    { id: 'aaaaaaaaaaaaaaaa', label: 'Safari on macOS', created_at: '2027-01-15T08:00:00+00:00',
      last_seen: '2027-01-15T08:00:00+00:00', expires_at: '2027-02-14T08:00:00+00:00', current: true },
    { id: 'bbbbbbbbbbbbbbbb', label: INJECTION, created_at: '2027-01-14T08:00:00+00:00',
      last_seen: '2027-01-14T08:00:00+00:00', expires_at: '2027-02-13T08:00:00+00:00', current: false }] };
  return { '/api/state': state, '/api/settings': settings, '/api/admin/notifications': notifications,
    '/api/admin/dispatch': dispatch, '/api/web-sessions': browsers, '/api/whoami': state.me };
}

const forbidden = { ok: false, status: 403, statusText: 'Forbidden', json: async () => ({ message: 'only the human' }) };

// The human is signed in with the session cookie; an agent's token (which cannot get a sign-in link) is used in
// memory after the login-link exchange is refused.
async function setup({ human = true, url = 'http://localhost/#settings' } = {}) {
  const calls = [];
  const data = payloads(human);
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', beforeParse(win) {
    if (!human) win.localStorage.setItem('agent-comms-token', 'dummy');
    win.confirm = () => true;
    win.fetch = async (u, options) => {
      calls.push({ url: u, ...options });
      if (u === '/api/login-links') return forbidden;
      if (options.method === 'GET') {
        const key = u.split('?')[0];
        if (!human && key !== '/api/state') return forbidden;
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

test('Reset drops the unsaved draft, so the next Save does not write the override back', async () => {
  const { dom, win, document, calls } = await setup();
  input(win, document.querySelector('input[data-key="limits.body_max_bytes"]'), '9000');
  const row = document.querySelector('input[data-key="limits.body_max_bytes"]').closest('tr');
  [...row.querySelectorAll('button')].find(b => b.textContent === 'Reset').click();
  await settle();
  const puts = () => calls.filter(c => c.method === 'PUT').map(c => JSON.parse(c.body));
  assert.deepEqual(puts(), [{ 'limits.body_max_bytes': null }]);
  assert.notEqual(document.querySelector('input[data-key="limits.body_max_bytes"]').value, '9000', 'the draft is gone');
  document.querySelector('button[data-save="limits"]').click();
  await settle();
  assert.deepEqual(puts(), [{ 'limits.body_max_bytes': null }], 'Save sends nothing for the reset key');
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

test('automatic recovery is an on/off board setting the human saves with one click', async () => {
  const { dom, document, calls, win } = await setup();
  const toggle = document.querySelector('#auto-recover-stalled-work');
  assert.ok(toggle, 'shown on the Board card like require_human_accept');
  assert.equal(toggle.checked, true);
  assert.ok(document.querySelector('#require-human-accept'));
  assert.match(document.querySelector('#settings-board').textContent, /tasks\.auto_recover_stalled_work/);
  toggle.checked = false;
  toggle.dispatchEvent(new win.Event('change', { bubbles: true }));
  await settle();
  const put = calls.find(c => c.method === 'PUT');
  assert.deepEqual(JSON.parse(put.body), { 'tasks.auto_recover_stalled_work': false });
  dom.window.close();
});

test('signed-in browsers: listed as text, revoke one, sign out all', async () => {
  const { dom, document, calls } = await setup();
  const section = document.querySelector('#settings-browsers');
  assert.ok(section);
  assert.equal(section.querySelectorAll('img').length, 0);
  assert.match(section.textContent, /Safari on macOS/);
  assert.match(section.textContent, /this browser/);
  assert.match(section.textContent, /<img src=x onerror/);
  assert.match(section.textContent, /30 days after its last use/);
  // Both take a second click (the first arms the button; see dashboard-confirms.test.cjs).
  section.querySelector('tr[data-session="bbbbbbbbbbbbbbbb"] button').click();
  await settle(520);   // the confirming click comes at least 500 ms after arming
  document.querySelector('#settings-browsers tr[data-session="bbbbbbbbbbbbbbbb"] button').click();
  await settle();
  assert.ok(calls.some(c => c.url === '/api/web-sessions/bbbbbbbbbbbbbbbb/revoke' && c.method === 'POST'));
  document.querySelector('#sign-out-all').click();
  await settle(520);   // the confirming click comes at least 500 ms after arming
  document.querySelector('#sign-out-all').click();
  await settle();
  assert.ok(calls.some(c => c.url === '/api/web-sessions/revoke-all' && c.method === 'POST'));
  // every request carries the CSRF header, and the human's requests carry no token
  for (const c of calls) {
    assert.equal(c.headers['X-Board-Request'], '1', c.url);
    assert.equal(c.headers.Authorization, undefined, c.url);
  }
  dom.window.close();
});
