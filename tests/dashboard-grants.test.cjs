// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-grants.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve, 20));

async function setup(human = true, fail = false) {
  const calls = [];
  const state = { me: { name: human ? 'human' : 'codex', is_human: human }, paused: false,
    threads: [], sessions: [], needs_you: [], limits: {},
    agents: [{ name: 'human', is_human: true }, { name: 'codex', is_human: false }],
    task_categories: ['review', 'implementation', 'tests', 'documentation'],
    authorization_grants: [{ id: 7, category: 'review', project: '/repo', agents: ['codex'],
      purpose: '<img src=x onerror=alert(1)>', active: true, expires_at: null, revoked_at: null }] };
  const dom = new JSDOM(html, { url: 'http://localhost/#token=dummy', runScripts: 'dangerously', beforeParse(win) {
    win.confirm = () => true;
    win.fetch = async (url, options) => {
      calls.push({ url, ...options });
      if (options.method === 'GET') return { ok: true, json: async () => state };
      if (fail) return { ok: false, statusText: 'Bad request', json: async () => ({ message: 'Scope rejected' }) };
      return { ok: true, json: async () => ({}) };
    };
  } });
  await settle();
  return { dom, calls, document: dom.window.document };
}
function change(win, input, value) {
  input.value = value;
  input.dispatchEvent(new win.Event('input', { bubbles: true }));
}
function fill(dom) {
  const form = dom.window.document.querySelector('.grant-form');
  change(dom.window, form.querySelector('input:not([type])'), '/repo');
  change(dom.window, form.querySelector('textarea'), 'Review changes only');
  const agent = form.querySelector('input[type=checkbox]');
  agent.checked = true; agent.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  return form;
}

test('only human sees grant controls and content stays text', async () => {
  const { dom, document } = await setup();
  assert.ok(document.querySelector('#category-approvals'));
  assert.equal(document.querySelectorAll('#category-approvals img').length, 0);
  assert.match(document.querySelector('#category-approvals').textContent, /<img src=x/);
  assert.equal(document.querySelectorAll('.agent-option').length, 1);
  dom.window.close();
  const agent = await setup(false);
  assert.equal(agent.document.querySelector('#category-approvals'), null);
  agent.dom.window.close();
});

test('create sends explicit scope, agents, category and optional expiry', async () => {
  const { dom, calls } = await setup();
  const form = fill(dom);
  const expiry = new Date(Date.now() + 86400000).toISOString().slice(0, 16);
  change(dom.window, form.querySelector('[type=datetime-local]'), expiry);
  form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const call = calls.find(call => call.url === '/api/admin/grants');
  assert.deepEqual(JSON.parse(call.body), { project: '/repo', category: 'review', agents: ['codex'],
    purpose: 'Review changes only', expires_at: Date.parse(expiry) / 1000 });
  assert.equal(call.headers['Content-Type'], 'application/json');
  dom.window.close();
});

test('failed create keeps draft and visible error after refresh', async () => {
  const { dom, document } = await setup(true, true);
  fill(dom).dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(document.querySelector('[role=alert]').textContent, 'Scope rejected');
  assert.equal(document.querySelector('.grant-form textarea').value, 'Review changes only');
  assert.equal(document.querySelector('.grant-form input[type=checkbox]').checked, true);
  dom.window.close();
});

test('revoke uses the human endpoint without modifying grant payload', async () => {
  const { dom, document, calls } = await setup();
  [...document.querySelectorAll('#category-approvals button')].find(button => button.textContent === 'Revoke').click();
  await settle();
  const call = calls.find(call => call.url === '/api/admin/grants/7/revoke');
  assert.equal(call.method, 'POST');
  assert.equal(call.body, undefined);
  dom.window.close();
});


test('active lease is not shown as permission after a grant expires', async () => {
  const { dom } = await setup();
  const table = dom.window.renderTasks([{ id: 9, title: 'Review patch', category: 'review',
    acceptance: '', intends_files: [], events: [], status: 'working', owner_agent: 'codex', owner_session: 2,
    lease_state: 'active', lease_seconds_left: 900, owner_may_work: false,
    authorization: { source: 'grant', grant_id: 7, active: false } }], true);
  assert.match(table.textContent, /Category: review/);
  assert.match(table.textContent, /Authorization: category approval #7/);
  assert.match(table.textContent, /Approval expired or revoked. Work must stop./);
  const lease = [...table.querySelectorAll('.chip')].find(node => node.textContent.startsWith('lease '));
  assert.ok(lease.classList.contains('warn'));
  assert.ok(!lease.classList.contains('ok'));
  dom.window.close();
});
