const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve, 50));
const ok = value => ({ ok: true, status: 200, json: async () => value });
const injection = '<img src=x onerror=alert(1)>';

async function setup({ human = true, policies = [], fail = false } = {}) {
  const calls = [];
  const state = { me: { name: human ? 'human' : 'codex', is_human: human }, paused: false,
    threads: [], sessions: [], agents: [], needs_you: [], limits: {}, authorization_grants: [], task_categories: [] };
  const data = {
    '/api/whoami': state.me, '/api/state': state,
    '/api/settings': { settings: [], files: { writable: true }, audit: [], agents: [] },
    '/api/admin/notifications': { deliverable: false, rules: [] },
    '/api/admin/dispatch': { status: { running: false }, threads: [], agents: [], rules: [], runs: [],
      inbox_policies: policies },
    '/api/web-sessions': { sessions: [] }, '/api/browser/gates': { gates: [] },
  };
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/#settings', runScripts: 'dangerously', beforeParse(win) {
    win.fetch = async (url, options = {}) => {
      calls.push({ url, ...options });
      if (options.method === 'GET') return ok(data[url.split('?')[0]]);
      if (fail) return { ok: false, status: 400, json: async () => ({ message: 'Policy rejected' }) };
      return ok({});
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, calls };
}
function edit(win, node, value) {
  node.value = value;
  node.dispatchEvent(new win.Event('input', { bubbles: true }));
}
function select(win, node, value) {
  node.value = value;
  node.dispatchEvent(new win.Event('change', { bubbles: true }));
}
function submit(page) {
  page.document.querySelector('.inbox-policy-form').dispatchEvent(new page.win.Event('submit', { bubbles: true, cancelable: true }));
}
function mutations(page) { return page.calls.filter(c => c.method !== 'GET'); }
function policy(id, state = 'active', extra = {}) {
  return { id, state, active: state === 'active', scope: 'all_projects', project: null, agent: 'codex',
    purpose: 'Delivery and triage only', daily_launch_limit: 100, launches_left: state === 'exhausted' ? 0 : 98,
    reset_at: '2027-01-16T00:00:00Z', include_existing: false, pickup_grace_seconds: 120,
    revoked_at: state === 'revoked' ? '2027-01-15T12:00:00Z' : null, revoked_by: state === 'revoked' ? 'human' : null,
    ...extra };
}

test('default pickup explicitly covers all projects without including old queued requests', async () => {
  const page = await setup();
  try {
    const section = page.document.querySelector('#settings-inbox-pickup');
    assert.match(section.textContent, /Existing work approvals and permissions still apply/);
    assert.equal(section.querySelector('[name=inbox_scope]').value, 'all_projects');
    assert.equal(section.querySelector('[name=include_existing]').checked, false);
    assert.equal(section.querySelector('[name=inbox_project]'), null);
    assert.equal(mutations(page).length, 0, 'viewing settings never enables a policy');
    submit(page); await settle();
    const call = mutations(page)[0];
    assert.equal(call.url, '/api/admin/dispatch/inbox-policies');
    assert.equal(call.method, 'POST');
    assert.deepEqual(JSON.parse(call.body), { scope: 'all_projects', project: null, daily_launch_limit: 100,
      include_existing: false, pickup_grace_seconds: 120 });
  } finally { page.dom.window.close(); }
});

test('project pickup needs an absolute path and never turns a missing path into all-project scope', async () => {
  const page = await setup();
  try {
    select(page.win, page.document.querySelector('[name=inbox_scope]'), 'project');
    assert.equal(page.document.querySelector('#settings-inbox-pickup details').open, true,
      'choosing a scope keeps the form open');
    assert.equal(page.document.activeElement.name, 'inbox_scope', 'scope selection retains keyboard focus');
    edit(page.win, page.document.querySelector('[name=inbox_project]'), 'relative/path');
    submit(page); await settle();
    assert.equal(mutations(page).length, 0);
    assert.match(page.document.querySelector('[role=alert]').textContent, /absolute path/);
    edit(page.win, page.document.querySelector('[name=inbox_project]'), '  /repo/app  ');
    submit(page); await settle();
    assert.deepEqual(JSON.parse(mutations(page)[0].body), { scope: 'project', project: '/repo/app',
      daily_launch_limit: 100, include_existing: false, pickup_grace_seconds: 120 });
  } finally { page.dom.window.close(); }
});

test('existing requests are an explicit opt-in, and successful create clears that opt-in', async () => {
  const page = await setup();
  try {
    const box = page.document.querySelector('[name=include_existing]');
    box.checked = true; box.dispatchEvent(new page.win.Event('change', { bubbles: true }));
    edit(page.win, page.document.querySelector('[name=daily_launch_limit]'), '12');
    edit(page.win, page.document.querySelector('[name=pickup_grace_seconds]'), '60');
    submit(page); await settle();
    assert.deepEqual(JSON.parse(mutations(page)[0].body), { scope: 'all_projects', project: null,
      daily_launch_limit: 12, include_existing: true, pickup_grace_seconds: 60 });
    assert.equal(page.document.querySelector('[name=include_existing]').checked, false);
    assert.equal(page.document.querySelector('[name=daily_launch_limit]').value, '100');
  } finally { page.dom.window.close(); }
});

test('failed creation preserves scope and the unsaved fields with a visible server error', async () => {
  const page = await setup({ fail: true });
  try {
    select(page.win, page.document.querySelector('[name=inbox_scope]'), 'project');
    edit(page.win, page.document.querySelector('[name=inbox_project]'), '/repo/draft');
    edit(page.win, page.document.querySelector('[name=daily_launch_limit]'), '9');
    submit(page); await settle();
    assert.match(page.document.querySelector('[role=alert]').textContent, /Policy rejected/);
    assert.equal(page.document.querySelector('[name=inbox_scope]').value, 'project');
    assert.equal(page.document.querySelector('[name=inbox_project]').value, '/repo/draft');
    assert.equal(page.document.querySelector('[name=daily_launch_limit]').value, '9');
  } finally { page.dom.window.close(); }
});

test('invalid budgets and grace periods do not reach the mutation endpoint', async () => {
  for (const [name, value] of [['daily_launch_limit', '101'], ['daily_launch_limit', '1.5'],
    ['daily_launch_limit', '0'], ['pickup_grace_seconds', '29'], ['pickup_grace_seconds', '3601'],
    ['pickup_grace_seconds', '30.5']]) {
    const page = await setup();
    try {
      edit(page.win, page.document.querySelector(`[name=${name}]`), value);
      submit(page); await settle();
      assert.equal(mutations(page).length, 0, `${name}=${value}`);
      assert.match(page.document.querySelector('[role=alert]').textContent, /whole number/);
    } finally { page.dom.window.close(); }
  }
});

test('enabled and exhausted policies remain revocable; revoked history stays quiet and escapes text', async () => {
  const page = await setup({ policies: [policy(1, 'revoked'), policy(2, 'exhausted'),
    policy(3, 'active', { scope: 'project', project: injection, purpose: injection })] });
  try {
    const section = page.document.querySelector('#settings-inbox-pickup');
    assert.deepEqual([...section.querySelectorAll('[data-inbox-policy]')].map(n => n.dataset.inboxPolicy), ['2', '3', '1']);
    assert.match(section.textContent, /0\/100 launches left today · resets/);
    assert.equal(section.querySelectorAll('img').length, 0);
    assert.match(section.textContent, /<img src=x onerror/);
    assert.equal(section.querySelector('[data-inbox-policy="1"] button'), null);
    assert.ok(section.querySelector('[data-inbox-policy="1"]').classList.contains('muted'));
    section.querySelector('[data-inbox-policy="2"] button').click(); await settle();
    assert.equal(mutations(page)[0].url, '/api/admin/dispatch/inbox-policies/2/revoke');
    assert.equal(mutations(page)[0].method, 'POST');
  } finally { page.dom.window.close(); }
});

test('agent sessions cannot see or load inbox policy administration', async () => {
  const page = await setup({ human: false });
  try {
    assert.equal(page.document.querySelector('#settings-inbox-pickup'), null);
    assert.equal(page.win.renderInboxPickup(), null);
    assert.equal(page.calls.some(c => c.url.startsWith('/api/admin/')), false);
    assert.equal(mutations(page).length, 0);
  } finally { page.dom.window.close(); }
});
