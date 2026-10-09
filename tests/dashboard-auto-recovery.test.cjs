// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-auto-recovery.test.cjs
// Thread status labels for automatic recovery (state.auto_recovery) and unclaimed tasks.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });
const MINUTES_AGO = m => new Date(Date.now() - m * 60000).toISOString();
const INJECTION = '<img src=x onerror="window.pwned=1">';

function post(id, threadId, extra = {}) {
  return { id, seq: id, thread_id: threadId, agent: 'human', session_id: 1, type: 'request', body: 'post ' + INJECTION,
    to: [], needs_response: false, task_id: null, refs: [], sealed: false, created_at: MINUTES_AGO(180), ...extra };
}
function thread(id, extra = {}) {
  return { id, title: 'Thread ' + id, project: '/repo/app', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], task_counts: {}, posts: [post(id * 100, id)], created_at: MINUTES_AGO(300), ...extra };
}
const task = (id, extra = {}) => ({ id, title: 'T' + id + INJECTION, status: 'working', lease_state: 'expired',
  owner_agent: 'codex', owner_session: 70, created_by: 'codex', intends_files: [], depends_on: [], events: [],
  created_at: MINUTES_AGO(200), updated_at: MINUTES_AGO(200), ...extra });
const pickup = (extra = {}) => ({ waiting: [], processing: [], blocked: [], complete: false, ...extra });
const abandoned = (extra = {}) => ({ kind: 'abandoned', task_id: 22, thread_id: 1, agent: 'codex', owner_session: 70,
  post_id: 105, state: 'sent', reason: null, detail: null, escalation_post_id: null, held_request_ids: [102],
  sent_at: MINUTES_AGO(5), escalated_at: null, ...extra });

// window.confirm/prompt/alert throw: the embedded browser the human uses blocks them.
async function setup({ threads, autoRecovery = [], runs = [], needsYou = [], human = true }) {
  const prompts = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', beforeParse(win) {
    for (const name of ['confirm', 'prompt', 'alert'])
      win[name] = text => { prompts.push([name, text]); throw new Error(`window.${name} must not be called`); };
    win.fetch = async (u) => {
      const me = human ? { name: 'human', is_human: true } : { name: 'codex', is_human: false };
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, paused: false, threads, needs_you: needsYou, sessions: [],
        limits: {}, authorization_grants: [], task_categories: [], active_runs: runs,
        ...(human ? { auto_recovery: autoRecovery } : {}),
        agents: [{ name: 'human', is_human: 1 }, { name: 'claude', is_human: 0 }, { name: 'codex', is_human: 0 }] });
      return ok({});
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, prompts };
}
function dot(document, id, kind) {
  const node = document.querySelector(`li[data-thread="${id}"] .dot`);
  assert.ok(node, `thread ${id} has a status dot`);
  assert.ok(node.classList.contains(kind), `expected ${kind}, got ${node.className}: ${node.title}`);
  return node.title;
}
const pick = async (document, id) => { document.querySelector(`li[data-thread="${id}"]`).click(); await settle(10); };

test('a pending automatic recovery is the agent\'s turn, not a stall', async () => {
  const held = { post_id: 102, recipient: 'codex', assigned_agent: 'codex', assigned_session: 70, state: 'started',
    reason: 'Owner acknowledgement is stale; no current task lease confirms continued work', deadline_at: null, overdue: false };
  const t = thread(1, { tasks: [task(22)], pickup: pickup({ blocked: [held],
    waiting: [{ post_id: 105, recipient: 'codex', assigned_agent: 'codex', assigned_session: null, state: 'queued',
      reason: '', deadline_at: new Date(Date.now() + 30 * 60000).toISOString(), overdue: false }] }) });
  const page = await setup({ threads: [t], autoRecovery: [abandoned()] });
  const label = dot(page.document, 1, 'unread');
  assert.match(label, /automatic recovery sent to codex for task 22 \(#105\)/);
  assert.match(label, /#102 · codex s70: held by an abandoned session; automatic recovery sent to codex/);
  assert.doesNotMatch(label, /A task lease expired/);
  await pick(page.document, 1);
  assert.equal(page.document.getElementById('unstick'), null, 'nothing to unstick while the recovery is pending');
  page.dom.window.close();

  // The launched run acknowledged the recovery request (processing): grey, with the recovery named.
  const started = { ...t, pickup: pickup({ blocked: [held], processing: [{ post_id: 105, recipient: 'codex',
    assigned_agent: 'codex', assigned_session: 71, state: 'started', reason: '', deadline_at: null, overdue: false }] }) };
  const running = await setup({ threads: [started], autoRecovery: [abandoned()],
    runs: [{ thread_id: 1, agent: 'codex', run_id: 's9-codex', started_at: MINUTES_AGO(1) }] });
  assert.match(dot(running.document, 1, 'active'), /automatic recovery sent to codex for task 22/);
  running.dom.window.close();
});

test('a recovery that did not take waits on the human with the precise reason', async () => {
  const note = post(110, 1, { type: 'status', needs_response: true, body: 'Automatic recovery did not take ' + INJECTION });
  const t = thread(1, { tasks: [task(22)], posts: [post(100, 1), note], pickup: pickup() });
  const page = await setup({ threads: [t], needsYou: [note], autoRecovery: [abandoned({ state: 'escalated',
    reason: 'its recovery request #105 to codex is blocked', detail: 'Runner ended ' + INJECTION,
    escalation_post_id: 110, escalated_at: MINUTES_AGO(1) })] });
  const label = dot(page.document, 1, 'stalled');
  assert.match(label, /automatic recovery of task 22 failed: its recovery request #105 to codex is blocked: Runner ended <img src=x onerror="window\.pwned=1"> — needs you/);
  assert.match(label, /Waiting on you: post #110/);
  assert.equal(page.win.pwned, undefined, 'agent text stays text');
  assert.equal(page.document.querySelectorAll('img').length, 0);
  await pick(page.document, 1);
  const b = page.document.getElementById('unstick');
  assert.ok(b && b.title.includes('codex'), 'the human can still Unstick by hand');
  assert.deepEqual(page.prompts, []);
  page.dom.window.close();
});

test('a browser-denied escalation names the request and launches nothing', async () => {
  const note = post(111, 1, { type: 'status', needs_response: true });
  const t = thread(1, { tasks: [task(22)], posts: [post(100, 1), note], pickup: pickup() });
  const page = await setup({ threads: [t], needsYou: [note], autoRecovery: [abandoned({ state: 'escalated', post_id: null,
    escalation_post_id: 111,
    reason: 'request #102 is held by a browser policy denial; a human permission change is needed, so nothing was launched' })] });
  assert.match(dot(page.document, 1, 'stalled'),
    /automatic recovery of task 22 not launched: request #102 is held by a browser policy denial; .* — needs you/);
  page.dom.window.close();
});

test('an accepted task nobody claimed names its creator', async () => {
  const stale = thread(1, { tasks: [task(44, { status: 'accepted', lease_state: 'none', owner_agent: null, owner_session: null,
    updated_at: MINUTES_AGO(125) })], pickup: pickup() });
  const fresh = thread(2, { tasks: [task(45, { status: 'accepted', lease_state: 'none', owner_agent: null, owner_session: null,
    updated_at: MINUTES_AGO(5) })], pickup: pickup() });
  const byHuman = thread(3, { tasks: [task(46, { status: 'accepted', lease_state: 'none', owner_agent: null, owner_session: null,
    created_by: 'human' })], pickup: pickup() });
  const sent = thread(4, { tasks: [task(47, { status: 'accepted', lease_state: 'none', owner_agent: null, owner_session: null })],
    pickup: pickup() });
  const page = await setup({ threads: [stale, fresh, byHuman, sent], autoRecovery: [
    { ...abandoned(), kind: 'unclaimed', task_id: 47, thread_id: 4, owner_session: null, post_id: 106 }] });
  assert.match(dot(page.document, 1, 'stalled'), /task 44 unclaimed for 2h \(created by codex\)/);
  await pick(page.document, 1);
  assert.ok(page.document.getElementById('unstick').title.includes('codex'), 'Unstick asks the creator');
  assert.match(dot(page.document, 2, 'unread'), /task 45 accepted, awaiting agent pickup/);
  assert.match(dot(page.document, 3, 'stalled'), /task 46 accepted, awaiting agent pickup/);
  assert.match(dot(page.document, 4, 'unread'), /automatic recovery sent to codex for unclaimed task 47 \(#106\)/);
  page.dom.window.close();
});

test('agents never get automatic recovery records and see the plain status', async () => {
  const t = thread(1, { tasks: [task(22)], pickup: pickup() });
  const page = await setup({ threads: [t], human: false, autoRecovery: [abandoned()] });
  assert.match(dot(page.document, 1, 'stalled'), /A task lease expired/);
  page.dom.window.close();
});

test('the page trusts the server for which escalations are open (needs_you in the snapshot is capped)', async () => {
  const t = thread(1, { tasks: [task(22)], pickup: pickup() });
  // Handled: the server no longer lists it, so the plain labels apply.
  const handled = await setup({ threads: [t], autoRecovery: [] });
  const plain = dot(handled.document, 1, 'stalled');
  assert.doesNotMatch(plain, /automatic recovery/);
  assert.match(plain, /A task lease expired/);
  handled.dom.window.close();
  // Open, but its post is beyond the snapshot's capped needs_you list: still shown.
  const open = await setup({ threads: [t], needsYou: [], autoRecovery: [abandoned({ state: 'escalated',
    reason: 'its recovery request #105 to codex is blocked', escalation_post_id: 110 })] });
  assert.match(dot(open.document, 1, 'stalled'), /automatic recovery of task 22 failed: .* — needs you/);
  open.dom.window.close();
});
