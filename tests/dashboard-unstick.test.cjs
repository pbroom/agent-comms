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

// window.confirm/prompt/alert throw (and are recorded): the embedded browser the human uses blocks them, and Unstick
// must not call any of them. `sessions` may be a function, re-read on every /api/state (sessions appear later).
async function setup({ threads, human = true, runs = [], needsYou = [], reply = () => ok({}), sessions = [] }) {
  const calls = [], prompts = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', beforeParse(win) {
    for (const name of ['confirm', 'prompt', 'alert'])
      win[name] = text => { prompts.push([name, text]); throw new Error(`window.${name} must not be called`); };
    win.fetch = async (u, opts = {}) => {
      const me = human ? { name: 'human', is_human: true } : { name: 'codex', is_human: false };
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, paused: false, threads, needs_you: needsYou,
        sessions: typeof sessions === 'function' ? sessions() : sessions,
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

test('one click sends at once (no confirm), and says what happens next', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex', 'claude'], needs_response: true })]);
  const reply = u => u === '/api/threads/1/unstick' ? ok({ post_id: 51, thread_id: 1, agents: ['codex', 'claude'],
    rule_id: 7, dispatcher_running: true, paused: false, live_agents: ['claude'], sessions: [], no_runner: [], reasons: [] }) : fail(404, 'nf');
  const { dom, document, calls, prompts, win } = await setup({ threads: [t], reply });
  assert.match(document.getElementById('unstick').title, /^Ask codex and claude to find and fix why this thread is stuck\. Sends at once/);
  document.getElementById('unstick').click();
  await settle();
  assert.deepEqual(prompts, [], 'no window.confirm/prompt/alert');
  assert.equal(calls.filter(c => c.u === '/api/threads/1/unstick').length, 1);
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

test('dispatcher not running, single agent wording, a double click posts once', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex'], needs_response: true })]);
  let release;
  const gate = new Promise(r => { release = r; });
  const reply = async () => { await gate; return ok({ post_id: 52, agents: ['codex'], rule_id: 8, dispatcher_running: false,
    paused: false, live_agents: [], sessions: [], no_runner: [], reasons: [] }); };
  const { dom, document, calls, prompts } = await setup({ threads: [t], reply });
  assert.match(document.getElementById('unstick').title, /launches it once if not already running/);
  document.getElementById('unstick').click();
  document.getElementById('unstick').click();   // busy: disabled and ignored
  release();
  await settle();
  assert.deepEqual(prompts, []);
  assert.equal(calls.filter(c => c.u === '/api/threads/1/unstick').length, 1);
  assert.equal(document.getElementById('unstick-result').firstChild.textContent,
    "Sent to codex (post #52). The dispatcher isn't running — start it with `board dispatch run` to launch codex.");
  assert.equal(document.getElementById('unstick-sessions').textContent, 'No session has picked it up yet.');
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

test('the list row offers Unstick under the amber dot; it opens that thread and shows the result there', async () => {
  const quiet = thread(1, [post(10, 1, { to: ['claude'], needs_response: true, created_at: MINUTES_AGO(1) })]);
  const stuck = thread(2, [post(20, 2, { to: ['codex'], needs_response: true })]);
  const youOnly = thread(3, [post(30, 3, { needs_response: true })]);
  const reply = u => u === '/api/threads/2/unstick' ? ok({ post_id: 53, agents: ['codex'], rule_id: 9,
    dispatcher_running: true, paused: false, live_agents: [], sessions: [], no_runner: [], reasons: [] }) : fail(404, 'nf');
  const { dom, document, calls, prompts } = await setup({ threads: [quiet, stuck, youOnly], reply,
    needsYou: [post(30, 3, { needs_response: true })] });
  const buttons = [...document.querySelectorAll('.thread-list button.unstick-row')].map(b => b.dataset.unstick);
  assert.deepEqual(buttons, ['2'], 'only rows stalled on an agent get the button');
  const cell = document.querySelector('tr[data-thread="2"] td.when');
  assert.ok(cell.querySelector('.dot.stalled').compareDocumentPosition(cell.querySelector('.unstick-row')) & 4, 'under the dot');
  document.querySelector('button.unstick-row').click();
  await settle();
  assert.deepEqual(prompts, [], 'no window.confirm/prompt/alert');
  assert.equal(calls.filter(c => c.u === '/api/threads/2/unstick').length, 1);
  assert.ok(calls.some(c => c.u === '/api/threads/2/unstick' && c.method === 'POST'));
  assert.ok(document.getElementById('thread-2'), 'the stalled thread is opened');
  assert.match(document.getElementById('unstick-result').textContent, /Sent to codex \(post #53\)/);
  dom.window.close();
});

test('the note and the Sessions panel show which sessions received it, with their conversation links', async () => {
  const CLAUDE_ID = '3f2c8a5e-1b7d-4c9e-a0f1-6d5e4c3b2a19', CODEX_ID = '01a11421-ce71-7370-a005-a5179018a42d';
  const conv = (url) => ({ app: 'x', url, resume_command: 'x', cwd: '/repo/app' });
  const sess = (id, agent, startedMinAgo, conversation = null) => ({ id, agent, runtime: agent, project: '/repo/app',
    worktree: null, started_at: new Date(Date.now() - startedMinAgo * 60000).toISOString(), last_seen: MINUTES_AGO(0),
    conversation });
  const live = sess(10, 'claude', 60, conv(`claude://resume?session=${CLAUDE_ID}`));
  const oldCodex = sess(11, 'codex', 90);                 // a target agent's session, neither live nor new
  const other = sess(12, 'grok', 1);                      // new, but not a target
  const launched = { ...sess(13, 'codex', 0, conv(`codex://threads/${CODEX_ID}`)),
    started_at: new Date(Date.now() + 2000).toISOString() };   // the dispatcher's launch registers after the click
  let sessions = [live, oldCodex, other];
  const t = thread(1, [post(10, 1, { to: ['codex', 'claude'], needs_response: true })]);
  const reply = u => u === '/api/threads/1/unstick' ? ok({ post_id: 61, thread_id: 1, agents: ['codex', 'claude'],
    rule_id: 3, dispatcher_running: true, paused: false, live_agents: ['claude'], sessions: [10], no_runner: [], reasons: [] })
    : fail(404, 'nf');
  const { dom, document } = await setup({ threads: [t], reply, sessions: () => sessions });
  try {
  const chips = () => [...document.querySelectorAll('#sessions [data-session]')]
    .map(n => [n.dataset.session, [...n.querySelectorAll('.unstick-chip')].map(c => `${c.textContent} ${c.getAttribute('href')}`)]);
  assert.deepEqual(chips(), [['10', []], ['11', []], ['12', []]], 'no chips before any Unstick');
  document.getElementById('unstick').click();
  await settle();
  const line = document.getElementById('unstick-sessions');
  assert.match(line.textContent, /^Received in:\s*s10 claude/);
  assert.deepEqual([...line.querySelectorAll('a')].map(a => [a.textContent, a.getAttribute('href')]),
    [['Open in Claude', `claude://resume?session=${CLAUDE_ID}`]]);
  assert.deepEqual(chips(), [['10', ['Unstick #61 #post-61']], ['11', []], ['12', []]]);
  assert.ok(document.querySelector('#sessions [data-session="10"] .convo a'), 'its conversation link is shown next to the chip');

  // A dispatcher launch registers a new codex session: it shows up on the next refresh, in the server's order.
  sessions = [launched, live, oldCodex, other];
  document.getElementById('show-completed').click();   // any action that refreshes
  await settle();
  assert.deepEqual(chips(), [['13', ['Unstick #61 #post-61']], ['10', ['Unstick #61 #post-61']], ['11', []], ['12', []]]);
  assert.deepEqual([...document.querySelectorAll('#unstick-sessions [data-session]')].map(n => n.dataset.session), ['13', '10']);
  assert.deepEqual([...document.querySelectorAll('#unstick-sessions a')].map(a => a.textContent), ['Open in ChatGPT', 'Open in Claude']);
  } finally { dom.window.close(); }   // a failed assertion must not leave the page's refresh timer running
});

test('after a click the button reads "Sending…" until the agent restarts, then leaves', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex'], needs_response: true })]);
  const runs = [];
  let unstickCalls = 0;
  const reply = u => u === '/api/threads/1/unstick' ? (unstickCalls++, ok({ post_id: 61, agents: ['codex'], rule_id: 9,
    dispatcher_running: true, paused: false, live_agents: [], no_runner: [], sessions: [], reasons: [] })) : fail(404, 'nf');
  const { dom, document, win } = await setup({ threads: [t], reply, runs });
  const rowButton = () => document.querySelector('button.unstick-row');
  assert.ok(rowButton().classList.contains('unstick-btn'), 'shared style with a hover state');
  rowButton().click();
  await settle();
  for (const b of [rowButton(), document.getElementById('unstick')]) {
    assert.equal(b.textContent, 'Sending…');
    assert.ok(b.classList.contains('sending') && b.disabled, 'tracing outline, not clickable again');
  }
  t.posts.push(post(61, 1, { agent: 'human', to: ['codex'], needs_response: true, created_at: new Date().toISOString() }));
  await win.refresh(); await settle(20);
  assert.equal(rowButton() && rowButton().textContent, 'Sending…', 'still waiting: the request alone is not a restart');
  rowButton().click(); await settle(20);
  assert.equal(unstickCalls, 1, 'no second send while waiting');
  runs.push({ thread_id: 1, agent: 'codex', run_id: 's61-codex' });
  await win.refresh(); await settle(20);
  assert.equal(document.querySelector('.unstick-btn.sending'), null, 'the launch clears it');
  assert.equal(rowButton(), null, 'and the thread is no longer stalled, so the button leaves');
  dom.window.close();
});

test('"Sending…" stops at once when nothing can restart (dispatcher not running) and on errors', async () => {
  const t = thread(1, [post(10, 1, { to: ['codex'], needs_response: true })]);
  const down = await setup({ threads: [t], reply: () => ok({ post_id: 62, agents: ['codex'], rule_id: 9,
    dispatcher_running: false, paused: false, live_agents: [], no_runner: [], sessions: [], reasons: [] }) });
  down.document.querySelector('button.unstick-row').click(); await down.settle?.() ; await settle();
  assert.equal(down.document.querySelector('.unstick-btn.sending'), null);
  assert.match(down.document.getElementById('unstick-result').textContent, /dispatcher isn't running/);
  down.dom.window.close();
  const err = await setup({ threads: [t], reply: () => fail(409, 'unstick was used on this thread less than 2 minutes ago') });
  err.document.querySelector('button.unstick-row').click(); await settle();
  assert.equal(err.document.querySelector('.unstick-btn.sending'), null);
  assert.ok(err.document.querySelector('button.unstick-row'), 'the button is back to "Unstick" so you can retry');
  err.dom.window.close();
});

for (const type of ['status', 'finding', 'request']) {
  for (const needs_response of [false, true]) {
    test(`${type} response intent ${needs_response} controls reply stalls`, async () => {
      const t = thread(1, [post(10, 1, { type, to: ['codex'], needs_response })]);
      const { dom, document } = await setup({ threads: [t] });
      try {
        const shouldStall = needs_response || type === 'request';
        assert.equal(Boolean(document.querySelector('tr[data-thread="1"] .dot.stalled')), shouldStall);
        assert.equal(Boolean(document.getElementById('unstick')), shouldStall);
      } finally {
        dom.window.close();
      }
    });
  }
}

test('human approval statuses still wait on their recipient', async () => {
  const t = thread(1, [post(10, 1, { agent: 'human', type: 'status', to: ['codex'] })]);
  const { dom, document } = await setup({ threads: [t] });
  try {
    assert.ok(document.querySelector('tr[data-thread="1"] .dot.stalled'));
    assert.ok(document.getElementById('unstick'));
  } finally {
    dom.window.close();
  }
});
