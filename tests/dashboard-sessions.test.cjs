// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-sessions.test.cjs
// The Sessions tab (hosted_lineage.py, GET /api/hosted-trees): one row per session tree, the tree nested in the center,
// the selected session's details under it. Read-only; what an agent wrote is shown as plain text only.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });

const NOW = 1_800_000_000;
const ROOT = '11111111-1111-4111-8111-111111111111', KID = '22222222-2222-4222-8222-222222222222',
  GRAND = '33333333-3333-4333-8333-333333333333', GONE = '44444444-4444-4444-8444-444444444444',
  OTHER = '55555555-5555-4555-8555-555555555555', IROOT = '66666666-6666-4666-8666-666666666666',
  IKID = '77777777-7777-4777-8777-777777777777';
const HOSTILE = '<script>window.pwned = 1</script><img src=x onerror="window.pwned = 2"> **bold** ' +
  '[click](javascript:window.pwned=3) <a href="https://evil.example/">evil</a>\nline 2\nline 3\nline 4\nline 5\nline 6';

function session(id, extra = {}) {
  return { session_id: id, kind: 'hosted', agent: 'claude', runtime: 'claude-code', project: '/repo/app', node: 'laptop',
    model: null, state: 'idle', pid: null, turns: 1, queued: 0, in_flight: null, last_result: { message: 1, ok: true, detail: '', denials: [] },
    denials: [], stop: null, error: null, created_at: NOW - 600, updated_at: NOW - 60, ended_at: null, provider_id: null,
    node_live: true, transcript: `data/node-logs/sessions/${id}.jsonl on node laptop`, parent: null, root: ROOT, depth: 0,
    access: 'workspace', started_by: { kind: 'human', name: 'human' }, result: null, output: null, closing: false,
    children: [], version: 'v1', ...extra };
}
function fixture() {
  const grand = session(GRAND, { parent: KID, depth: 2, name: 'review-1', role: 'review', title: 'check the diff',
    access: 'read-only', isolation: 'shared', state: 'running', in_flight: 2, turns: 1, model: 'haiku', effort: 'low',
    started_by: { kind: 'agent', name: 'claude', via: 'hosted', session: KID }, account_home: '/Users/you/.claude-work' });
  const kid = session(KID, { parent: ROOT, depth: 1, name: 'general-1', title: 'look around', isolation: 'worktree',
    worktree: '/wt/session-2222', model: 'opus', effort: 'high', turns: 3, queued: 1,
    started_by: { kind: 'agent', name: 'claude', via: 'hosted', session: ROOT },
    result: { n: 4, message: 3, started_by: 'parent', outcome: 'completed', ok: true, detail: '', state: 'pending',
      carrier: null, attempts: [], why: null, waiting_because: "a dispatched run is using the parent's checkout: it goes once the run is over" },
    output: { text: HOSTILE, truncated: false, length: HOSTILE.length, for_message: 3 }, children: [grand] });
  const gone = session(GONE, { parent: ROOT, depth: 1, name: 'test-1', state: 'ended', error: 'closed by the human',
    ended_at: NOW - 120, isolation: 'shared' });
  const root = session(ROOT, { turns: 2, children: [kid, gone], output: { text: 'root says hi', truncated: false, length: 12, for_message: 2 } });
  const other = session(OTHER, { root: OTHER, state: 'ended', error: 'closed by the human', ended_at: NOW - 3000 });
  const ikid = session(IKID, { parent: IROOT, root: IROOT, depth: 1, name: 'research-1', parent_kind: 'interactive',
    started_by: { kind: 'agent', name: 'codex', via: 'interactive', session: IROOT } });
  const iroot = { session_id: IROOT, kind: 'interactive', agent: 'codex', project: '/repo/app', state: 'open', error: null,
    created_at: NOW - 900, ended_at: null, parent: null, root: IROOT, depth: 0, children: [ikid],
    started_by: { kind: 'interactive', name: 'codex' }, version: 'v1' };
  const live = [
    { root: ROOT, kind: 'hosted', agent: 'claude', project: '/repo/app', node: 'laptop', audit_thread: 9, sessions: 4, live: 3, created_at: NOW - 600, session: root },
    { root: IROOT, kind: 'interactive', agent: 'codex', project: '/repo/app', node: null, audit_thread: null, sessions: 1, live: 1, created_at: NOW - 900, session: iroot },
  ];
  const ended = [{ root: OTHER, kind: 'hosted', agent: 'claude', project: '/repo/other', node: 'laptop', audit_thread: null, sessions: 1, live: 0, created_at: NOW - 4000, session: other }];
  return { live, ended };
}

const out = text => ({ text, truncated: false, length: text.length });
// The dashboard's details route (GET /api/hosted-trees/{id}): the session's view, capped texts only.
function detailOf(id) {
  if (id === IROOT) return { session_id: IROOT, kind: 'interactive', agent: 'codex', project: '/repo/app', state: 'open',
    created_at: NOW - 900, messages: [], results: [], version: 'v1' };
  return { ...session(id), ...(id === GRAND ? { account_home: '/Users/you/.claude-work' } : {}), messages: [{ n: 1, kind: 'user', state: 'done', detail: '' }, { n: 2, kind: 'child', state: 'done', detail: '' }],
    results: id === ROOT ? [{ n: 7, child: GONE, name: 'test-1', message: 1, outcome: 'failed', state: 'undelivered',
      why: 'its parent session ended', output: out('<b>kept</b> [x](https://evil.example)') }] : [],
    output: { ...out(`full reply of ${id.slice(0, 8)} <i>not html</i>`), for_message: 2 }, version: 'v1' };
}
// `intercept(url, answer)`: a test may hold an answer back (a deferred response) or replace it.
async function setup({ url = 'http://127.0.0.1:8787/#sessions', human = true, intercept = null } = {}) {
  const calls = [];
  const world = fixture();
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', pretendToBeVisual: true, beforeParse(win) {
    const answer = u => {
      if (u === '/api/whoami') return ok({ name: human ? 'human' : 'claude', is_human: human });
      if (u.startsWith('/api/state')) return ok({ me: { name: human ? 'human' : 'claude', is_human: human }, paused: false,
        threads: [{ id: 9, title: 'Session tree of claude', project: '/repo/app', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
          pinned_summary: null, tasks: [], task_counts: {}, posts: [], created_at: '2027-01-15T07:00:00+00:00' }],
        sessions: [], needs_you: [], limits: {}, authorization_grants: [], agents: [], task_categories: [], ...(human ? { hosted_live: 4 } : {}) });
      if (!human && u.startsWith('/api/hosted-')) return { ok: false, status: 403, statusText: '403', json: async () => ({ message: 'human only' }) };
      const m = /^\/api\/hosted-trees\/([0-9a-f-]+)$/.exec(u);
      if (m) return [ROOT, KID, GRAND, GONE, OTHER, IROOT, IKID].includes(m[1]) ? ok(detailOf(m[1]))
        : { ok: false, status: 404, statusText: '404', json: async () => ({ message: 'no such hosted session' }) };
      if (u.startsWith('/api/hosted-trees')) return ok({ trees: u.includes('all=true') ? [...world.live, ...world.ended] : world.live,
        live_sessions: 4, now: NOW, output_max: 4000 });
      return { ok: false, status: 404, statusText: '404', json: async () => ({ message: 'not found' }) };
    };
    win.fetch = async u => { calls.push(u); return intercept ? intercept(u, () => answer(u)) : answer(u); };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, calls, world };
}
const deferred = () => { let resolve; const p = new Promise(r => { resolve = r; }); return { p, resolve }; };
const treeRows = d => [...d.querySelectorAll('.session-list li.row')].map(r => r.dataset.tree);
const ids = (d, sel) => [...d.querySelectorAll(sel)].map(n => n.dataset.session);

test('the Sessions tab counts live sessions and lists one row per tree, never a child on its own', async () => {
  const { dom, document } = await setup();
  const tab = document.getElementById('tab-sessions');
  assert.ok(tab, 'a third tab');
  assert.equal(tab.getAttribute('aria-selected'), 'true');
  assert.match(tab.textContent, /Sessions\s*4/);
  assert.deepEqual(treeRows(document), [ROOT, IROOT], 'ended trees are hidden by default');
  dom.window.close();
});

test('the selected tree is nested under its root at every depth, with the root as the anchor', async () => {
  const { dom, document } = await setup();
  const root = document.querySelector('.lin-tree > li');
  assert.equal(root.dataset.session, ROOT);
  assert.ok(root.classList.contains('lin-root'));
  assert.match(root.querySelector('.lin-row').textContent, /root/);
  assert.deepEqual(ids(root, ':scope > ul.lin-group > li'), [KID, GONE]);
  const kid = root.querySelector(`li[data-session="${KID}"]`);
  assert.deepEqual(ids(kid, ':scope > ul.lin-group > li'), [GRAND]);
  assert.equal(kid.querySelector(`li[data-session="${GRAND}"]`).dataset.depth, '2');
  const facts = kid.querySelector(':scope > .lin-facts').textContent;
  for (const want of ['claude', 'model opus, effort high', 'workspace', 'own worktree', 'node laptop', '3 turns', '1 queued',
    'by agent claude (from session 11111111)']) assert.ok(facts.includes(want), want);
  assert.match(kid.querySelector(':scope > .lin-facts').textContent, /open 10m 0s/, 'how long it has been open, by the server clock');
  assert.match(document.querySelector(`li[data-session="${GONE}"] .lin-facts`).textContent, /ran 8m 0s/);
  assert.match(document.querySelector(`li[data-session="${GRAND}"] .lin-state`).textContent, /running/);
  assert.ok(document.querySelector(`li[data-session="${GONE}"]`).classList.contains('ended'), 'an ended member is greyed');
  assert.match(document.querySelector(`li[data-session="${GONE}"]`).textContent, /closed by the human/);
  const audit = document.getElementById(`lin-audit-${ROOT}`);
  assert.equal(audit.getAttribute('href'), '#thread-9');
  dom.window.close();
});

test("a child's output is plain text: no element, no link, no script from it", async () => {
  const { dom, document, win } = await setup();
  const kid = document.querySelector(`li[data-session="${KID}"]`);
  const pre = kid.querySelector(':scope > .lin-output-box pre.lin-output');
  assert.equal(pre.textContent, HOSTILE);
  assert.equal(pre.children.length, 0, 'not one element parsed out of it');
  assert.equal(win.pwned, undefined);
  assert.equal(document.querySelectorAll('img').length, 0);
  assert.ok(![...document.querySelectorAll('a')].some(a => /evil|javascript/.test(a.getAttribute('href') || '')), 'never a link');
  assert.match(kid.querySelector(':scope > .lin-output-box').textContent, /untrusted · plain text/);
  assert.match(kid.querySelector(':scope > .lin-result').textContent, /Latest result.*message 3: completed.*waiting for its parent/s);
  assert.match(kid.querySelector(':scope > .lin-result .lin-waits').textContent, /dispatched run is using the parent's checkout/);
  dom.window.close();
});

test('fold, expanded output and focus survive the refresh', async () => {
  const { dom, document, win } = await setup();
  const outBtn = document.getElementById(`out-tree-${KID}-toggle`);
  assert.equal(outBtn.getAttribute('aria-expanded'), 'false');
  assert.ok(document.getElementById(`out-tree-${KID}`).classList.contains('clamped'));
  outBtn.click();
  document.getElementById(`lin-fold-${KID}`).click();
  document.getElementById(`lin-open-${GONE}`).focus();
  await win.refresh();
  await settle();
  assert.equal(document.getElementById(`out-tree-${KID}-toggle`).getAttribute('aria-expanded'), 'true');
  assert.ok(!document.getElementById(`out-tree-${KID}`).classList.contains('clamped'));
  assert.ok(document.getElementById(`lin-group-${KID}`).hidden, 'still folded');
  assert.equal(document.getElementById(`lin-fold-${KID}`).getAttribute('aria-expanded'), 'false');
  assert.equal(document.activeElement.id, `lin-open-${GONE}`, 'focus did not move');
  dom.window.close();
});

test('keyboard: Up/Down move between sessions, Left folds and goes to the parent, Right unfolds', async () => {
  const { dom, document, win } = await setup();
  const key = (k) => document.activeElement.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true }));
  document.getElementById(`lin-open-${ROOT}`).focus();
  key('ArrowDown'); assert.equal(document.activeElement.id, `lin-open-${KID}`);
  key('ArrowDown'); assert.equal(document.activeElement.id, `lin-open-${GRAND}`);
  key('ArrowLeft'); assert.equal(document.activeElement.id, `lin-open-${KID}`, 'a leaf goes to its parent');
  key('ArrowLeft'); assert.ok(document.getElementById(`lin-group-${KID}`).hidden, 'then folds');
  assert.equal(document.activeElement.id, `lin-open-${KID}`);
  key('ArrowDown'); assert.equal(document.activeElement.id, `lin-open-${GONE}`, 'folded children are skipped');
  key('ArrowUp'); key('ArrowRight');
  assert.ok(!document.getElementById(`lin-group-${KID}`).hidden);
  key('ArrowRight'); assert.equal(document.activeElement.id, `lin-open-${GRAND}`);
  key('End'); assert.equal(document.activeElement.id, `lin-open-${GONE}`);
  key('Home'); assert.equal(document.activeElement.id, `lin-open-${ROOT}`);
  const fold = document.getElementById(`lin-fold-${ROOT}`);
  assert.match(fold.getAttribute('aria-label'), /Hide the 2 sessions started by/, 'icon-only control is labelled');
  dom.window.close();
});

test("a row opens the session's details from show, its reply as plain text", async () => {
  const { dom, document, calls } = await setup();
  assert.ok(calls.includes(`/api/hosted-trees/${ROOT}`), 'the root by default');
  const detail = document.getElementById('session-detail');
  assert.match(detail.textContent, /Session details: claude root session/);
  const kept = [...detail.querySelectorAll('pre.lin-output')].map(p => p.textContent);
  assert.ok(kept.includes('<b>kept</b> [x](https://evil.example)'), 'an undelivered result keeps its text, as text');
  assert.ok(kept.includes('full reply of 11111111 <i>not html</i>'));
  assert.equal(detail.querySelectorAll('b, i').length, 0);
  document.getElementById(`lin-open-${GRAND}`).click();
  await settle();
  assert.ok(calls.includes(`/api/hosted-trees/${GRAND}`));
  assert.equal(dom.window.location.hash, `#session-${GRAND}`);
  assert.match(document.getElementById('session-detail').textContent, /Session details: review-1: check the diff/);
  assert.equal(document.getElementById(`lin-open-${GRAND}`).getAttribute('aria-current'), 'true');
  // An interactive root is no hosted session: its details come from the same read-only route.
  document.querySelector(`.session-list li[data-tree="${IROOT}"]`).click();
  await settle();
  assert.ok(calls.includes(`/api/hosted-trees/${IROOT}`));
  assert.ok(!calls.some(u => u.startsWith('/api/hosted-sessions')), 'never the CLI show route');
  assert.match(document.getElementById('session-detail').textContent, /interactive root/);
  dom.window.close();
});

test('Show ended asks for every tree; the view has no action buttons', async () => {
  const { dom, document, calls } = await setup();
  const box = document.getElementById('show-ended-sessions');
  box.checked = true; box.dispatchEvent(new dom.window.Event('change'));
  await settle();
  assert.ok(calls.includes('/api/hosted-trees?all=true'));
  assert.deepEqual(treeRows(document), [ROOT, IROOT, OTHER]);
  assert.ok(document.querySelector(`.session-list li[data-tree="${OTHER}"]`).classList.contains('ended'));
  const labels = [...document.querySelectorAll('#thread-pane button')].map(b => b.textContent + ' ' + (b.getAttribute('aria-label') || ''));
  assert.ok(!labels.some(l => /\b(start|send|cancel|close|stop)\b/i.test(l)), labels.join(' | '));
  dom.window.close();
});

test('an agent token gets no Sessions tab and never asks for trees', async () => {
  const { dom, document, calls } = await setup({ human: false });
  assert.equal(document.getElementById('tab-sessions'), null);
  assert.ok(!calls.some(u => u.startsWith('/api/hosted-trees')));
  assert.ok(document.querySelector('[data-tab="threads"]'), 'falls back to Threads');
  dom.window.close();
});

test('details are fetched again only when the selection or the session version changes', async () => {
  const { dom, win, calls, world } = await setup();
  const detailCalls = () => calls.filter(u => u === `/api/hosted-trees/${ROOT}`).length;
  assert.equal(detailCalls(), 1);
  await win.refresh(); await settle(); await win.refresh(); await settle();
  assert.equal(detailCalls(), 1, 'not on every refresh');
  assert.ok(calls.filter(u => u === '/api/hosted-trees').length >= 3, 'the trees are');
  world.live[0].session.version = 'v2';
  await win.refresh(); await settle();
  assert.equal(detailCalls(), 2, 'a new version: fetched again');
  assert.ok(!calls.some(u => u.startsWith('/api/hosted-sessions')), 'never the CLI show route');
  dom.window.close();
});

// Answers a test holds back until it releases them: `held` gets one release function per held request.
function holder() {
  const held = [];
  let when = null, as = null;
  return { held, hold: (match, replace) => { when = match; as = replace || null; }, stop: () => { when = null; },
    intercept: (u, answer) => {
      if (!when || !when(u)) return answer();
      const d = deferred(), give = as;
      held.push(() => d.resolve(give ? give() : answer()));
      return d.p;
    } };
}

test('a late live-only answer never overwrites the Show ended trees', async () => {
  const h = holder();
  const { dom, document, win } = await setup({ intercept: h.intercept });
  h.hold(u => u === '/api/hosted-trees');
  win.refresh();                       // a live-only refresh, its answer held back
  await settle();
  h.stop();
  const box = document.getElementById('show-ended-sessions');
  box.checked = true; box.dispatchEvent(new win.Event('change'));
  await settle();
  assert.deepEqual(treeRows(document), [ROOT, IROOT, OTHER]);
  assert.equal(h.held.length, 1);
  h.held[0](); await settle(); await settle();       // the held answer lands, and its refresh renders
  assert.deepEqual(treeRows(document), [ROOT, IROOT, OTHER], 'the late live-only answer was dropped');
  assert.ok(document.getElementById('show-ended-sessions').checked);
  dom.window.close();
});

test("a late detail failure never erases the newly selected session's details", async () => {
  const h = holder();
  const { dom, document } = await setup({ intercept: h.intercept });
  h.hold(u => u === `/api/hosted-trees/${GRAND}`,
    () => ({ ok: false, status: 500, statusText: '500', json: async () => ({ message: 'boom' }) }));
  document.getElementById(`lin-open-${GRAND}`).click();     // its details are held back
  await settle();
  h.stop();
  document.getElementById(`lin-open-${KID}`).click();       // answered at once
  await settle();
  const shown = () => document.getElementById('session-detail').textContent;
  assert.match(shown(), /full reply of 22222222/);
  h.held[0](); await settle();
  assert.match(shown(), /Session details: general-1: look around/);
  assert.match(shown(), /full reply of 22222222/, 'still the new selection');
  assert.doesNotMatch(shown(), /Couldn't load its details/);
  dom.window.close();
});

test('a link to an ended session loads the ended trees; one to a session that is gone says so', async () => {
  const a = await setup({ url: `http://127.0.0.1:8787/#session-${OTHER}` });
  assert.ok(a.calls.includes('/api/hosted-trees?all=true'), 'looked among the ended trees');
  assert.equal(a.document.querySelector('.lin-tree > li').dataset.session, OTHER, 'its own tree, not another');
  assert.ok(a.document.getElementById('show-ended-sessions').checked, 'and the toggle says so');
  assert.equal(a.win.location.hash, `#session-${OTHER}`);
  a.dom.window.close();
  const GHOST = '99999999-9999-4999-8999-999999999999';
  const b = await setup({ url: `http://127.0.0.1:8787/#session-${GHOST}` });
  assert.equal(b.document.querySelector('.lin-tree'), null, 'no other tree under a URL that names this one');
  assert.match(b.document.getElementById('lineage-empty').textContent, new RegExp(`Session ${GHOST} is not available`));
  assert.equal(b.win.location.hash, `#session-${GHOST}`);
  const box = b.document.getElementById('show-ended-sessions');
  box.checked = false; box.dispatchEvent(new b.win.Event('change'));
  await settle();
  const looked = b.calls.filter(u => u === '/api/hosted-trees?all=true').length;
  await b.win.refresh(); await settle();
  assert.equal(b.calls.filter(u => u === '/api/hosted-trees?all=true').length, looked, 'unticking is respected: looked once');
  assert.match(b.document.getElementById('lineage-empty').textContent, /not in a live tree/);
  b.dom.window.close();
});

test('the Show ended checkbox keeps keyboard focus through the refresh', async () => {
  const { dom, document, win } = await setup();
  document.getElementById('show-ended-sessions').focus();
  await win.refresh(); await settle();
  assert.equal(document.activeElement.id, 'show-ended-sessions');
  dom.window.close();
});

test('a hub slower than the refresh is caught up with: an identical poll waits for the request in flight', async () => {
  const h = holder();
  const { dom, document, win, calls, world } = await setup({ intercept: h.intercept });
  const treeCalls = () => calls.filter(u => u === '/api/hosted-trees').length;
  const before = treeCalls();
  h.hold(u => u === '/api/hosted-trees');
  const polls = [win.refresh(), win.refresh(), win.refresh()];      // three refreshes while the answer is slow
  await settle();
  assert.equal(treeCalls(), before + 1, 'one request, not one per refresh');
  h.stop();
  world.live = world.live.slice(1);                                  // what the slow answer carries when it lands
  h.held.forEach(release => release());
  await Promise.all(polls); await settle();
  assert.deepEqual(treeRows(document), [IROOT], 'the slow answer was applied, not chased');
  // The same for the details: a new version, its answer slow across several refreshes.
  world.live[0].session.version = 'v9';
  h.hold(u => u === `/api/hosted-trees/${IROOT}`);
  const detailCalls = () => calls.filter(u => u === `/api/hosted-trees/${IROOT}`).length;
  const d0 = detailCalls();
  const more = [win.refresh(), win.refresh(), win.refresh()];
  await settle();
  assert.equal(detailCalls(), d0 + 1);
  h.stop(); h.held.forEach(release => release());
  await Promise.all(more); await settle();
  assert.match(document.getElementById('session-detail').textContent, /interactive root/);
  dom.window.close();
});

test('coming back to the Sessions tab keeps #sessions until a session is chosen', async () => {
  const { dom, document, win } = await setup();
  document.getElementById('tab-threads').click(); await settle();
  document.getElementById('tab-sessions').click(); await settle();
  assert.equal(win.location.hash, '#sessions', 'not the first tree, which a refresh may reorder');
  document.getElementById(`lin-open-${KID}`).click(); await settle();
  assert.equal(win.location.hash, `#session-${KID}`);
  dom.window.close();
});

test('with the linked session gone, the first tree row is still reachable by keyboard and nothing is selected', async () => {
  const GHOST = '99999999-9999-4999-8999-999999999999';
  const { dom, document } = await setup({ url: `http://127.0.0.1:8787/#session-${GHOST}` });
  const links = [...document.querySelectorAll('.session-list a.row-link')];
  assert.equal(links[0].getAttribute('tabindex'), '0');
  assert.ok(links.slice(1).every(a => a.getAttribute('tabindex') === '-1'));
  assert.ok(links.every(a => !a.hasAttribute('aria-current')), 'the missing target stays unselected');
  dom.window.close();
});

test('a cut note counts code points as the server does, and names a command only where one can show the text', async () => {
  const { dom, document, world } = await setup({ intercept: (u, answer) => {
    if (u !== `/api/hosted-trees/${IROOT}`) return answer();
    return ok({ session_id: IROOT, kind: 'interactive', agent: 'codex', project: '/repo/app', state: 'open', created_at: NOW - 900,
      messages: [], version: 'v1', results: [{ n: 1, child: IKID, name: 'research-1', message: 1, outcome: 'completed',
        state: 'undelivered', why: 'its root was closed', output: { text: '😀😀😀', truncated: true, length: 4100 } }] });
  } });
  world.live[0].session.children[0].output = { text: '🎉'.repeat(3), truncated: true, length: 4100, for_message: 3 };
  await dom.window.refresh(); await settle();
  const kidBox = document.querySelector(`li[data-session="${KID}"] > .lin-output-box`).textContent;
  assert.match(kidBox, /Cut at 3 of 4100 characters here; board session show 22222222/);
  document.querySelector(`.session-list li[data-tree="${IROOT}"]`).click(); await settle();
  const detail = document.getElementById('session-detail').textContent;
  assert.match(detail, /Cut at 3 of 4100 characters here\./);
  assert.doesNotMatch(detail, /board session show 66666666/, 'show cannot fetch an interactive root');
  assert.match(detail, /Root made/);
  assert.match(document.querySelector(`li[data-session="${IROOT}"] .lin-facts`).textContent, /root made .*at its first child/);
  dom.window.close();
});

test('a slow details answer never holds up the trees: they keep being fetched and rendered', async () => {
  const h = holder();
  const { dom, document, win, calls, world } = await setup({ intercept: h.intercept });
  const treeCalls = () => calls.filter(u => u === '/api/hosted-trees').length;
  const detailCalls = () => calls.filter(u => u === `/api/hosted-trees/${ROOT}`).length;
  const t0 = treeCalls(), d0 = detailCalls();
  world.live[0].session.version = 'v2';
  h.hold(u => u === `/api/hosted-trees/${ROOT}`);              // only the details answer is slow
  const tick = () => Promise.race([win.refresh(), settle(300)]);
  await tick(); await settle();
  world.live = world.live.slice(0, 1);                          // the trees change meanwhile
  await tick(); await settle();
  await tick(); await settle();
  assert.equal(treeCalls(), t0 + 3, 'a trees request on every refresh');
  assert.deepEqual(treeRows(document), [ROOT], 'and each one rendered');
  assert.equal(detailCalls(), d0 + 1, 'one details request, waited for');
  h.stop(); h.held.forEach(release => release()); await settle();
  assert.match(document.getElementById('session-detail').textContent, /full reply of 11111111/);
  dom.window.close();
});

test('details are cached under the version their body carries, so a passing change does not stick', async () => {
  let first = true;
  const { dom, document, win, calls } = await setup({ intercept: (u, answer) => {
    if (u !== `/api/hosted-trees/${ROOT}` || !first) return answer();
    first = false;      // read between two changes: a waiting reason that came and went, with its own version
    return ok({ ...detailOf(ROOT), output: { ...out('a transient reason'), for_message: 2 }, version: 'vT' });
  } });
  assert.match(document.getElementById('session-detail').textContent, /a transient reason/);
  await win.refresh(); await settle();            // the tree still says v1: not what the panel holds
  assert.equal(calls.filter(u => u === `/api/hosted-trees/${ROOT}`).length, 2);
  assert.doesNotMatch(document.getElementById('session-detail').textContent, /a transient reason/);
  await win.refresh(); await settle();
  assert.equal(calls.filter(u => u === `/api/hosted-trees/${ROOT}`).length, 2, 'then cached under v1');
  dom.window.close();
});

test("a session whose node gives its runner an account folder shows the folder's path, and others show none", async () => {
  const { dom, document } = await setup();
  const facts = document.querySelector(`li[data-session="${GRAND}"] > .lin-facts`);
  const item = [...facts.querySelectorAll('.lin-fact')].find(x => x.textContent.startsWith('account folder'));
  assert.equal(item.textContent, 'account folder ~/.claude-work');
  assert.equal(item.getAttribute('title'), '/Users/you/.claude-work', 'the whole path in the title');
  assert.ok(!document.querySelector(`li[data-session="${KID}"] > .lin-facts`).textContent.includes('account folder'));
  document.getElementById(`lin-open-${GRAND}`).click();
  await settle();
  assert.match(document.getElementById('session-detail').textContent, /Account folder\/Users\/you\/\.claude-work/);
  document.getElementById(`lin-open-${KID}`).click();
  await settle();
  assert.doesNotMatch(document.getElementById('session-detail').textContent, /Account folder/);
  dom.window.close();
});

// ---------------------------------------------------------------- review follow-ups (2026-11-23)

test("the trees carry a reply's preview: Show all loads the session's details, the whole reply unfolded there", async () => {
  const full = 'p'.repeat(299) + '<b>q</b>' + 'r'.repeat(700);
  const { dom, document, calls, world } = await setup({ intercept: (u, answer) => {
    if (u !== `/api/hosted-trees/${KID}`) return answer();
    return ok({ ...detailOf(KID), output: { text: full, truncated: false, length: full.length, for_message: 3 } });
  } });
  world.live[0].session.children[0].output = { text: full.slice(0, 300), truncated: true, length: full.length, for_message: 3 };
  await dom.window.refresh(); await settle();
  const box = document.querySelector(`li[data-session="${KID}"] > .lin-output-box`);
  assert.equal(box.querySelector('pre.lin-output').textContent, full.slice(0, 300), 'the preview, as text');
  assert.ok(!calls.includes(`/api/hosted-trees/${KID}`));
  const more = document.getElementById(`out-tree-${KID}-toggle`);
  assert.equal(more.textContent, 'Show all');
  more.click(); await settle();
  assert.ok(calls.includes(`/api/hosted-trees/${KID}`), 'the details are loaded');
  assert.equal(dom.window.location.hash, `#session-${KID}`);
  const pre = document.getElementById(`out-detail-${KID}`);
  assert.equal(pre.textContent, full, 'the whole capped reply, as text');
  assert.equal(pre.children.length, 0);
  assert.ok(!pre.classList.contains('clamped'), 'unfolded');
  const region = document.getElementById(`out-detail-${KID}-box`);
  assert.equal(document.activeElement, region, 'the focus moved to the expanded reply');
  assert.equal(region.getAttribute('tabindex'), '-1');
  assert.match(region.getAttribute('aria-label'), /Last reply from general-1/);
  dom.window.close();
});

test('when the details fail to load, Show all moves the focus to the error, not to the preview', async () => {
  const { dom, document, world } = await setup({ intercept: (u, answer) => u === `/api/hosted-trees/${KID}`
    ? { ok: false, status: 500, statusText: '500', json: async () => ({ message: 'boom' }) } : answer() });
  world.live[0].session.children[0].output = { text: 'p'.repeat(300), truncated: true, length: 1000, for_message: 3 };
  await dom.window.refresh(); await settle();
  document.getElementById(`out-tree-${KID}-toggle`).click(); await settle();
  const err = document.getElementById('session-detail-error');
  assert.ok(err, 'the error is shown');
  assert.equal(document.activeElement, err, 'and it has the focus');
  assert.equal(err.getAttribute('tabindex'), '-1');
  assert.match(err.getAttribute('aria-label'), /Couldn't load the details of general-1: look around: boom/);
  dom.window.close();
});

test('Show all moves the focus only if its session is still the one selected when the details land', async () => {
  const full = 'p'.repeat(1000);
  const h = holder();
  const { dom, document, world } = await setup({ intercept: (u, answer) => {
    if (u === `/api/hosted-trees/${KID}`) return h.intercept(u, () => ok({ ...detailOf(KID), output: { text: full, truncated: false, length: 1000 } }));
    return answer();
  } });
  world.live[0].session.children[0].output = { text: full.slice(0, 300), truncated: true, length: 1000, for_message: 3 };
  await dom.window.refresh(); await settle();
  h.hold(u => u === `/api/hosted-trees/${KID}`);
  document.getElementById(`out-tree-${KID}-toggle`).click(); await settle();
  h.stop();
  document.getElementById(`lin-open-${GRAND}`).click(); await settle();     // another session, before KID's details
  h.held.forEach(release => release()); await settle();
  assert.notEqual(document.activeElement.id, `out-detail-${KID}-box`);
  assert.match(document.getElementById('session-detail').textContent, /review-1: check the diff/, 'still the new selection');
  dom.window.close();
});

test('a session on a loop of parent links is a root of its own with the note', async () => {
  const { dom, document, world } = await setup();
  const LOOPED = 'parent links form a loop';
  world.live[0].session = { ...world.live[0].session, detached: true, loop: LOOPED, parent: KID };
  await dom.window.refresh(); await settle();
  const chip = [...document.querySelectorAll(`li[data-session="${ROOT}"] > .lin-row .chip`)].find(c => c.textContent === 'parent loop');
  assert.ok(chip, 'the loop is named');
  assert.equal(chip.getAttribute('title'), LOOPED);
  assert.ok(![...document.querySelectorAll('.chip')].some(c => c.textContent === 'parent gone'));
  dom.window.close();
});

test('a refresh with the details cached draws the page once', async () => {
  const { dom, win } = await setup();
  let draws = 0;
  const real = win.render;
  win.render = (...a) => { draws++; return real(...a); };
  await win.refresh(); await settle();
  assert.equal(draws, 1, 'no second render for an unchanged, cached detail');
  dom.window.close();
});

test('with the linked session gone, arrow keys move from the focused first tree row', async () => {
  const GHOST = '99999999-9999-4999-8999-999999999999';
  const { dom, document, win } = await setup({ url: `http://127.0.0.1:8787/#session-${GHOST}` });
  const first = document.querySelector('.session-list a.row-link');
  first.focus();
  first.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  await settle();
  assert.equal(win.location.hash, `#session-${IROOT}`, 'the next tree is selected');
  assert.equal(document.activeElement.id, `tree-row-${IROOT}`);
  dom.window.close();
});

test('a kept or interrupted result says so in words', async () => {
  const { dom, document, world } = await setup();
  world.live[0].session.children[0].result = { n: 5, message: 1, started_by: 'parent', outcome: 'interrupted', ok: false,
    detail: 'interrupted by a restart: message 2 replaced it', state: 'kept', carrier: null, attempts: [], why: null };
  await dom.window.refresh(); await settle();
  const line = document.querySelector(`li[data-session="${KID}"] > .lin-result`).textContent;
  assert.match(line, /message 1: interrupted by a restart/);
  assert.match(line, /kept, not sent as a turn \(its parent restarted the turn itself\)/);
  dom.window.close();
});
