// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-delivery-reset.test.cjs
// "Reset stuck delivery" under a dependent-stack continuation post: shown to the human only when the server says the
// fallback delivery is stuck, takes two clicks, posts the request's current version and shows the server's answer.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 60) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });
const fail = (status, message) => ({ ok: false, status, statusText: String(status), json: async () => ({ message }) });
const ago = minutes => new Date(Date.now() - minutes * 60000).toISOString();
const INJECTION = '<img src=x onerror="window.pwned=1">';

function continuationPost(resettable, extra = {}) {
  return { id: 7, seq: 7, thread_id: 1, agent: 'codex', session_id: 1, type: 'handoff', body: 'propagate ' + INJECTION,
    to: ['codex', 'claude'], needs_response: false, task_id: 3, refs: [], sealed: false, created_at: ago(30),
    requests: [{ recipient: 'codex', assigned_agent: 'claude', assigned_session: 5, state: 'blocked',
      reason: 'Runner failed to start ' + INJECTION, version: 4, evidence_post_ids: [], updated_at: ago(2) }],
    continuation: { post_id: 7, recipient: 'codex', epoch: 1, dispatch_run_id: null, blocker: '',
      deadline: new Date(Date.now() + 120000).toISOString(),
      delivery_reset: resettable === undefined ? undefined : { reserved: false, resettable } },
    ...extra };
}
function thread(posts) {
  return { id: 1, title: 'Stack', project: '/repo/app', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: ago(300) };
}

async function setup({ board, routes = {}, me = { name: 'human', is_human: true } }) {
  const calls = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', pretendToBeVisual: true, beforeParse(win) {
    for (const name of ['confirm', 'alert', 'prompt']) win[name] = () => { throw new Error(name + ' called'); };
    win.fetch = async (u, opts = {}) => {
      calls.push({ url: u, method: opts.method || 'GET', body: opts.body });
      if (u === '/api/whoami') return ok(me);
      for (const [prefix, answer] of Object.entries(routes)) if (u.startsWith(prefix)) return answer(u, opts);
      if (u.startsWith('/api/state')) return ok({ me, paused: false, sessions: [], needs_you: [], limits: {},
        authorization_grants: [], agents: [], task_categories: [], active_runs: [], ...board });
      return fail(404, 'not found');
    };
  } });
  await settle();
  return { dom, win: dom.window, d: dom.window.document, calls };
}
const button = d => d.querySelector('[data-delivery-reset="7"] button.reset-delivery');
const resets = calls => calls.filter(c => c.url === '/api/posts/7/continuation/reset-delivery');

test('offered only to the human, and only when the server reports the delivery stuck', async () => {
  for (const [resettable, me, shown] of [[true, { name: 'human', is_human: true }, true],
                                         [false, { name: 'human', is_human: true }, false],
                                         [undefined, { name: 'human', is_human: true }, false],
                                         [true, { name: 'codex', is_human: false }, false]]) {
    const { dom, d } = await setup({ board: { threads: [thread([continuationPost(resettable)])] }, me });
    try {
      assert.ok(d.getElementById('post-7'), 'the post renders');
      assert.equal(!!button(d), shown, JSON.stringify({ resettable, me }));
    } finally { dom.window.close(); }
  }
  const plain = continuationPost(true);
  delete plain.continuation;
  const { dom, d } = await setup({ board: { threads: [thread([plain])] } });
  try { assert.equal(button(d), null, 'no continuation, no control'); } finally { dom.window.close(); }
});

test('two clicks reset with the current version, then the result shows inline and the board refreshes', async () => {
  const board = { threads: [thread([continuationPost(true)])] };
  const { dom, win, d, calls } = await setup({ board, routes: {
    '/api/posts/7/continuation/reset-delivery': () => {
      board.threads = [thread([continuationPost(false)])];
      return ok({ post_id: 7, recipient: 'codex', epoch: 1, dispatch_run_id: null, blocker: '',
        deadline: new Date(Date.now() + 120000).toISOString() });
    } } });
  try {
    button(d).click();
    await settle();
    assert.equal(resets(calls).length, 0, 'one click only arms it');
    assert.equal(button(d).textContent, 'Confirm reset?');
    await settle(550);
    const before = calls.filter(c => c.url.startsWith('/api/state')).length;
    button(d).click();
    await settle(100);
    const sent = resets(calls);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].method, 'POST');
    assert.deepEqual(JSON.parse(sent[0].body), { expected_version: 4 });
    assert.ok(calls.filter(c => c.url.startsWith('/api/state')).length > before, 'refreshed afterwards');
    assert.equal(button(d), null, 'no longer stuck: the control is gone');
    const note = d.querySelector('[data-delivery-reset="7"] .delivery-reset-note');
    assert.ok(note && !note.classList.contains('failed'));
    assert.match(note.textContent, /^Delivery reset/);
    note.querySelector('button').click();
    assert.equal(d.querySelector('[data-delivery-reset="7"]'), null, 'dismissed');
    assert.equal(win.pwned, undefined);
  } finally { dom.window.close(); }
});

test("the server's refusal shows inline as text, and the control stays", async () => {
  const board = { threads: [thread([continuationPost(true)])] };
  const { dom, win, d, calls } = await setup({ board, routes: {
    '/api/posts/7/continuation/reset-delivery': () =>
      fail(409, 'the delivery run is still active or registered a session ' + INJECTION) } });
  try {
    button(d).click();
    await settle(550);
    button(d).click();
    await settle(100);
    assert.equal(resets(calls).length, 1);
    const note = d.querySelector('[data-delivery-reset="7"] .delivery-reset-note.failed');
    assert.ok(note, 'the failure is shown');
    assert.match(note.textContent, /Reset failed: the delivery run is still active/);
    assert.ok(note.textContent.includes(INJECTION), 'rendered as text');
    assert.equal(d.querySelector('[data-delivery-reset="7"] img'), null);
    assert.ok(button(d), 'still offered: the board still reports it stuck');
    assert.equal(button(d).textContent, 'Reset stuck delivery');
    assert.equal(win.pwned, undefined);
  } finally { dom.window.close(); }
});
