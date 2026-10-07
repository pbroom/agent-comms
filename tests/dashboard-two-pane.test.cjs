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
