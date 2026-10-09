// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-browser-gates.test.cjs
// Settings > Browser permission gates: the human sees each sticky denied gate and can record a permission change
// ("Allow again") through POST /api/browser/permission-change, behind the click-twice confirm. Never
// window.confirm/alert/prompt.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 60) => new Promise(resolve => setTimeout(resolve, ms));
const INJECTION = '<img src=x onerror=alert(1)>';

const GATE = { project: '/work/lab', origin: 'http://127.0.0.1:5185', epoch: 3, created_by: 'codex',
  failure: 'policy_denied', recorded_by: 'codex', recorded_at: '2026-01-15T08:00:00+00:00',
  reason: 'browser_take_screenshot refused ' + INJECTION };

function payloads(gates) {
  const state = { me: { name: 'human', is_human: true }, paused: false, threads: [], sessions: [], needs_you: [],
    limits: {}, authorization_grants: [], agents: [{ name: 'human', is_human: true }], task_categories: [] };
  const settings = { settings: [], paused: false, audit: [], agents: [],
    files: { writable: true, local_exists: true, local_error: null, board_toml_error: null, reload_error: null } };
  const dispatch = { status: { running: false }, rules: [], runs: [], runners: {}, env: {}, worktrees: {},
    risky_runners: {}, threads: [], agents: [] };
  return { '/api/state': state, '/api/whoami': state.me, '/api/settings': settings,
    '/api/admin/notifications': { deliverable: false, rules: [] }, '/api/admin/dispatch': dispatch,
    '/api/web-sessions': { session_days: 30, session_max_days: 90, sessions: [] },
    '/api/browser/gates': gates === undefined ? undefined : { gates } };
}

async function setup(gates) {
  const calls = [], dialogs = [];
  const data = payloads(gates);
  const dom = new JSDOM(html, { url: 'http://localhost/#settings', runScripts: 'dangerously', beforeParse(win) {
    for (const name of ['confirm', 'alert', 'prompt']) win[name] = text => { dialogs.push([name, text]); return false; };
    win.fetch = async (u, options) => {
      calls.push({ url: u, ...options });
      if (u === '/api/login-links') return { ok: false, status: 403, statusText: 'Forbidden', json: async () => ({}) };
      if (options.method === 'GET') {
        const key = u.split('?')[0];
        if (data[key] === undefined) return { ok: false, status: 500, statusText: 'Error', json: async () => ({ message: 'boom' }) };
        return { ok: true, json: async () => data[key] };
      }
      if (u === '/api/browser/permission-change') data['/api/browser/gates'] = { gates: [] };
      return { ok: true, json: async () => ({ status: 'fresh_probe_required', permission_granted_by_board: false }) };
    };
  } });
  await settle();
  return { dom, calls, dialogs, win: dom.window, document: dom.window.document };
}

function input(win, node, value) {
  node.value = value;
  node.dispatchEvent(new win.Event('input', { bubbles: true }));
}

test('a sticky gate is listed with project, origin, reason, who and when, as text', async () => {
  const { dom, document } = await setup([GATE]);
  const section = document.querySelector('#settings-browser-gates');
  assert.ok(section, 'the human sees the gate section');
  const gate = section.querySelector('[data-gate="http://127.0.0.1:5185"]');
  assert.ok(gate);
  assert.match(gate.textContent, /http:\/\/127\.0\.0\.1:5185/);
  assert.match(gate.textContent, /\/work\/lab/);
  assert.match(gate.textContent, /policy_denied/);
  assert.match(gate.textContent, /Recorded by codex · \d+d ago/);
  assert.match(gate.textContent, /<img src=x onerror/, 'agent-written reason shown as text');
  assert.equal(section.querySelectorAll('img').length, 0);
  // What allowing again means, in the UI itself.
  assert.match(section.textContent, /records a human permission change/);
  assert.match(section.textContent, /changes no browser or host setting/);
  assert.match(section.textContent, /fresh probe/);
  dom.window.close();
});

test('Allow again needs a note, takes a second click, and posts the recorded change with the gate epoch', async () => {
  const { dom, win, document, calls, dialogs } = await setup([GATE]);
  const button = () => document.querySelector('#settings-browser-gates button[data-label="Allow again"]');
  assert.ok(button().disabled, 'disabled until the human says what they changed or checked');
  const note = document.querySelector('#settings-browser-gates input');
  assert.equal(document.querySelector(`label[for="${note.id}"]`).textContent,
    'What you changed or checked (recorded with the change)');
  input(win, note, '  Checked: Playwright refused a screenshot path, not a host denial  ');
  assert.equal(button().disabled, false);
  button().click();
  await settle();
  assert.equal(button().textContent, 'Confirm allow again?', 'the first click only arms');
  assert.ok(button().classList.contains('btn-confirm'));
  assert.match(document.getElementById('confirm-live').textContent, /records a human permission change/);
  assert.ok(!calls.some(c => c.method === 'POST'), 'nothing is sent on the first click');
  await settle(520);
  button().click();
  await settle();
  const posts = calls.filter(c => c.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/browser/permission-change');
  assert.deepEqual(JSON.parse(posts[0].body), { project: '/work/lab', target_url: 'http://127.0.0.1:5185',
    evidence: 'Checked: Playwright refused a screenshot path, not a host denial', expected_epoch: 3 });
  assert.deepEqual(dialogs, [], 'no window.confirm/alert/prompt');
  // The list reloads: the gate is gone.
  assert.match(document.querySelector('#settings-browser-gates').textContent, /No browser origin is blocked/);
  dom.window.close();
});

test('clearing the note disables the button again; no gates and a failed load each say so', async () => {
  const one = await setup([GATE]);
  const note = one.document.querySelector('#settings-browser-gates input');
  input(one.win, note, 'changed it');
  input(one.win, note, '   ');
  assert.ok(one.document.querySelector('#settings-browser-gates button[data-label="Allow again"]').disabled);
  one.dom.window.close();

  const none = await setup([]);
  assert.match(none.document.querySelector('#settings-browser-gates').textContent, /No browser origin is blocked/);
  assert.equal(none.document.querySelector('#settings-browser-gates button'), null);
  none.dom.window.close();

  const failed = await setup(undefined);
  assert.match(failed.document.querySelector('#settings-browser-gates').textContent, /Could not load/);
  assert.ok(failed.document.querySelector('#settings-board'), 'the rest of Settings still renders');
  failed.dom.window.close();
});
