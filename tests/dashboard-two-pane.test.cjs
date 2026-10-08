// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-two-pane.test.cjs
// Board view: thread list on the left, one selected thread on the right.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });

function post(id, threadId, minute, extra = {}) {
  return { id, seq: id, thread_id: threadId, agent: 'codex', session_id: 1, type: 'status', body: 'post ' + id, to: [],
    needs_response: false, task_id: null, refs: [], sealed: false,
    created_at: `2027-01-15T08:${String(minute).padStart(2, '0')}:00+00:00`, ...extra };
}
function thread(id, posts, status = 'open') {
  return { id, title: 'Thread ' + id, project: '/repo/app-' + id, status, agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: '2027-01-15T07:00:00+00:00' };
}
// Thread 2 has the newest post, so it sorts first; thread 3 is newest by id.
const THREADS = [thread(3, [post(30, 3, 10)]), thread(2, [post(20, 2, 40), post(21, 2, 45)]), thread(1, [post(10, 1, 5)])];

async function setup({ url = 'http://127.0.0.1:8787/', storage = {}, needsYou = [] } = {}) {
  // Every post unread, so no thread has settled (settled threads are hidden); these tests are about layout.
  storage = { 'agent-comms-seen': '{}', ...storage };
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', beforeParse(win) {
    for (const [k, v] of Object.entries(storage)) win.localStorage.setItem(k, v);
    win.fetch = async u => {
      if (u === '/api/whoami') return ok({ name: 'human', is_human: true });
      if (u.startsWith('/api/state')) return ok({ me: { name: 'human', is_human: true }, paused: false, threads: THREADS,
        sessions: [], needs_you: needsYou, limits: {}, authorization_grants: [], agents: [], task_categories: [] });
      return { ok: false, status: 404, statusText: '404', json: async () => ({ message: 'not found' }) };
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document };
}
const rows = d => [...d.querySelectorAll('.thread-list tr')].map(r => Number(r.dataset.thread));
const shown = d => [...d.querySelectorAll('#thread-pane > section')].map(s => s.id);

test('lists every thread by latest activity and shows only the newest one', async () => {
  const { dom, document } = await setup();
  assert.deepEqual(rows(document), [2, 3, 1]);
  assert.deepEqual(shown(document), ['thread-2']);
  assert.equal(document.querySelector('tr[aria-selected=true]').dataset.thread, '2');
  assert.match(document.querySelector('tr[data-thread="2"]').textContent, /app-2\s*· 2 posts/);
  dom.window.close();
});

test('clicking a row shows that thread, updates the hash and is remembered', async () => {
  const { dom, document, win } = await setup();
  document.querySelector('tr[data-thread="1"]').click();
  assert.deepEqual(shown(document), ['thread-1']);
  assert.equal(win.location.hash, '#thread-1');
  assert.equal(win.localStorage.getItem('agent-comms-thread'), '1');
  assert.ok(!document.getElementById('thread-1').classList.contains('highlight'), 'a click selects, it does not highlight');
  dom.window.close();
  const again = await setup({ storage: { 'agent-comms-thread': '1' } });
  assert.deepEqual(shown(again.document), ['thread-1']);
  again.dom.window.close();
});

test('arrow keys move the selection', async () => {
  const { dom, document, win } = await setup();
  const table = document.querySelector('.thread-list table');
  table.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  assert.deepEqual(shown(document), ['thread-3']);
  document.querySelector('.thread-list table').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }));
  assert.deepEqual(shown(document), ['thread-2']);
  dom.window.close();
});

test('#post-<id> and #thread-<id> pick the thread that holds them', async () => {
  const a = await setup({ url: 'http://127.0.0.1:8787/#post-10', storage: { 'agent-comms-thread': '3' } });
  assert.deepEqual(shown(a.document), ['thread-1']);
  assert.ok(a.document.getElementById('post-10').classList.contains('highlight'));
  a.dom.window.close();
  const b = await setup({ url: 'http://127.0.0.1:8787/#thread-3' });
  assert.deepEqual(shown(b.document), ['thread-3']);
  b.dom.window.close();
});

test('a thread with posts waiting on the human is flagged in the list', async () => {
  const { dom, document } = await setup({ needsYou: [post(10, 1, 5, { needs_response: true })] });
  assert.match(document.querySelector('tr[data-thread="1"]').textContent, /1 needs you/);
  assert.doesNotMatch(document.querySelector('tr[data-thread="2"]').textContent, /needs you/);
  dom.window.close();
});

// ---------------------------------------------------------------- status dots
const MINUTES_AGO = m => new Date(Date.now() - m * 60000).toISOString();
async function dots({ threads, runs = [], storage = {}, needsYou = [], url = 'http://127.0.0.1:8787/' }) {
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', beforeParse(win) {
    for (const [k, v] of Object.entries(storage)) win.localStorage.setItem(k, v);
    win.fetch = async u => {
      if (u === '/api/whoami') return ok({ name: 'human', is_human: true });
      if (u.startsWith('/api/state')) return ok({ me: { name: 'human', is_human: true }, paused: false,
        threads: threads(u), sessions: [], needs_you: needsYou, limits: {}, authorization_grants: [], agents: [],
        task_categories: [], active_runs: runs });
      return { ok: false, status: 404, statusText: '404', json: async () => ({ message: 'not found' }) };
    };
  } });
  await settle();
  const d = dom.window.document;
  const dot = id => { const n = d.querySelector(`tr[data-thread="${id}"] .dot`); return n ? [n.className.replace('dot ', ''), n.title] : null; };
  return { dom, document: d, win: dom.window, dot };
}
const task = (id, status, lease_state = 'none', owner_agent = 'codex') => ({ id, title: 'T' + id, status, lease_state, owner_agent });

test('status dots: stalled, being worked on or waiting to be picked up, completed (hidden)', async () => {
  const blocked = thread(1, [post(10, 1, 1)]); blocked.tasks = [task(1, 'blocked')];
  const working = thread(2, [post(20, 2, 2)]); working.tasks = [task(2, 'working', 'active')];
  const asked = thread(3, [post(30, 3, 3, { agent: 'claude', to: ['codex'], needs_response: true, created_at: MINUTES_AGO(120) })]);
  const fresh = thread(4, [post(40, 4, 4, { agent: 'claude', to: ['codex'], needs_response: true, created_at: MINUTES_AGO(5) })]);
  const running = thread(5, [post(50, 5, 5, { agent: 'claude', to: ['codex'], needs_response: true, created_at: MINUTES_AGO(120) })]);
  const finished = thread(6, [post(60, 6, 6)]); finished.tasks = [task(3, 'done')];
  const closed = thread(7, [post(70, 7, 7)], 'closed');
  const all = [blocked, working, asked, fresh, running, finished, closed];
  const { dom, document, dot } = await dots({ threads: u => u.includes('closed=true') ? all : all.slice(0, 6),
    runs: [{ thread_id: 5, agent: 'codex', run_id: 's50-codex' }], storage: { 'agent-comms-thread': '4' } });
  assert.deepEqual(dot(1), ['stalled', '1 blocked task']);
  assert.deepEqual(dot(2), ['active', 'codex working on task 2']);
  assert.match(dot(3)[1], /^#30 queued · codex$/);
  assert.equal(dot(3)[0], 'stalled');
  assert.deepEqual(dot(4), ['active', '#40 queued · codex'], 'a fresh ask is pending, not stalled yet');
  assert.deepEqual(dot(5), ['active', 'codex running (dispatcher); #50 queued · codex']);
  assert.ok(!document.querySelector('tr[data-thread="6"]'), 'completed threads are hidden by default');
  assert.match(document.querySelector('.thread-list').textContent, /Show completed/);
  assert.ok(!document.querySelector('header input[type=checkbox]'), 'the toggle moved out of the header');
  const box = document.getElementById('show-completed');
  box.checked = true; box.dispatchEvent(new dom.window.Event('change'));
  await settle();
  assert.deepEqual(dot(6), ['done', 'All tasks done']);
  assert.deepEqual(dot(7), ['done', 'Closed']);
  dom.window.close();
});

test('status dots: blue for posts this browser has not shown, cleared by opening the thread', async () => {
  // First visit: nothing lights up.
  const first = await dots({ threads: () => THREADS });
  assert.equal(first.dot(1), null); assert.equal(first.dot(3), null);
  const seen = first.win.localStorage.getItem('agent-comms-seen');
  first.dom.window.close();
  // Thread 1 gets a new agent post and thread 3 a new human post (one's own posts are never unread).
  const later = [thread(3, [post(30, 3, 10), post(31, 3, 50, { agent: 'human' })]), thread(2, [post(20, 2, 40), post(21, 2, 45)]),
                 thread(1, [post(10, 1, 5), post(11, 1, 55)])];
  const { dom, document, dot } = await dots({ threads: () => later, storage: { 'agent-comms-seen': seen, 'agent-comms-thread': '2' } });
  assert.deepEqual(dot(1), ['unread', 'New posts']);
  assert.equal(dot(3), null);
  document.querySelector('tr[data-thread="1"]').click();
  assert.deepEqual(dot(1), ['done', 'Settled: nothing waiting on anyone'], 'opening the thread marks it seen');
  assert.ok(document.querySelector('tr[data-thread="1"]'), 'the thread being read stays listed');
  document.querySelector('tr[data-thread="2"]').click();
  assert.ok(document.querySelector('tr[data-thread="1"]'), 'threads opened on this page stay listed while you browse');
  const seenNow = dom.window.localStorage.getItem('agent-comms-seen');
  dom.window.close();
  const reload = await dots({ threads: () => later, storage: { 'agent-comms-seen': seenNow, 'agent-comms-thread': '2' } });
  assert.equal(reload.document.querySelector('tr[data-thread="1"]'), null, 'after a reload a settled thread is hidden');
  assert.ok(reload.document.querySelector('tr[data-thread="2"]'), 'the selected thread is always listed');
  reload.dom.window.close();
});

test('status dots: every open thread has one; unclaimed tasks are pending, then stalled', async () => {
  const fresh = thread(1, [post(10, 1, 1)]); fresh.tasks = [{ ...task(1, 'accepted'), updated_at: MINUTES_AGO(5) }];
  const old = thread(2, [post(20, 2, 2)]); old.tasks = [{ ...task(2, 'proposed'), updated_at: MINUTES_AGO(90) }];
  const quiet = thread(3, [post(30, 3, 3)]);
  const { dom, document, dot } = await dots({ threads: () => [fresh, old, quiet],
    storage: { 'agent-comms-seen': JSON.stringify({ 1: 10, 2: 20, 3: 30 }), 'agent-comms-thread': '3' } });
  assert.deepEqual(dot(1), ['active', 'task 1 accepted, not picked up for 5m']);
  assert.deepEqual(dot(2), ['stalled', 'task 2 proposed, not picked up for 2h']);
  assert.deepEqual(dot(3), ['done', 'Settled: nothing waiting on anyone']);
  for (const row of document.querySelectorAll('.thread-list tr')) assert.ok(row.querySelector('.dot'), 'no row without a dot');
  dom.window.close();
});

test('sorting: recent activity (default), newest first, priority; the choice is remembered', async () => {
  const mk = (id, created, last, extra = {}) => Object.assign(thread(id, [post(id * 10, id, last)]),
    { created_at: `2027-01-15T0${created}:00:00+00:00` }, extra);
  const onMe = mk(1, 1, 10);                                                   // waiting on the human
  const onAgent = mk(2, 2, 20); onAgent.tasks = [task(1, 'blocked')];          // stalled on someone else
  const fresh = mk(3, 3, 30);                                                   // unread
  const busy = mk(4, 4, 40); busy.tasks = [task(2, 'working', 'active')];       // being worked on
  const quiet = mk(5, 0, 50);                                                   // nothing to flag; oldest thread
  const all = [onMe, onAgent, fresh, busy, quiet];
  const seen = JSON.stringify({ 1: 10, 2: 20, 3: 0, 4: 40, 5: 50 });
  const { dom, document, win } = await dots({ threads: () => all, needsYou: [post(10, 1, 10)],
    storage: { 'agent-comms-seen': seen, 'agent-comms-thread': '5' } });
  assert.deepEqual(rows(document), [5, 4, 3, 2, 1], 'recent activity by default');
  const pick = v => { const s = document.getElementById('thread-sort'); s.value = v; s.dispatchEvent(new win.Event('change')); };
  pick('priority');
  assert.deepEqual(rows(document), [1, 2, 3, 4, 5]);
  assert.equal(win.localStorage.getItem('agent-comms-sort'), 'priority');
  pick('created');
  assert.deepEqual(rows(document), [4, 3, 2, 1, 5]);
  dom.window.close();
  const again = await dots({ threads: () => all, storage: { 'agent-comms-sort': 'created', 'agent-comms-thread': '5' } });
  assert.equal(again.document.getElementById('thread-sort').value, 'created');
  again.dom.window.close();
});

test('status dots: the last word went to an agent that never replied (request); grace before amber', async () => {
  const quietFor = (id, minutes) => thread(id, [post(id * 10, id, 1, { agent: 'claude-code' }),
    post(id * 10 + 1, id, 2, { agent: 'codex', type: 'request', to: ['claude-code'], created_at: MINUTES_AGO(minutes) })]);
  const answered = thread(4, [post(40, 4, 1, { type: 'request', to: ['claude-code'], created_at: MINUTES_AGO(90) }),
    post(41, 4, 2, { agent: 'claude-code', created_at: MINUTES_AGO(80) })]);
  const seen = JSON.stringify({ 1: 11, 2: 21, 3: 31, 4: 41 });
  const { dom, dot } = await dots({ threads: () => [quietFor(1, 35), quietFor(2, 45), quietFor(3, 300), answered],
    storage: { 'agent-comms-seen': seen, 'agent-comms-thread': '4' } });
  assert.deepEqual(dot(1), ['active', '#11 queued · claude-code'], 'inside the window plus grace');
  assert.deepEqual(dot(2), ['stalled', '#21 queued · claude-code']);
  assert.deepEqual(dot(3), ['stalled', '#31 queued · claude-code']);
  assert.deepEqual(dot(4), ['stalled', '#40 queued · claude-code'], 'an unrelated reply cannot complete the request');
  dom.window.close();
});
