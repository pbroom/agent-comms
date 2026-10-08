// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-needs-you.test.cjs
// The Needs you callout: at the top of the selected thread, a call to action and one-click resolve buttons.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });
const fail = (status, message) => ({ ok: false, status, statusText: String(status), json: async () => ({ message }) });
const MINUTES_AGO = m => new Date(Date.now() - m * 60000).toISOString();
const INJECTION = '<img src=x onerror="window.pwned=1">';

function post(id, threadId, extra = {}) {
  return { id, seq: id, thread_id: threadId, agent: 'codex', session_id: 1, type: 'status', body: 'post ' + id + ' ' + INJECTION,
    to: [], needs_response: false, task_id: null, refs: [], sealed: false, created_at: MINUTES_AGO(120 - id), ...extra };
}
function thread(id, posts, extra = {}) {
  return { id, title: 'Thread ' + id + ' ' + INJECTION, project: '/repo/app', status: 'open', agent_posts_since_human: 0,
    thread_cap: 12, pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: MINUTES_AGO(300), ...extra };
}

// window.confirm/prompt/alert throw (and are recorded): the human's embedded browser blocks them.
// `threads` and `needsYou` may be functions, re-read on every /api/state.
async function setup({ threads, needsYou = [], human = true, launchable = [], reply = () => ok({}), url = 'http://127.0.0.1:8787/',
  storage = {} }) {
  const calls = [], prompts = [];
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', pretendToBeVisual: true, beforeParse(win) {
    for (const [k, v] of Object.entries({ 'agent-comms-seen': '{}', ...storage })) win.localStorage.setItem(k, v);
    for (const name of ['confirm', 'prompt', 'alert'])
      win[name] = text => { prompts.push([name, text]); throw new Error(`window.${name} must not be called`); };
    win.HTMLElement.prototype.scrollIntoView = function () { (win.scrolled = win.scrolled || []).push(this.id || this.dataset.post || this.tagName); };
    win.fetch = async (u, opts = {}) => {
      const me = human ? { name: 'human', is_human: true } : { name: 'codex', is_human: false };
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, paused: false,
        threads: typeof threads === 'function' ? threads() : threads,
        needs_you: typeof needsYou === 'function' ? needsYou() : needsYou,
        sessions: [], limits: { body_max_bytes: 4096 }, authorization_grants: [], task_categories: [], active_runs: [],
        launchable_agents: launchable,
        agents: [{ name: 'human', is_human: 1 }, { name: 'claude-code', is_human: 0 }, { name: 'codex', is_human: 0 }] });
      calls.push({ u, method: opts.method, headers: opts.headers, body: opts.body ? JSON.parse(opts.body) : undefined });
      return reply(u, opts);
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, calls, prompts };
}
const items = d => [...d.querySelectorAll('#needs-you .ny-item')].map(n => Number(n.dataset.post));
const ctas = d => [...d.querySelectorAll('#needs-you .ny-cta')].map(n => n.textContent);
const actions = (d, id) => [...d.querySelectorAll(`#needs-you [data-post="${id}"] .ny-actions button`)].map(b => b.textContent);
const button = (d, id, action) => d.querySelector(`#needs-you [data-post="${id}"] button[data-action="${action}"]`);
const pick = async (d, id) => { d.querySelector(`tr[data-thread="${id}"]`).click(); await settle(10); };

test('the callout sits at the top of the thread, only for items that need you, newest first', async () => {
  const q = post(10, 1, { type: 'question', agent: 'claude-code', needs_response: true });
  const d = post(12, 1, { type: 'decision', decision_status: 'proposal (NOT binding until the human finalizes it)' });
  const r = post(14, 1, { type: 'request', needs_response: true });
  const other = post(20, 2, { type: 'request', needs_response: true });
  const t1 = thread(1, [q, post(11, 1), d, post(13, 1), r]), t2 = thread(2, [other]), t3 = thread(3, [post(30, 3)]);
  const { dom, document, win } = await setup({ threads: [t1, t2, t3], needsYou: [r, d, q, other] });
  try {
    await pick(document, 1);
    const pane = document.getElementById('thread-pane');
    assert.equal(pane.firstElementChild.id, 'needs-you', 'above the thread header and posts');
    assert.equal(pane.children[1].id, 'thread-1');
    assert.deepEqual(items(document), [14, 12, 10], 'newest first, this thread only');
    assert.deepEqual(ctas(document), ['codex is waiting on you (request) — approve or reply',
      'codex proposes a decision — finalize it or reply', 'claude-code asks you a question — reply']);
    assert.equal(document.querySelector('#needs-you h2').textContent, 'Needs you (3)');
    await pick(document, 3);
    assert.equal(document.getElementById('needs-you'), null, 'no callout where nothing needs you');
    assert.equal(win.pwned, undefined);
    assert.equal(document.querySelector('img'), null, 'post text and titles are never parsed as HTML');
    assert.ok(document.querySelector('#needs-you, #thread-3 .body').textContent.includes('<img'), 'shown as text');
  } finally { dom.window.close(); }
});

test('hidden for an agent viewing the board', async () => {
  const r = post(14, 1, { type: 'request', needs_response: true, agent: 'claude-code' });
  const { dom, document } = await setup({ threads: [thread(1, [r])], needsYou: [r], human: false });
  assert.ok(document.getElementById('thread-1'));
  assert.equal(document.getElementById('needs-you'), null);
  dom.window.close();
});

test('buttons per item: decisions get Finalize and Reject; Approve & launch only for a launchable author', async () => {
  const d = post(12, 1, { type: 'decision' }), sealedD = post(13, 1, { type: 'decision', sealed: true });
  const q = post(14, 1, { type: 'question', agent: 'claude-code', needs_response: true });
  const { dom, document } = await setup({ threads: [thread(1, [d, sealedD, q])], needsYou: [q, sealedD, d], launchable: ['codex'] });
  try {
    assert.deepEqual(actions(document, 12), ['Finalize', 'Reject', 'Approve', 'Approve & launch codex', 'Not now', 'Reply…', 'Jump to post #12']);
    assert.deepEqual(actions(document, 13), ['Reject', 'Approve', 'Approve & launch codex', 'Not now', 'Reply…', 'Jump to post #13'],
      'a sealed decision cannot be finalized until unsealed');
    assert.deepEqual(actions(document, 14), ['Approve', 'Not now', 'Reply…', 'Jump to post #14'], 'claude-code is not launchable');
  } finally { dom.window.close(); }
});

test('each button calls the resolve endpoint once, with no confirm, and the result line says what happened', async () => {
  const cases = [
    ['approve', { type: 'request', needs_response: true }, { action: 'approve', post_id: 90, resolved_post_id: 14, to: ['codex'] },
      'Approved #14; told codex to go ahead (post #90).'],
    ['not_now', { type: 'request', needs_response: true }, { action: 'not_now', post_id: 91, resolved_post_id: 14, to: ['codex'] },
      'Parked #14; told codex not now (post #91).'],
    ['reject', { type: 'decision' }, { action: 'reject', post_id: 92, resolved_post_id: 14, to: ['codex'] },
      'Rejected decision #14; told codex (post #92).'],
    ['approve_launch', { type: 'request', needs_response: true }, { action: 'approve_launch', post_id: 93, resolved_post_id: 14,
      to: ['codex'], agent: 'codex', rule_id: 5, dispatcher_running: true, paused: false, live: false, no_runner: false },
      'Approved #14 and launched codex (dispatcher running; it starts within seconds) (post #93).'],
    ['approve_launch', { type: 'request', needs_response: true }, { action: 'approve_launch', post_id: 94, resolved_post_id: 14,
      to: ['codex'], agent: 'codex', rule_id: 6, dispatcher_running: false, paused: false, live: false, no_runner: false },
      "Approved #14 (post #94). The dispatcher isn't running — start it with `board dispatch run` to launch codex."],
  ];
  for (const [action, extra, answer, message] of cases) {
    const item = post(14, 1, extra);
    let waiting = [item];
    const reply = u => { if (u === '/api/posts/14/resolve') { waiting = []; return ok(answer); } return fail(404, 'nf'); };
    const { dom, document, calls, prompts } = await setup({ threads: [thread(1, [item])], needsYou: () => waiting, reply,
      launchable: ['codex'] });
    try {
      button(document, 14, action).click();
      await settle();
      assert.deepEqual(prompts, [], 'no window.confirm/prompt/alert');
      const sent = calls.filter(c => c.u === '/api/posts/14/resolve');
      assert.equal(sent.length, 1, action);
      assert.equal(sent[0].method, 'POST');
      assert.equal(sent[0].headers['X-Board-Request'], '1');
      assert.deepEqual(sent[0].body, { action });
      assert.equal(document.querySelector('#needs-you-result span').textContent, message);
      assert.deepEqual(items(document), [], 'the item leaves the callout on refresh');
      assert.match(document.querySelector('#needs-you h2').textContent, /nothing left here/);
      document.querySelector('#needs-you-result button').click();
      assert.equal(document.getElementById('needs-you'), null, 'dismissed: the callout goes away');
    } finally { dom.window.close(); }
  }
});

test('Finalize uses the existing endpoint; errors show inline, not in an alert; a double click sends once', async () => {
  const d = post(12, 1, { type: 'decision' });
  let release;
  const gate = new Promise(r => { release = r; });
  const reply = async u => { if (u === '/api/posts/12/finalize') { await gate; return ok({ id: 12 }); }
    return fail(409, 'post #12 no longer needs you (it was already handled)'); };
  const { dom, document, calls, prompts } = await setup({ threads: [thread(1, [d])], needsYou: [d], reply });
  try {
    button(document, 12, 'finalize').click();
    await settle(10);
    assert.ok(button(document, 12, 'finalize').disabled && button(document, 12, 'approve').disabled, 'busy while in flight');
    button(document, 12, 'finalize').click();
    release();
    await settle();
    assert.equal(calls.filter(c => c.u === '/api/posts/12/finalize').length, 1);
    assert.equal(document.querySelector('#needs-you-result span').textContent, 'Finalized decision #12.');
    button(document, 12, 'approve').click();
    await settle();
    const res = document.getElementById('needs-you-result');
    assert.ok(res.classList.contains('failed'));
    assert.equal(res.querySelector('span').textContent, "Couldn't resolve #12: post #12 no longer needs you (it was already handled)");
    assert.deepEqual(prompts, []);
  } finally { dom.window.close(); }
});

test('Reply… opens a textarea; Send posts the human text; the byte limit is enforced', async () => {
  const q = post(14, 1, { type: 'question', needs_response: true });
  let waiting = [q];
  const reply = u => { if (u === '/api/posts/14/resolve') { waiting = []; return ok({ action: 'reply', post_id: 95, resolved_post_id: 14, to: ['codex'] }); }
    return fail(404, 'nf'); };
  const { dom, document, win, calls, prompts } = await setup({ threads: [thread(1, [q])], needsYou: () => waiting, reply });
  try {
    assert.equal(document.querySelector('#needs-you textarea'), null);
    button(document, 14, 'reply-open').click();
    const area = document.querySelector('#needs-you [data-post="14"] textarea');
    assert.ok(area, 'the textarea opens inline');
    const send = () => document.querySelector('#needs-you .ny-send');
    assert.ok(send().disabled, 'nothing to send yet');
    area.value = 'x'.repeat(4097); area.dispatchEvent(new win.Event('input'));
    assert.ok(send().disabled, 'over 4 KB');
    assert.match(document.querySelector('#needs-you .ny-count').textContent, /4097 \/ 4096 bytes — too long/);
    area.value = '  Use option B, not ' + INJECTION + '  '; area.dispatchEvent(new win.Event('input'));
    assert.ok(!send().disabled);
    await win.refresh(); await settle(10);
    assert.equal(document.querySelector('#needs-you textarea').value, '  Use option B, not ' + INJECTION + '  ', 'the draft survives a refresh');
    send().click();
    await settle();
    const [sent] = calls.filter(c => c.u === '/api/posts/14/resolve');
    assert.deepEqual(sent.body, { action: 'reply', text: 'Use option B, not ' + INJECTION });
    assert.equal(document.querySelector('#needs-you-result span').textContent, 'Replied to #14 (to codex) (post #95).');
    assert.equal(document.querySelector('#needs-you textarea'), null);
    assert.equal(win.pwned, undefined);
    assert.deepEqual(prompts, []);
  } finally { dom.window.close(); }
});

test('long bodies collapse with Show more; Jump to post scrolls to and highlights it in the thread', async () => {
  const long = post(14, 1, { type: 'request', needs_response: true, body: Array.from({ length: 12 }, (_, i) => 'line ' + i).join('\n'),
    refs: [{ kind: 'file', path: 'src/a.py', rev: 'abc123' }] });
  const { dom, document, win } = await setup({ threads: [thread(1, [post(10, 1), long])], needsYou: [long] });
  try {
    const body = () => document.querySelector('#needs-you .ny-body');
    assert.ok(body().classList.contains('clamped'));
    assert.equal(document.querySelector('#needs-you .refs').textContent, 'file: src/a.py @ abc123');
    button(document, 14, 'more').click();
    assert.ok(!body().classList.contains('clamped'));
    assert.equal(button(document, 14, 'more').textContent, 'Show less');
    win.scrolled = [];
    button(document, 14, 'jump').click();
    await settle(10);
    assert.ok(document.getElementById('post-14').classList.contains('highlight'));
    assert.ok(win.scrolled.includes('post-14'), 'scrolled to the post in the thread');
  } finally { dom.window.close(); }
});

test('the "needs you" chip in the list opens the thread at the callout; so do the sidebar items', async () => {
  const r = post(20, 2, { type: 'request', needs_response: true });
  const q = post(30, 3, { type: 'question', needs_response: true });
  const { dom, document, win } = await setup({ threads: [thread(1, [post(10, 1)]), thread(2, [r]), thread(3, [q])],
    needsYou: [q, r], storage: { 'agent-comms-thread': '1' } });
  try {
    assert.equal(document.getElementById('needs-you'), null);
    const chip = document.querySelector('tr[data-thread="2"] button.needs-chip');
    assert.equal(chip.textContent, '1 needs you');
    win.scrolled = [];
    chip.click();
    await settle(10);
    assert.equal(document.querySelector('tr[aria-selected=true]').dataset.thread, '2');
    assert.deepEqual(items(document), [20]);
    assert.deepEqual(win.scrolled, ['needs-you'], 'the callout is brought into view');
    assert.equal(win.location.hash, '#thread-2');
    win.scrolled = [];
    document.querySelector('.ny-side[data-post="30"]').click();
    await settle(10);
    assert.equal(document.querySelector('tr[aria-selected=true]').dataset.thread, '3');
    assert.deepEqual(items(document), [30]);
    assert.deepEqual(win.scrolled, ['30'], 'scrolled to that item in the callout');
    document.querySelector('.ny-side[data-post="20"] a').click();   // the #N link does the same, not a hash jump
    await settle(10);
    assert.deepEqual(items(document), [20]);
    assert.equal(win.location.hash, '#thread-2');
  } finally { dom.window.close(); }
});

test('a #post-N deep link to a needs-you post shows the callout and lands on its block', async () => {
  const r = post(20, 2, { type: 'request', needs_response: true });
  const { dom, document, win } = await setup({ threads: [thread(1, [post(10, 1)]), thread(2, [post(19, 2), r])], needsYou: [r],
    url: 'http://127.0.0.1:8787/#post-20' });
  try {
    await settle();
    assert.equal(document.querySelector('tr[aria-selected=true]').dataset.thread, '2');
    assert.equal(document.getElementById('thread-pane').firstElementChild.id, 'needs-you');
    assert.deepEqual(items(document), [20]);
    assert.ok(win.scrolled.includes('20'), 'scrolled to the callout block');
    assert.ok(document.getElementById('post-20').classList.contains('highlight'), 'the post stays highlighted below');
  } finally { dom.window.close(); }
});
