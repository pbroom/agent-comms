// Agent pickup is request lifecycle state, independent of the human browser's read cursor.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 60) => new Promise(resolve => setTimeout(resolve, ms));
const ago = minutes => new Date(Date.now() - minutes * 60000).toISOString();
const ok = body => ({ ok: true, status: 200, json: async () => body });

function request(recipient, state = 'queued', age = 1) {
  return { recipient, assigned_agent: recipient, assigned_session: state === 'queued' ? null : 4,
    state, reason: '', evidence_post_ids: [], version: state === 'queued' ? 0 : 1, updated_at: ago(age) };
}
function post(id, requests, extra = {}) {
  return { id, seq: id, thread_id: 1, agent: 'claude', session_id: 2, type: 'request', body: 'Please review the fix',
    to: requests.map(r => r.recipient), needs_response: true, task_id: null, refs: [], sealed: false,
    created_at: ago(1), requests, ...extra };
}
function thread(id, posts) {
  return { id, title: `Thread ${id}`, project: '/repo/app', status: 'open', agent_posts_since_human: 0,
    thread_cap: 12, pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: ago(300) };
}
async function setup({ threads, runs = [], issues = [], storage = {} }) {
  const calls = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', beforeParse(win) {
    for (const [key, value] of Object.entries({ 'agent-comms-thread': '1', ...storage })) win.localStorage.setItem(key, value);
    win.fetch = async (url, options = {}) => {
      calls.push({ url, method: options.method || 'GET' });
      if (url === '/api/whoami') return ok({ name: 'human', is_human: true });
      if (url.startsWith('/api/state')) return ok({ me: { name: 'human', is_human: true }, paused: false,
        threads, active_runs: runs, issues, sessions: [], needs_you: [], limits: {}, authorization_grants: [],
        task_categories: [], agents: [{ name: 'human', is_human: true }, { name: 'codex', is_human: false },
          { name: 'claude', is_human: false }] });
      return { ok: false, status: 404, json: async () => ({ message: 'not found' }) };
    };
  } });
  await settle();
  return { dom, document: dom.window.document, win: dom.window, calls };
}
function dot(document, id = 1) {
  const node = document.querySelector(`tr[data-thread="${id}"] .dot`);
  assert.ok(node, `thread ${id} has a status dot`);
  return node;
}
function assertKind(document, kind, id = 1) {
  const node = dot(document, id);
  assert.ok(node.classList.contains(kind), `expected ${kind}, got ${node.className}: ${node.title}`);
  assert.equal(node.getAttribute('aria-label'), node.title, 'the status is available without relying on color');
  return node.title;
}

test('a fresh queued request is blue on first visit and stays blue after human opening and refresh', async () => {
  const target = thread(1, [post(10, [request('codex')])]);
  const other = thread(2, [post(20, [request('claude')], { thread_id: 2, agent: 'human' })]);
  const page = await setup({ threads: [target, other], storage: { 'agent-comms-thread': '2' } });
  try {
    assert.match(assertKind(page.document, 'unread'), /waiting|pickup|pick.?up|queued/i);
    assert.match(dot(page.document).title, /codex/i);
    page.document.querySelector('tr[data-thread="1"]').click();
    assertKind(page.document, 'unread');
    await page.win.refresh();
    assertKind(page.document, 'unread');
    assert.deepEqual(page.calls.filter(c => c.method !== 'GET'), [], 'viewing never acknowledges a request for an agent');
  } finally { page.dom.window.close(); }
});

test('a saved human read cursor cannot clear intended-agent pickup', async () => {
  const target = thread(1, [post(10, [request('codex')])]);
  const page = await setup({ threads: [target], storage: { 'agent-comms-seen': JSON.stringify({ 1: 9999 }) } });
  try { assertKind(page.document, 'unread'); }
  finally { page.dom.window.close(); }
});

test('only explicit request.started changes pickup blue to gray in progress', async () => {
  const lifecycle = request('codex');
  const target = thread(1, [post(10, [lifecycle])]);
  const runs = [{ thread_id: 1, agent: 'codex', run_id: 'running-without-ack' }];
  const page = await setup({ threads: [target], runs });
  try {
    assertKind(page.document, 'unread');
    lifecycle.state = 'started'; lifecycle.assigned_session = 4; lifecycle.version += 1;
    await page.win.refresh();
    assert.match(assertKind(page.document, 'active'), /started|progress|working/i);
  } finally { page.dom.window.close(); }
});

for (const runningAgent of ['codex', 'claude']) {
  test(`stale queued pickup stays stuck while ${runningAgent} has a dispatcher run`, async () => {
    const target = thread(1, [post(10, [request('codex', 'queued', 120)], { created_at: ago(120) }),
      post(11, [], { type: 'status', agent: 'codex', to: [], needs_response: false, body: 'Unrelated progress update' })]);
    const page = await setup({ threads: [target], runs: [{ thread_id: 1, agent: runningAgent, run_id: 'unrelated-run' }] });
    try {
      assert.match(assertKind(page.document, 'stalled'), /codex/i);
      assert.match(dot(page.document).title, /queued|pickup|pick.?up|waiting|stuck/i);
    } finally { page.dom.window.close(); }
  });
}

test('one finished recipient cannot hide another recipient still waiting for pickup', async () => {
  const target = thread(1, [post(10, [request('codex', 'finished'), request('claude')], { agent: 'human' })]);
  const page = await setup({ threads: [target] });
  try {
    assert.match(assertKind(page.document, 'unread'), /claude/i);
    assert.equal(page.document.querySelector('tr[data-thread="1"] .dot.done'), null);
  } finally { page.dom.window.close(); }
});

test('mixed queued and started recipients retain outstanding pickup', async () => {
  const target = thread(1, [post(10, [request('codex', 'started'), request('claude')], { agent: 'human' })]);
  const page = await setup({ threads: [target] });
  try { assert.match(assertKind(page.document, 'unread'), /claude/i); }
  finally { page.dom.window.close(); }
});

test('all recipients explicitly finished settle the thread despite unseen evidence or FYI', async () => {
  const target = thread(1, [post(10, [request('codex', 'finished'), request('claude', 'finished')], { agent: 'human' }),
    post(11, [], { type: 'status', agent: 'codex', to: ['claude'], needs_response: false, body: 'Verified the fix' })]);
  const other = thread(2, [post(20, [request('claude')], { thread_id: 2, agent: 'human' })]);
  const page = await setup({ threads: [target, other], storage: { 'agent-comms-seen': '{}', 'agent-comms-thread': '2' } });
  try {
    assert.equal(page.document.querySelector('tr[data-thread="1"]'), null, 'completed work is hidden by default');
    page.document.getElementById('show-completed').click();
    await settle();
    assert.match(assertKind(page.document, 'done'), /done|closed|settled|finished/i);
  }
  finally { page.dom.window.close(); }
});

test('an addressed FYI without response intent never creates blue pickup', async () => {
  const target = thread(1, [post(10, [], { type: 'status', agent: 'codex', to: ['claude'], needs_response: false })]);
  const other = thread(2, [post(20, [request('claude')], { thread_id: 2, agent: 'human' })]);
  const page = await setup({ threads: [target, other], storage: { 'agent-comms-seen': '{}', 'agent-comms-thread': '2' } });
  try {
    page.document.getElementById('show-completed').click();
    await settle();
    assert.equal(page.document.querySelector('tr[data-thread="1"] .dot.unread'), null);
    assert.equal(page.document.querySelector('tr[data-thread="1"] .dot'), null, 'FYI is neither pickup, processing nor completion');
    assert.ok(page.document.querySelector('tr[data-thread="1"]'), 'unclassified discussion remains visible');
  } finally { page.dom.window.close(); }
});

for (const scenario of [
  { bucket: 'waiting', state: 'queued', overdue: false, kind: 'unread' },
  { bucket: 'waiting', state: 'queued', overdue: true, kind: 'stalled' },
  { bucket: 'processing', state: 'started', overdue: false, kind: 'active' },
  { bucket: 'blocked', state: 'blocked', overdue: false, kind: 'stalled' },
]) {
  test(`server pickup projection retains ${scenario.bucket}${scenario.overdue ? ' overdue' : ''} outside visible post window`, async () => {
    const target = thread(1, [post(999, [], { type: 'status', to: [], needs_response: false, body: 'Recent FYI' })]);
    target.pickup = { waiting: [], processing: [], blocked: [], complete: false };
    target.pickup[scenario.bucket] = [{ post_id: 10, recipient: 'codex', assigned_agent: 'codex', assigned_session: 4,
      state: scenario.state, reason: scenario.state === 'blocked' ? 'Missing access' : '',
      deadline_at: ago(scenario.overdue ? 120 : -10), overdue: scenario.overdue }];
    const page = await setup({ threads: [target], runs: [{ thread_id: 1, agent: 'codex', run_id: 'not-acknowledgement' }] });
    try { assert.match(assertKind(page.document, scenario.kind), /codex/i); }
    finally { page.dom.window.close(); }
  });
}

for (const content of ['empty', 'FYI', 'finished-task']) {
  test(`an authoritative incomplete projection never paints ${content} as finished`, async () => {
    const target = thread(1, content === 'FYI' ? [post(10, [], { type: 'status', needs_response: false })] : []);
    target.pickup = { waiting: [], processing: [], blocked: [], complete: false };
    if (content === 'finished-task') target.tasks = [{ id: 7, title: 'One earlier task', status: 'done',
      lease_state: 'none', intends_files: [], events: [] }];
    const page = await setup({ threads: [target], runs: [{ thread_id: 1, agent: 'codex', run_id: 'unbound' }] });
    try {
      assert.ok(page.document.querySelector('tr[data-thread="1"]'), 'uncertain work is not hidden as complete');
      assert.equal(page.document.querySelector('tr[data-thread="1"] .dot'), null, 'no processing or completion is inferred');
    } finally { page.dom.window.close(); }
  });
}

for (const complete of [false, true]) {
  test(`an unresolved shared discussion is not processing or finished even when pickup.complete=${complete}`, async () => {
    const target = thread(1, []);
    target.pickup = { waiting: [], processing: [], blocked: [], complete };
    const issue = { id: 7, title: 'Open design discussion', body: 'Still under discussion', status: 'open', needs_human: false,
      created_by: 'codex', created_at: ago(5), updated_at: ago(1), comments: [], decisions: [], resolution: null,
      links: [{ thread_id: 1, post_id: null, project: '/repo/app', title: target.title, needs_human: false }] };
    const page = await setup({ threads: [target], issues: [issue] });
    try {
      assert.ok(page.document.querySelector('tr[data-thread="1"]'), 'the unresolved thread remains visible');
      assert.equal(page.document.querySelector('tr[data-thread="1"] .dot'), null, 'shared discussion supplies neither started nor completion evidence');
      assert.ok(page.document.querySelector('#thread-issues a[href="#issue-7"]'), 'discussion evidence remains accessible');
    } finally { page.dom.window.close(); }
  });
}
