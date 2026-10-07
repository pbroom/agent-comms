// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-unstick.test.cjs
// The Unstick button in the thread header: shown to the human for stalls that wait on an agent.
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
  return { id, seq: id, thread_id: threadId, agent: 'claude', session_id: 1, type: 'request', body: 'post ' + INJECTION,
    to: [], needs_response: false, task_id: null, refs: [], sealed: false, created_at: MINUTES_AGO(120), ...extra };
}
function thread(id, posts, extra = {}) {
  return { id, title: 'Thread ' + id, project: '/repo/app', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: MINUTES_AGO(300), ...extra };
}
const task = (id, status, lease_state = 'none', owner_agent = 'codex') => ({ id, title: 'T' + id, status, lease_state, owner_agent,
  owner_session: 3, intends_files: [], events: [] });

async function setup({ threads, human = true, runs = [], needsYou = [], reply = () => ok({}), confirmAnswer = true }) {
  const calls = [], prompts = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', beforeParse(win) {
    win.confirm = text => { prompts.push(text); return confirmAnswer; };
    win.fetch = async (u, opts = {}) => {
      const me = human ? { name: 'human', is_human: true } : { name: 'codex', is_human: false };
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, paused: false, threads, sessions: [], needs_you: needsYou,
        limits: {}, authorization_grants: [], task_categories: [], active_runs: runs,
        agents: [{ name: 'human', is_human: 1 }, { name: 'claude', is_human: 0 }, { name: 'codex', is_human: 0 }] });
      calls.push({ u, method: opts.method, headers: opts.headers });
      return reply(u);
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, calls, prompts };
}
const pick = async (document, id) => { document.querySelector(`tr[data-thread="${id}"]`).click(); await settle(10); };

test('the button shows only for stalls that wait on an agent', async () => {
  const asked = thread(1, [post(10, 1, { to: ['codex'], needs_response: true })]);
  const blocked = thread(2, [post(20, 2)], { tasks: [task(1, 'blocked')] });
  const youOnly = thread(3, [post(30, 3, { needs_response: true })]);
  const atCap = thread(4, [post(40, 4)], { agent_posts_since_human: 12 });
  const fresh = thread(5, [post(50, 5, { to: ['codex'], needs_response: true, created_at: MINUTES_AGO(2) })]);
  const running = thread(6, [post(60, 6)], { tasks: [task(2, 'blocked')] });
  const humanOwned = thread(7, [post(70, 7)], { tasks: [task(3, 'blocked', 'none', 'human')] });
  const expired = thread(8, [post(80, 8)], { tasks: [task(4, 'working', 'expired', 'claude')] });
  const { dom, document } = await setup({ threads: [asked, blocked, youOnly, atCap, fresh, running, humanOwned, expired],
    needsYou: [post(30, 3, { needs_response: true })], runs: [{ thread_id: 6, agent: 'codex', run_id: 'r' }] });
  const expect = { 1: 'codex', 2: 'codex', 3: null, 4: null, 5: null, 6: null, 7: null, 8: 'claude' };
  for (const [id, who] of Object.entries(expect)) {
    await pick(document, id);
    const b = document.getElementById('unstick');
    if (who) assert.ok(b && b.title.includes(who), `thread ${id} offers Unstick for ${who}`);
    else assert.equal(b, null, `thread ${id} has no Unstick button`);
  }
  dom.window.close();
});

test('confirm, call the endpoint, and say what happens next', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex', 'claude'], needs_response: true })]);
  const reply = u => u === '/api/threads/1/unstick' ? ok({ post_id: 51, thread_id: 1, agents: ['codex', 'claude'],
    rule_id: 7, dispatcher_running: true, paused: false, live_agents: ['claude'], no_runner: [], reasons: [] }) : fail(404, 'nf');
  const { dom, document, calls, prompts, win } = await setup({ threads: [t], reply });
  document.getElementById('unstick').click();
  await settle();
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /^Ask codex and claude to find and fix why this thread is stuck\? This posts a request as you and, if codex and claude aren't already running, launches each once \(uses their tokens\)\.$/);
  const call = calls.find(c => c.u === '/api/threads/1/unstick');
  assert.equal(call.method, 'POST');
  assert.equal(call.headers['X-Board-Request'], '1');
  const note = document.getElementById('unstick-result');
  assert.equal(note.firstChild.textContent, 'Sent to codex and claude (post #51). The dispatcher will launch codex within seconds. ' +
    'claude is active in a session and will see it there.');
  assert.equal(win.pwned, undefined);
  assert.equal(document.querySelector('img'), null, 'post text is never parsed as HTML');
  dom.window.close();
});

test('dispatcher not running, single agent wording, cancel does nothing', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex'], needs_response: true })]);
  const reply = () => ok({ post_id: 52, agents: ['codex'], rule_id: 8, dispatcher_running: false, paused: false,
    live_agents: [], no_runner: [], reasons: [] });
  const cancelled = await setup({ threads: [t], reply, confirmAnswer: false });
  cancelled.document.getElementById('unstick').click();
  await settle();
  assert.match(cancelled.prompts[0], /^Ask codex to find .* if codex isn't already running, launches it once \(uses its tokens\)\.$/);
  assert.equal(cancelled.calls.length, 0);
  assert.equal(cancelled.document.getElementById('unstick-result'), null);
  cancelled.dom.window.close();
  const { dom, document } = await setup({ threads: [t], reply });
  document.getElementById('unstick').click();
  await settle();
  assert.equal(document.getElementById('unstick-result').firstChild.textContent,
    "Sent to codex (post #52). The dispatcher isn't running — start it with `board dispatch run` to launch codex.");
  document.querySelector('#unstick-result button').click();
  assert.equal(document.getElementById('unstick-result'), null, 'dismissed');
  dom.window.close();
});

test('errors render in the banner', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex'], needs_response: true })]);
  const reply = () => fail(409, 'thread 1 was unstuck less than 2 minutes ago');
  const { dom, document } = await setup({ threads: [t], reply });
  document.getElementById('unstick').click();
  await settle();
  assert.match(document.querySelector('.banner').textContent, /unstuck less than 2 minutes ago/);
  assert.match(document.getElementById('unstick-result').textContent, /^Unstick failed: thread 1 was unstuck/);
  dom.window.close();
});

test('hidden for an agent viewing the board', async () => {
  const t = thread(1, [post(10, 1, { to: ['claude'], needs_response: true })], { tasks: [task(1, 'blocked')] });
  const { dom, document } = await setup({ threads: [t], human: false });
  assert.ok(document.getElementById('thread-1'));
  assert.equal(document.getElementById('unstick'), null);
  dom.window.close();
});
