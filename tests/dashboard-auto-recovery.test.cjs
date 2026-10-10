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

// Recovery waits (autorecover.list_records kind "recovery_wait"): ownership recovery hit a transient blocker; the
// board retries by itself once the owner worktree is free, so it is not the human's turn.
const recoveryWait = (extra = {}) => ({ kind: 'recovery_wait', task_id: null, thread_id: 1, agent: 'codex',
  owner_session: 70, request_post_id: 103, recipient: 'codex', post_id: null, state: 'waiting',
  reason: 'Another live session in the owner worktree has active or unknown activity', detail: null,
  escalation_post_id: null, held_request_ids: [103], covered_post_ids: [120], covered_task_ids: [22],
  retries_used: 0, max_retries: 3, sent_at: null, escalated_at: null, ...extra });

test('a recovery wait shows the automatic retry, not "needs you"', async () => {
  // #103 is held by the abandoned session 70; the blocked attempt left its own request #120 and task 22 blocked.
  const held = { post_id: 103, recipient: 'codex', assigned_agent: 'codex', assigned_session: 70, state: 'started',
    reason: 'on it', deadline_at: null, overdue: false };
  const own = { post_id: 120, recipient: 'codex', assigned_agent: 'codex', assigned_session: 105, state: 'blocked',
    reason: 'Another live session in the owner worktree has active or unknown activity', deadline_at: null, overdue: false };
  const t = thread(1, { tasks: [task(22, { status: 'blocked', lease_state: 'expired' })], pickup: pickup({ blocked: [held, own] }) });
  const page = await setup({ threads: [t], autoRecovery: [recoveryWait()] });
  const label = dot(page.document, 1, 'unread');
  assert.match(label, /#103 · codex: waiting for the worktree to be free; automatic retry 1\/3/);
  assert.doesNotMatch(label, /needs you|blocked task|#120|lease expired/);
  await pick(page.document, 1);
  assert.equal(page.document.getElementById('unstick'), null, 'nothing to unstick while the board retries');
  page.dom.window.close();

  // Relaunched (retry 2 of 3 sent) while the run is going: the board's work in progress.
  const again = await setup({ threads: [t], autoRecovery: [recoveryWait({ state: 'relaunched', retries_used: 2, post_id: 130 })],
    runs: [{ thread_id: 1, agent: 'codex', run_id: 's130-codex', started_at: MINUTES_AGO(1) }] });
  assert.match(dot(again.document, 1, 'active'), /#103 · codex: worktree free; automatic retry 2\/3 sent to codex \(#130\)/);
  again.dom.window.close();

  // A wait for another thread does not hide this thread's own stalls.
  const other = await setup({ threads: [t], autoRecovery: [recoveryWait({ thread_id: 2 })] });
  assert.match(dot(other.document, 1, 'stalled'), /#120 · codex s105: blocked/);
  other.dom.window.close();
});

test('a recovery wait whose retries ran out needs the human with the reason', async () => {
  const note = post(140, 1, { type: 'question', needs_response: true, body: 'Automatic recovery did not take' });
  const t = thread(1, { posts: [post(100, 1), note], pickup: pickup() });
  const page = await setup({ threads: [t], needsYou: [note], autoRecovery: [recoveryWait({ state: 'escalated',
    retries_used: 3, escalation_post_id: 140, escalated_at: MINUTES_AGO(1),
    reason: '3 automatic retries in 24 hours did not recover it ' + INJECTION })] });
  const label = dot(page.document, 1, 'stalled');
  assert.match(label, /#103 · codex: automatic recovery retries ran out: 3 automatic retries in 24 hours did not recover it .* — needs you/);
  assert.equal(page.win.pwned, undefined, 'text stays text');
  await pick(page.document, 1);
  const b = page.document.getElementById('unstick');
  assert.ok(b && b.title.includes('codex'), 'the human can Unstick codex');
  assert.deepEqual(page.prompts, []);
  page.dom.window.close();
});

test('an escalation the dispatcher closed because its stall cleared says so, not "resolved by human"', async () => {
  const note = post(602, 1, { type: 'question', automatic: true, needs_response: true,
    body: 'Automatic recovery did not take ' + INJECTION,
    attention_resolution: { resolved_by: 'human', session_id: 1, evidence_post_ids: [], resolved_at: MINUTES_AGO(1),
      automatic: true, reason: 'Closed automatically by the dispatcher under your board setting '
        + 'auto_recover_stalled_work (not your click): task 23 was claimed or settled (now working) ' + INJECTION } });
  const manual = post(603, 1, { type: 'proposal', agent: 'codex',
    attention_resolution: { resolved_by: 'codex', session_id: 3, evidence_post_ids: [604], resolved_at: MINUTES_AGO(1),
      reason: 'done' } });
  const t = thread(1, { posts: [note, manual], tasks: [task(23, { owner_session: 9, lease_state: 'active' })] });
  const page = await setup({ threads: [t] });
  await pick(page.document, 1);
  const closed = page.document.querySelector('#post-602 .attention-resolution');
  assert.ok(closed, 'the closure is shown on the post');
  assert.match(closed.textContent, /Closed automatically by the dispatcher \(your auto-recovery setting, not your click\)/);
  assert.doesNotMatch(closed.textContent, /resolved by human/);
  assert.match(closed.textContent, /task 23 was claimed or settled/);
  assert.equal(page.win.pwned, undefined, 'the reason is rendered as text');
  assert.match(page.document.querySelector('#post-603 .attention-resolution').textContent, /Attention resolved by codex/);
  assert.deepEqual(page.prompts, []);
  page.dom.window.close();
});
