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
