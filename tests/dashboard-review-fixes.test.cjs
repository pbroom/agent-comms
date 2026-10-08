// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-review-fixes.test.cjs
// Regressions from the dashboard review: each test failed before its fix.
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

function post(id, threadId, extra = {}) {
  return { id, seq: id, thread_id: threadId, agent: 'codex', session_id: 1, type: 'status', body: 'post ' + id, to: [],
    needs_response: false, task_id: null, refs: [], sealed: false, created_at: ago(100 - id / 100), ...extra };
}
function thread(id, posts, extra = {}) {
  return { id, title: 'Thread ' + id, project: '/repo/app-' + id, status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: ago(300), ...extra };
}
// A thread the server reports as complete (every recorded request finished).
const complete = { waiting: [], processing: [], blocked: [], complete: true };

// `board` is mutable: change it and call win.refresh() (or wait for the poll). `routes` answers other requests.
async function setup({ board, url = 'http://127.0.0.1:8787/', storage = {}, routes = {}, me = { name: 'human', is_human: true } }) {
  const calls = [];
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', pretendToBeVisual: true, beforeParse(win) {
    for (const [k, v] of Object.entries(storage)) win.localStorage.setItem(k, v);
    for (const name of ['confirm', 'alert', 'prompt']) win[name] = () => { throw new Error(name + ' called'); };
    win.fetch = async (u, opts = {}) => {
      calls.push({ url: u, method: opts.method || 'GET', body: opts.body });
      if (u === '/api/whoami') return ok(me);
      for (const [prefix, answer] of Object.entries(routes)) if (u.startsWith(prefix)) return answer(u, opts);
      if (u.startsWith('/api/state')) {
        const closed = u.includes('closed=true');
        return ok({ me, paused: false, sessions: [], needs_you: [], limits: {}, authorization_grants: [], agents: [],
          task_categories: [], active_runs: [], ...board,
          threads: board.threads.filter(t => closed || t.status === 'open') });
      }
      return fail(404, 'not found');
    };
  } });
  await settle();
  return { dom, win: dom.window, d: dom.window.document, calls };
}
const rowSel = id => `.thread-list [data-thread="${id}"]`;
const dotOf = (d, id) => { const n = d.querySelector(`${rowSel(id)} .dot`); return n ? [n.className.replace('dot ', ''), n.title] : null; };
const shown = d => [...d.querySelectorAll('#thread-pane > section[id^="thread-"]')].map(s => s.id);

// ---------------------------------------------------------------- 2. new posts bring a settled thread back
test('a completed thread comes back with a blue dot when a new post arrives, until it is opened', async () => {
  const done = thread(1, [post(10, 1)], { pickup: complete });
  const other = thread(2, [post(20, 2)], { pickup: { ...complete, complete: false } });
  const board = { threads: [done, other] };
  const { dom, win, d } = await setup({ board, storage: { 'agent-comms-thread': '2' } });
  try {
    assert.equal(d.querySelector(rowSel(1)), null, 'completed and already seen: hidden');
    // A later finding sent to the human (no needs_response) arrives in the completed thread.
    done.posts = [...done.posts, post(11, 1, { type: 'finding', to: ['human'] })];
    await win.refresh();
    assert.deepEqual(dotOf(d, 1), ['unread', 'New posts'], 'it is back in the list with a blue dot');
    assert.equal(JSON.parse(win.localStorage.getItem('agent-comms-seen'))[1], 10, 'seen marks are per browser');
    d.querySelector(rowSel(1)).click();
    assert.deepEqual(dotOf(d, 1), ['done', 'All recorded work finished'], 'opening it clears the dot');
    assert.equal(JSON.parse(win.localStorage.getItem('agent-comms-seen'))[1], 11);
    d.querySelector(rowSel(2)).click();
    assert.ok(d.querySelector(rowSel(1)), 'opened this page load: stays listed');
    await win.refresh();
    assert.deepEqual(dotOf(d, 1), ['done', 'All recorded work finished']);
  } finally { dom.window.close(); }
  // A reload tidies it away again: it was seen.
  const again = await setup({ board, storage: { 'agent-comms-thread': '2', 'agent-comms-seen': JSON.stringify({ 1: 11, 2: 20 }) } });
  try { assert.equal(again.d.querySelector(rowSel(1)), null); } finally { again.dom.window.close(); }
});

test('new-post marks never hide agent pickup, and my own posts are not new', async () => {
  const request = { recipient: 'codex', assigned_agent: 'codex', state: 'queued', updated_at: ago(1) };
  const pending = thread(1, [post(10, 1, { agent: 'claude', type: 'request', to: ['codex'], requests: [request] })]);
  const mine = thread(2, [post(20, 2)], { pickup: complete });
  const board = { threads: [pending, mine] };
  const { dom, win, d } = await setup({ board, storage: { 'agent-comms-thread': '1' } });
  try {
    mine.posts = [...mine.posts, post(21, 2, { agent: 'human' })];
    await win.refresh();
    assert.equal(d.querySelector(rowSel(2)), null, 'a post of my own does not bring a thread back');
    assert.equal(dotOf(d, 1)[0], 'unread');
    assert.match(dotOf(d, 1)[1], /awaiting agent pickup/, 'pickup keeps its own label');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 3. the poll retries a failed first load
test('a failed first /api/state is retried by the 3-second refresh', async () => {
  let failures = 1;
  const board = { threads: [thread(1, [post(10, 1)])] };
  const { dom, d } = await setup({ board, routes: {
    '/api/state': () => failures-- > 0 ? fail(500, 'board busy') : ok({ me: { name: 'human', is_human: true }, paused: false,
      sessions: [], needs_you: [], limits: {}, authorization_grants: [], agents: [], task_categories: [], active_runs: [],
      threads: board.threads }) } });
  try {
    assert.equal(d.querySelector('.thread-list'), null, 'the first load failed');
    await settle(3300);
    assert.ok(d.querySelector(rowSel(1)), 'the next poll loaded the board');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 5. the default thread stays put
test('the default thread stays selected when another thread gets a newer post', async () => {
  const a = thread(1, [post(10, 1, { created_at: ago(5) })]), b = thread(2, [post(20, 2, { created_at: ago(50) })]);
  const board = { threads: [a, b] };
  const { dom, win, d } = await setup({ board });
  try {
    assert.deepEqual(shown(d), ['thread-1'], 'the most recent thread is shown by default');
    b.posts = [...b.posts, post(21, 2, { created_at: ago(1) })];
    await win.refresh();
    assert.equal(d.querySelector(rowSel(2)), d.querySelector('.thread-list [data-thread]'), 'thread 2 now sorts first');
    assert.deepEqual(shown(d), ['thread-1'], 'the pane did not jump');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 6. a slow deep link loses to a newer click
test('a slow deep-link fetch does not override a click made meanwhile', async () => {
  const open1 = thread(1, [post(10, 1)]), open2 = thread(2, [post(20, 2)]);
  const old = thread(9, [post(90, 9)], { status: 'closed' });
  const board = { threads: [open1, open2, old] };
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const { dom, win, d } = await setup({ board, url: 'http://127.0.0.1:8787/#post-90', routes: {
    '/api/posts/90': async () => { await gate; return ok(post(90, 9)); },
    '/api/threads/9/posts': () => ok({ posts: [post(90, 9)] }) } });
  try {
    d.querySelector(rowSel(2)).click();
    assert.deepEqual(shown(d), ['thread-2']);
    release(); await settle(100);
    assert.deepEqual(shown(d), ['thread-2'], 'the click wins');
    assert.equal(d.getElementById('show-completed').checked, false, 'and "Show completed" is left alone');
    assert.equal(win.location.hash, '#thread-2');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 7. Needs you item in a closed thread
test('a Needs you item in a closed thread opens that thread (via the deep-link path)', async () => {
  const open1 = thread(1, [post(10, 1)]);
  const waiting = post(90, 9, { type: 'question', needs_response: true, to: ['human'] });
  const closed = thread(9, [waiting], { status: 'closed' });
  const board = { threads: [open1, closed], needs_you: [waiting] };
  const { dom, d } = await setup({ board, routes: {
    '/api/posts/90': () => ok(waiting), '/api/threads/9/posts': () => ok({ posts: [waiting] }) } });
  try {
    d.querySelector('#needs-you-side [data-post="90"]').click();
    await settle(150);
    assert.deepEqual(shown(d), ['thread-9'], 'the closed thread is shown, not the first open one');
    assert.equal(d.getElementById('show-completed').checked, true, '"Show completed" is turned on');
    assert.ok(d.querySelector('#needs-you [data-post="90"]'), 'with its Needs you item');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 8. issue search moves the pane with the list
const issue = (id, title, extra = {}) => ({ id, title, body: 'Body ' + id, status: 'open', needs_human: false, created_by: 'codex',
  created_at: ago(id), updated_at: ago(id), comments: [], decisions: [], resolution: null,
  links: [{ thread_id: 1, post_id: 10, project: '/repo/app-1', title: 'Thread 1' }], ...extra });
test('searching issues shows the newly selected first match in the pane', async () => {
  const one = issue(1, 'Flaky login'), two = issue(2, 'Disk full');
  const board = { threads: [thread(1, [post(10, 1)])], issues: [one, two] };
  const { dom, win, d } = await setup({ board, url: 'http://127.0.0.1:8787/#issues',
    routes: { '/api/issues?query=': u => ok([one, two].filter(i => i.title.toLowerCase().includes(new URL(u, 'http://x').searchParams.get('query').toLowerCase()))) } });
  try {
    assert.ok(d.getElementById('issue-1'), 'the newest issue is shown first');
    const input = d.querySelector('[type="search"]'); input.focus(); input.value = 'disk';
    input.dispatchEvent(new win.Event('input')); await settle();
    assert.equal(d.querySelector('.issue-row[data-issue="2"]') && d.querySelectorAll('.issue-row').length, 1);
    assert.ok(d.getElementById('issue-2'), 'the pane follows the selection');
    assert.equal(d.getElementById('issue-1'), null);
    assert.equal(d.activeElement, input, 'the search box keeps focus');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 9. tab focus survives the refresh
test('arrowing between the list tabs keeps focus on the new tab after the refresh', async () => {
  const board = { threads: [thread(1, [post(10, 1)])], issues: [issue(1, 'Flaky login')] };
  const { dom, win, d } = await setup({ board });
  try {
    d.getElementById('tab-threads').focus();
    d.getElementById('tab-threads').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    await settle();
    assert.equal(d.getElementById('tab-issues').getAttribute('aria-selected'), 'true');
    assert.equal(d.activeElement && d.activeElement.id, 'tab-issues', 'focus is on the Issues tab after the refresh');
    await win.refresh();
    assert.equal(d.activeElement && d.activeElement.id, 'tab-issues', 'and stays there across a poll');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 10. "Sending…" ends when the thread closes
const task = (id, status, extra = {}) => ({ id, title: 'T' + id, status, lease_state: 'none', owner_agent: 'codex', owner_session: 1,
  intends_files: [], events: [], depends_on: [], updated_at: ago(90), ...extra });
const UNSTICK_REPLY = { post_id: 50, agents: ['codex'], live_agents: [], no_runner: [], dispatcher_running: true, paused: false, sessions: [] };
test('"Sending…" clears at once when the thread is closed after Unstick', async () => {
  const stuck = thread(1, [post(10, 1)], { tasks: [task(1, 'blocked')] });
  const board = { threads: [stuck] };
  const { dom, win, d } = await setup({ board, routes: { '/api/threads/1/unstick': () => ok(UNSTICK_REPLY) } });
  try {
    d.getElementById('show-completed').click(); await settle();
    d.querySelector(`${rowSel(1)} [data-unstick="1"]`).click(); await settle();
    assert.equal(d.querySelector(`${rowSel(1)} [data-unstick="1"]`).textContent, 'Sending…', 'waiting for codex to restart');
    stuck.status = 'closed';
    await win.refresh();
    assert.equal(d.querySelector(`${rowSel(1)} [data-unstick="1"]`), null, 'closed: no "Sending…" for up to 10 minutes');
    stuck.status = 'open';
    await win.refresh();
    assert.equal(d.querySelector(`${rowSel(1)} [data-unstick="1"]`).textContent, 'Unstick', 'reopened: a fresh Unstick, not a stale wait');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 11. the post cap is "on you" for the human only
test('with an agent token, a thread at the post cap is not sorted into "stalled on you"', async () => {
  const capped = thread(1, [post(10, 1, { created_at: ago(60) })], { agent_posts_since_human: 12 });
  const blocked = thread(2, [post(20, 2, { created_at: ago(5) })], { tasks: [task(1, 'blocked')] });
  const me = { name: 'claude', is_human: false };
  const { dom, d } = await setup({ board: { threads: [capped, blocked] }, me, storage: { 'agent-comms-sort': 'priority' } });
  try {
    const order = [...d.querySelectorAll('.thread-list [data-thread]')].map(n => Number(n.dataset.thread));
    assert.deepEqual(order, [2, 1], 'both are stalled on someone else (the human), so recent activity decides');
  } finally { dom.window.close(); }
});

// ---------------------------------------------------------------- 12. tasks waiting on prerequisites are not stalled
test('a task waiting on an unfinished prerequisite is neither awaiting pickup nor stalled', async () => {
  const t = thread(1, [post(10, 1)], { tasks: [task(1, 'working', { lease_state: 'active' }), task(2, 'proposed', { depends_on: [1] })] });
  const board = { threads: [t] };
  const { dom, win, d } = await setup({ board });
  try {
    assert.deepEqual(dotOf(d, 1), ['active', 'codex working on task 1']);
    t.tasks[0] = task(1, 'done');
    await win.refresh();
    assert.deepEqual(dotOf(d, 1), ['stalled', 'task 2 proposed, awaiting agent pickup for 2h'], 'once its prerequisite is done it can stall');
  } finally { dom.window.close(); }
});
