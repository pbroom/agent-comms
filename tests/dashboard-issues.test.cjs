// Shared issue interactions use a mocked API; authorization is enforced by the backend tests.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(r => setTimeout(r, 40));
const ok = body => ({ ok: true, status: 200, json: async () => body });
const at = new Date().toISOString();
const poison = '<img src=x onerror="window.pwned=true">';
const issue = (id = 1) => ({ id, title: 'Git metadata blocker ' + poison, body: 'Shared evidence', status: 'open', needs_human: true,
  created_by: 'codex', created_at: at, updated_at: at, comments: [], decisions: [], resolution: null,
  links: [{ thread_id: 10, post_id: 100, project: '/repo/one', title: 'Detector fix' },
    { thread_id: 20, post_id: 200, project: '/repo/two', title: 'Review fix' }] });
async function setup({ issues = [issue()], human = true, hash = '#thread-10', pendingIssues = [], reply } = {}) {
  const calls = [];
  const me = { name: human ? 'human' : 'codex', is_human: human };
  const post = { id: 100, thread_id: 10, seq: 1, agent: 'codex', session_id: 1, type: 'question', body: 'Blocked',
    to: [], needs_response: true, refs: [], sealed: false, created_at: at };
  const thread = { id: 10, title: 'Detector fix', project: '/repo/one', status: 'open', agent_posts_since_human: 1,
    thread_cap: 12, tasks: [], task_counts: {}, posts: [post], created_at: at };
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/' + hash, runScripts: 'dangerously', beforeParse(win) {
    win.HTMLElement.prototype.scrollIntoView = () => {};
    win.fetch = async (u, opts = {}) => {
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, issues, needs_you_issues: pendingIssues, threads: [thread], needs_you: [], sessions: [], paused: false,
        authorization_grants: [], task_categories: [], active_runs: [], launchable_agents: [], agents: [], limits: {} });
      calls.push({ path: u, body: opts.body && JSON.parse(opts.body), method: opts.method });
      return reply ? reply(u, calls.at(-1).body) : ok({ id: 1 });
    };
  } });
  await settle();
  return { dom, win: dom.window, d: dom.window.document, calls };
}
function button(d, text) { return [...d.querySelectorAll('button')].find(b => b.textContent === text); }
function fill(win, d, name, value) {
  const node = d.querySelector(`[name="${name}"]`); node.value = value;
  node.dispatchEvent(new win.Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  return node;
}
function submit(win, node) { node.closest('form').dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true })); }

test('Needs you surfaces one issue with source links and renders untrusted content as text', async () => {
  const { dom, d, win } = await setup();
  try {
    assert.equal(d.querySelectorAll('.pane-side [data-issue="1"]').length, 1);
    assert.match(d.querySelector('.pane-side').textContent, /Needs you \(1\)/);
    assert.equal(d.querySelectorAll('#thread-issues a').length, 1);
    d.getElementById('issues-link').click(); await settle();
    d.querySelector('.issue-row').click();
    assert.equal(d.querySelector('#issue-1 a[href="#post-100"]').textContent, 'post #100');
    assert.equal(d.querySelector('#issue-1 a[href="#post-200"]').textContent, 'post #200');
    assert.equal(d.querySelector('img'), null); assert.equal(win.pwned, undefined);
  } finally { dom.window.close(); }
});

test('post reuse requires explicit linking and never silently merges a search match', async () => {
  const { dom, d, win, calls } = await setup();
  try {
    d.querySelector('[data-link-issue="100"]').click(); await settle();
    const search = d.querySelector('[type="search"]'); search.value = 'metadata'; search.dispatchEvent(new win.Event('input'));
    assert.equal(d.querySelectorAll('.issue-row').length, 1);
    assert.equal(calls.length, 0);
    d.querySelector('.issue-row').click();
    assert.equal(calls.length, 0);
    button(d, 'Link this post').click(); await settle();
    assert.deepEqual(calls[0], { path: '/api/issues/1/links', method: 'POST', body: { thread_id: 10, post_id: 100 } });
  } finally { dom.window.close(); }
});

test('create retains exact originating post and drafts; failed writes keep the draft', async () => {
  const { dom, d, win, calls } = await setup({ issues: [], reply: () => ({ ok: false, status: 400, json: async () => ({ message: 'Try again' }) }) });
  try {
    d.querySelector('[data-link-issue="100"]').click(); await settle();
    fill(win, d, 'title', 'Different blocker'); const body = fill(win, d, 'body', 'Exact context'); submit(win, body); await settle();
    assert.deepEqual(calls[0].body, { title: 'Different blocker', body: 'Exact context', needs_human: true, thread_id: 10, post_id: 100 });
    assert.equal(d.querySelector('[name="title"]').value, 'Different blocker');
    assert.match(d.querySelector('[role="alert"]').textContent, /Try again/);
  } finally { dom.window.close(); }
});

test('decision requires explicit thread scope and does not resolve implementation', async () => {
  const data = issue();
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1', reply: (url, body) => {
    if (url.endsWith('/decisions')) { data.needs_human = false; data.decisions.push({ id: 1, ...body, agent: 'human', created_at: at }); }
    return ok(data);
  } });
  try {
    assert.equal(d.querySelectorAll('fieldset input:checked').length, 0);
    submit(win, fill(win, d, 'decision', 'Approved only for detector work')); await settle();
    assert.equal(calls.length, 0); assert.match(d.querySelector('[role="alert"]').textContent, /Select the threads/);
    fill(win, d, 'outcome', 'approved'); d.querySelector('fieldset input[value="10"]').click();
    submit(win, d.querySelector('[name="decision"]')); await settle();
    assert.deepEqual(calls[0].body, { body: 'Approved only for detector work', outcome: 'approved', thread_ids: [10] });
    assert.match(d.querySelector('#issue-1').textContent, /Human answered · unresolved/);
    assert.equal(calls.some(c => c.path.endsWith('/resolve')), false);
    assert.ok(button(d, 'Mark resolved'));
  } finally { dom.window.close(); }
});

test('comments, evidence, proposals and new human requests are separate from resolution', async () => {
  const { dom, d, win, calls } = await setup({ hash: '#issue-1' });
  try {
    for (const kind of ['comment', 'evidence', 'proposal', 'request']) {
      fill(win, d, 'kind', kind); submit(win, fill(win, d, 'body', kind + ' content')); await settle();
      assert.deepEqual(calls.at(-1).body, { body: kind + ' content', kind });
      assert.equal(calls.at(-1).path, '/api/issues/1/comments');
    }
    submit(win, fill(win, d, 'resolution', 'Verified on isolated fixture')); await settle();
    assert.deepEqual(calls.at(-1).body, { body: 'Verified on isolated fixture' });
    assert.equal(calls.at(-1).path, '/api/issues/1/resolve');
  } finally { dom.window.close(); }
});

test('agent issue view is read-only and old snapshots without issues still render', async () => {
  const { dom, d } = await setup({ human: false, hash: '#issue-1' });
  try { assert.equal(d.querySelector('.issue-form'), null); assert.ok(d.getElementById('issue-1')); }
  finally { dom.window.close(); }
  const old = await setup({ issues: null });
  old.dom.window.close();
});


test('reopened issues retain historical evidence and allow a new verified resolution', async () => {
  const data = issue(); data.status = 'resolved'; data.needs_human = false;
  data.resolution = { body: 'First verification', agent: 'human', created_at: at };
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1', reply: (url, body) => {
    if (url.endsWith('/comments') && body.kind === 'request') { data.status = 'open'; data.needs_human = true; }
    if (url.endsWith('/resolve')) { data.status = 'resolved'; data.needs_human = false; data.resolution = { ...body, agent: 'human', created_at: at }; }
    return ok(data);
  } });
  try {
    assert.equal(button(d, 'Mark resolved'), undefined);
    fill(win, d, 'kind', 'request'); submit(win, fill(win, d, 'body', 'Blocker returned')); await settle();
    assert.match(d.querySelector('#issue-1').textContent, /Previous resolution by human/);
    assert.match(d.querySelector('#issue-1').textContent, /Awaiting decision/);
    assert.ok(button(d, 'Mark resolved'));
    submit(win, fill(win, d, 'resolution', 'Second verification')); await settle();
    assert.equal(calls.at(-1).path, '/api/issues/1/resolve');
    assert.match(d.querySelector('#issue-1').textContent, /Second verification/);
    assert.equal(button(d, 'Mark resolved'), undefined);
  } finally { dom.window.close(); }
});


test('linked unresolved issues keep read threads visible without duplicating decision cards', async () => {
  const data = issue(); data.links[0].needs_human = false; data.links[1].needs_human = true;
  const { dom, d, win } = await setup({ issues: [data], hash: '' });
  try {
    assert.ok(d.querySelector('tr[data-thread="10"]'));
    assert.match(d.querySelector('tr[data-thread="10"]').textContent + d.querySelector('tr[data-thread="10"]').innerHTML, /Shared issue unresolved/);
    assert.equal(d.querySelector('#needs-you'), null);
    d.getElementById('issues-link').click(); await settle();
    d.getElementById('issues-link').click(); await settle();
    assert.ok(d.querySelector('tr[data-thread="10"]'));
    assert.equal(d.querySelectorAll('.pane-side [data-issue="1"]').length, 1);
  } finally { dom.window.close(); }
});


test('older pending issues outside the recent list surface once and open from Needs you', async () => {
  const data = issue(9);
  const { dom, d, win } = await setup({ issues: [], pendingIssues: [data] });
  try {
    assert.equal(d.querySelectorAll('.pane-side [data-issue="9"]').length, 1);
    assert.ok(d.querySelector('#thread-issues a[href="#issue-9"]'));
    d.querySelector('.pane-side a[href="#issue-9"]').click(); await settle();
    assert.ok(d.getElementById('issue-9'));
    assert.ok(d.querySelector('#issue-9 a[href="#post-100"]'));
  } finally { dom.window.close(); }
  const duplicate = await setup({ issues: [data], pendingIssues: [data] });
  try { assert.equal(duplicate.d.querySelectorAll('.pane-side [data-issue="9"]').length, 1); }
  finally { duplicate.dom.window.close(); }
});

test('a deep link fetches an older resolved issue outside both snapshots', async () => {
  const data = issue(7); data.status = 'resolved'; data.needs_human = false;
  const { dom, d, calls } = await setup({ issues: [], hash: '#issue-7', reply: () => ok(data) });
  try {
    assert.ok(d.getElementById('issue-7'));
    assert.deepEqual(calls[0], { path: '/api/issues/7', method: 'GET', body: undefined });
    assert.match(d.getElementById('issue-7').textContent, /Resolved/);
  } finally { dom.window.close(); }
});
