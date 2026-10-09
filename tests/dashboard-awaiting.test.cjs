// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-awaiting.test.cjs
// Threads whose open work only waits on tasks in other threads: "Awaiting #N" (blue, or amber when that thread is
// stalled) instead of Unstick; the "Waits on task #" control; and a source thread whose prevention proposal went to the
// prevention inbox is not left stalled or waiting on the human.
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
  return { id, seq: id, thread_id: threadId, agent: 'human', session_id: 1, type: 'status', body: 'post ' + INJECTION,
    to: [], needs_response: false, task_id: null, refs: [], sealed: false, created_at: MINUTES_AGO(180), ...extra };
}
function thread(id, extra = {}) {
  return { id, title: 'Thread ' + id, project: '/repo/app', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], task_counts: {}, posts: [post(id * 100, id)], created_at: MINUTES_AGO(300), ...extra };
}
const task = (id, extra = {}) => ({ id, title: 'T' + id + INJECTION, status: 'accepted', lease_state: 'none',
  owner_agent: null, owner_session: null, created_by: 'codex', intends_files: [], depends_on: [], waiting_on: [],
  events: [], created_at: MINUTES_AGO(200), updated_at: MINUTES_AGO(200), ...extra });
const dep = (id, threadId, status = 'working') => ({ id, thread_id: threadId, status, title: 'Fix ' + INJECTION,
  thread_status: 'open' });
const waits = (id, deps, extra = {}) => task(id, { depends_on: deps.map(d => d.id), waiting_on: deps, ...extra });
const working = (id, extra = {}) => task(id, { status: 'working', lease_state: 'active', owner_agent: 'claude',
  owner_session: 4, created_by: 'human', ...extra });

async function setup({ threads, needsYou = [], reply = () => ok({}), human = true, hash = '' }) {
  const calls = [], prompts = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/' + hash, runScripts: 'dangerously', beforeParse(win) {
    for (const name of ['confirm', 'prompt', 'alert'])
      win[name] = text => { prompts.push([name, text]); throw new Error(`window.${name} must not be called`); };
    win.fetch = async (u, opts = {}) => {
      const me = human ? { name: 'human', is_human: true } : { name: 'codex', is_human: false };
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, paused: false, threads, needs_you: needsYou, sessions: [],
        limits: {}, authorization_grants: [], task_categories: [], active_runs: [], auto_recovery: [],
        agents: [{ name: 'human', is_human: 1 }, { name: 'claude', is_human: 0 }, { name: 'codex', is_human: 0 }] });
      calls.push({ u, method: opts.method, body: opts.body ? JSON.parse(opts.body) : undefined });
      return reply(u);
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, calls, prompts };
}
const row = (document, id) => document.querySelector(`li[data-thread="${id}"]`);
const pick = async (document, id) => { row(document, id).click(); await settle(10); };

test('a thread waiting only on another thread shows a blue Awaiting button, not Unstick', async () => {
  // Thread 1: an unclaimed task and a blocked task (its lease expired too), both waiting on task 9 in thread 2, which
  // claude is working on. Without the dependency both would be stalls with Unstick.
  const t1 = thread(1, { tasks: [waits(5, [dep(9, 2)]),
    waits(6, [dep(9, 2)], { status: 'blocked', lease_state: 'expired', owner_agent: 'codex', owner_session: 3 })] });
  const t2 = thread(2, { tasks: [working(9, { title: 'Fix ' + INJECTION })] });
  const { dom, document, win, prompts } = await setup({ threads: [t1, t2] });
  const r = row(document, 1);
  assert.equal(r.querySelector('.unstick-btn'), null, 'no Unstick for awaiting work');
  const b = r.querySelector('.awaiting-btn');
  assert.ok(b, 'Awaiting button');
  assert.equal(b.textContent, 'Awaiting #2');
  assert.ok(!b.classList.contains('stalled'), 'blue while the blocking thread is moving');
  assert.equal(b.title, 'Waiting on task 9: Fix ' + INJECTION + ' (thread #2, working). Opens thread #2.');
  const d = r.querySelector('.dot');
  assert.ok(d.classList.contains('awaiting') && !d.classList.contains('blocked'), d.className);
  assert.match(d.title, /^task 5 awaits task 9 \(thread #2, working\); task 6 awaits task 9/);
  assert.equal(win.pwned, undefined);
  assert.equal(document.querySelector('img'), null, 'titles are never parsed as HTML');
  assert.deepEqual(prompts, []);
  dom.window.close();
});

test('the button and dot turn amber when the blocking thread is itself stalled', async () => {
  const t1 = thread(1, { tasks: [waits(5, [dep(9, 2, 'blocked')])] });
  const t2 = thread(2, { tasks: [working(9, { status: 'blocked', lease_state: 'active', owner_agent: 'claude' })] });
  const { dom, document } = await setup({ threads: [t1, t2] });
  const b = row(document, 1).querySelector('.awaiting-btn');
  assert.ok(b.classList.contains('stalled'), 'amber: thread 2 is stalled');
  assert.match(b.title, /That thread is stalled\./);
  assert.ok(row(document, 1).querySelector('.dot').classList.contains('blocked'));
  assert.ok(row(document, 2).querySelector('.unstick-btn'), 'the stalled thread itself offers Unstick');
  dom.window.close();
});

test('several dependencies: the first is named, the rest counted, stalled ones first', async () => {
  const t1 = thread(1, { tasks: [waits(5, [dep(9, 2), dep(11, 3, 'blocked')]), waits(6, [dep(12, 3, 'blocked')])] });
  const t2 = thread(2, { tasks: [working(9)] });
  const t3 = thread(3, { tasks: [working(11, { status: 'blocked' }), working(12, { status: 'blocked' })] });
  const { dom, document } = await setup({ threads: [t1, t2, t3] });
  const b = row(document, 1).querySelector('.awaiting-btn');
  assert.equal(b.textContent, 'Awaiting #3 +2', 'the stalled thread #3 is shown first');
  assert.ok(b.classList.contains('stalled'));
  assert.match(b.title, /^Waiting on task 11: .* \(thread #3, blocked\)\. That thread is stalled\. Also waiting on 2 more tasks: task 12 \(thread #3\), task 9 \(thread #2\)\. Opens thread #3\.$/);
  // Without a stalled blocker it is blue and keeps the order.
  const calm = thread(1, { tasks: [waits(5, [dep(9, 2), dep(13, 2)])] });
  const t2b = thread(2, { tasks: [working(9), working(13)] });
  const second = await setup({ threads: [calm, t2b] });
  const b2 = row(second.document, 1).querySelector('.awaiting-btn');
  assert.equal(b2.textContent, 'Awaiting #2 +1');
  assert.ok(!b2.classList.contains('stalled'));
  second.dom.window.close();
  dom.window.close();
});

test('a click opens the blocking thread', async () => {
  const t1 = thread(1, { tasks: [waits(5, [dep(9, 2)])] });
  const t2 = thread(2, { tasks: [working(9)] });
  const { dom, document, win } = await setup({ threads: [t1, t2], hash: '#thread-1' });
  await pick(document, 1);
  assert.equal(document.querySelector('#thread-1 h2').textContent, '#1 Thread 1');
  assert.ok(document.getElementById('awaiting'), 'the thread header has the button too');
  row(document, 1).querySelector('.awaiting-btn').click();
  await settle(20);
  assert.equal(win.location.hash, '#thread-2');
  assert.ok(document.getElementById('thread-2'), 'thread 2 is open in the pane');
  assert.equal(document.getElementById('thread-1'), null);
  dom.window.close();
});

test('the human sets what a task waits on from its row; bad input never reaches the server', async () => {
  const t1 = thread(1, { tasks: [task(5)] });
  const { dom, document, calls, prompts } = await setup({ threads: [t1], reply: u => u === '/api/tasks/5/transition'
    ? ok({}) : fail(404, 'nf') });
  await pick(document, 1);
  const form = document.querySelector('form[data-waits-on="5"]');
  assert.ok(form, 'the control is on the task row');
  assert.equal(form.querySelector('label').textContent, 'Waits on task #');
  const input = form.querySelector('input');
  input.value = 'abc';
  form.querySelector('button').click();
  await settle(20);
  assert.equal(calls.filter(c => c.u.startsWith('/api/tasks/')).length, 0);
  assert.match(document.querySelector('.waits-on-error').textContent, /Give task numbers/);
  document.querySelector('form[data-waits-on="5"] input').value = '31, #32';
  document.querySelector('form[data-waits-on="5"] button').click();
  await settle(40);
  const call = calls.find(c => c.u === '/api/tasks/5/transition');
  assert.equal(call.method, 'POST');
  assert.deepEqual(call.body, { depends_on: [31, 32], note: 'via dashboard' });
  assert.equal(document.querySelector('.waits-on-error'), null, 'cleared once saved');
  assert.deepEqual(prompts, []);
  dom.window.close();
});

test('the server refusal (a cycle) shows under the control; agents get no control', async () => {
  const t1 = thread(1, { tasks: [task(5)] });
  const { dom, document } = await setup({ threads: [t1], reply: () => fail(400, 'depends_on would create a cycle') });
  await pick(document, 1);
  document.querySelector('form[data-waits-on="5"] input').value = '7';
  document.querySelector('form[data-waits-on="5"] button').click();
  await settle(40);
  assert.match(document.querySelector('.waits-on-error').textContent, /would create a cycle/);
  dom.window.close();
  const agent = await setup({ threads: [thread(1, { tasks: [task(5)] })], human: false });
  await pick(agent.document, 1);
  assert.equal(agent.document.querySelector('form[data-waits-on]'), null);
  agent.dom.window.close();
});

test('a source thread whose prevention proposal went to the inbox is neither stalled nor waiting on the human', async () => {
  // Thread 1: the human's Unstick request to codex (finished), codex's finding. Thread 14 (the prevention inbox):
  // codex's proposal to claude, which claude has not picked up yet. Nothing is in Needs you.
  const unstickReq = post(101, 1, { type: 'request', to: ['codex'], needs_response: true,
    body: 'Unstick: this thread is stalled on you', requests: [{ post_id: 101, recipient: 'codex', state: 'finished',
      assigned_agent: 'codex', assigned_session: 3, version: 2, updated_at: MINUTES_AGO(5) }] });
  const finding = post(102, 1, { agent: 'codex', type: 'finding', body: 'Cause ' + INJECTION, created_at: MINUTES_AGO(6),
    refs: [{ kind: 'commit', path: '/repo/app', rev: 'abc1234' }] });
  const source = thread(1, { posts: [unstickReq, finding] });
  const proposal = post(1401, 14, { agent: 'codex', type: 'proposal', to: ['claude'], needs_response: true,
    created_at: MINUTES_AGO(4), prevention_for: { request_post_id: 101, source_thread_id: 1 },
    requests: [{ post_id: 1401, recipient: 'claude', state: 'queued', assigned_agent: 'claude', assigned_session: null,
      version: 0, updated_at: MINUTES_AGO(4) }] });
  const inbox = thread(14, { posts: [post(1400, 14), proposal] });
  const { dom, document, win } = await setup({ threads: [source, inbox] });
  const s1 = win.threadStatus(source, true);
  assert.ok(!s1 || !['stalled'].includes(s1.kind), `source thread status: ${s1 && s1.kind}`);
  assert.ok(!s1 || !s1.needsMe);
  assert.equal(row(document, 1), null, 'settled: hidden unless "Show completed" is on');
  const s14 = win.threadStatus(inbox, true);
  assert.equal(s14.kind, 'unread', 'the inbox waits for claude to pick it up');
  assert.ok(!s14.needsMe);
  assert.equal(row(document, 14).querySelector('.needs-chip'), null, 'nothing needs the human');
  assert.equal(row(document, 14).querySelector('.unstick-btn'), null);
  dom.window.close();
});

test('a dependency in a closed thread is not awaited: amber, "blocking thread closed"', async () => {
  // The server lists it in blocked_by_closed, not waiting_on.
  const closedDep = { ...dep(9, 2, 'working'), thread_status: 'closed' };
  const t1 = thread(1, { tasks: [task(5, { depends_on: [9], waiting_on: [], blocked_by_closed: [closedDep],
    created_at: MINUTES_AGO(5), updated_at: MINUTES_AGO(5) })] });
  const { dom, document, win } = await setup({ threads: [t1] });
  const st = win.threadStatus(t1, true);
  assert.equal(st.kind, 'stalled');
  assert.match(st.label, /task 5 waits on task 9 in thread #2, which is closed \(blocking thread closed\)/);
  const r = row(document, 1);
  assert.ok(r.querySelector('.dot').classList.contains('stalled'));
  const b = r.querySelector('.awaiting-btn');
  assert.ok(b && b.classList.contains('stalled'), 'amber Awaiting button');
  assert.equal(b.textContent, 'Awaiting #2');
  assert.match(b.title, /\(thread #2, working\): blocking thread closed\./);
  dom.window.close();
});

test('a fresh pickup in the thread outranks awaiting', async () => {
  const fresh = post(150, 1, { agent: 'claude', type: 'request', to: ['codex'], needs_response: true,
    created_at: MINUTES_AGO(2) });
  const t1 = thread(1, { tasks: [waits(5, [dep(9, 2)])], posts: [post(100, 1), fresh] });
  const t2 = thread(2, { tasks: [working(9)] });
  const { dom, document, win } = await setup({ threads: [t1, t2] });
  const st = win.threadStatus(t1, true);
  assert.equal(st.kind, 'unread');
  assert.match(st.label, /#150 · codex: awaiting agent pickup/);
  assert.equal(row(document, 1).querySelector('.awaiting-btn'), null);
  dom.window.close();
});

test('a dependency in the same thread reads "Awaiting task #N"', async () => {
  // Task 5 waits on task 6 in the same thread, which another thread's agent is not working on: awaiting.
  const t1 = thread(1, { tasks: [waits(5, [dep(6, 1, 'accepted')]),
    task(6, { created_by: 'human', status: 'proposed', created_at: MINUTES_AGO(5), updated_at: MINUTES_AGO(5) })] });
  const { dom, win } = await setup({ threads: [t1] });
  const st = win.threadStatus(t1, true);
  assert.equal(st.kind, 'unread', 'task 6 itself awaits pickup, which comes first');
  const fake = { kind: 'awaiting', blocking: [{ ...dep(6, 1, 'accepted') }], blockerStalled: false, label: 'x' };
  const b = win.awaitingButton(t1, fake, 'row');
  assert.equal(b.textContent, 'Awaiting task #6');
  dom.window.close();
});
