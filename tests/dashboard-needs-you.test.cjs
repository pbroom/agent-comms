// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-needs-you.test.cjs
// The Needs you card: at the top of the selected thread, one decision component per item that waits on the human:
// what it blocks, the question, option cards (Recommended, Alternative, ..., Write your own reply) and one primary button.
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
  return { id, seq: id, thread_id: threadId, agent: 'codex', session_id: 1, type: 'status', body: 'post ' + id + ' ' + INJECTION,
    to: [], needs_response: false, task_id: null, refs: [], sealed: false, created_at: MINUTES_AGO(120 - id), ...extra };
}
function thread(id, posts, extra = {}) {
  return { id, title: 'Thread ' + id + ' ' + INJECTION, project: '/repo/app', status: 'open', agent_posts_since_human: 0,
    thread_cap: 12, pinned_summary: null, tasks: [], task_counts: {}, posts, created_at: MINUTES_AGO(300), ...extra };
}
const QUESTION = () => ({ question: 'Ship the parser fix now? ' + INJECTION, context: 'Trailing commas are dropped. ' + INJECTION,
  options: [{ id: 'wait', label: 'Wait for the refactor ' + INJECTION, description: 'No churn; costs a week.', outcome: 'declined' },
    { id: 'ship', label: 'Ship it now', description: 'Merges today; costs a re-review.', outcome: 'approved' }],
  recommended_option_id: 'ship' });

const AGENTS = [{ name: 'human', is_human: 1 }, { name: 'claude-code', is_human: 0 }, { name: 'codex', is_human: 0 }];

// window.confirm/prompt/alert throw (and are recorded): the human's embedded browser blocks them.
// `threads`, `needsYou` and `issues` may be functions, re-read on every /api/state.
async function setup({ threads, needsYou = [], issues = [], human = true, launchable = [], reply = () => ok({}), agents = AGENTS,
  url = 'http://127.0.0.1:8787/', storage = {} }) {
  const calls = [], prompts = [];
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', pretendToBeVisual: true, beforeParse(win) {
    for (const [k, v] of Object.entries({ 'agent-comms-seen': '{}', ...storage })) win.localStorage.setItem(k, v);
    for (const name of ['confirm', 'prompt', 'alert'])
      win[name] = text => { prompts.push([name, text]); throw new Error(`window.${name} must not be called`); };
    win.HTMLElement.prototype.scrollIntoView = function () { (win.scrolled = win.scrolled || []).push(this.id || this.dataset.post || this.tagName); };
    win.fetch = async (u, opts = {}) => {
      const me = human ? { name: 'human', is_human: true } : { name: 'codex', is_human: false };
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, paused: false,
        threads: typeof threads === 'function' ? threads() : threads,
        needs_you: typeof needsYou === 'function' ? needsYou() : needsYou,
        issues: typeof issues === 'function' ? issues() : issues, needs_you_issues: [],
        sessions: [], limits: { body_max_bytes: 4096 }, authorization_grants: [], task_categories: [], active_runs: [],
        launchable_agents: launchable,
        agents });
      calls.push({ u, method: opts.method, headers: opts.headers, body: opts.body ? JSON.parse(opts.body) : undefined });
      return reply(u, opts);
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, calls, prompts };
}
const items = d => [...d.querySelectorAll('#needs-you .ny-item')].map(n => Number(n.dataset.post));
const contexts = d => [...d.querySelectorAll('#needs-you .ny-item .ask-context .where')].map(n => n.textContent);
// The option cards of an item, in order: their titles (without the badge) and badges.
const cards = (d, id) => [...d.querySelectorAll(`#needs-you [data-post="${id}"] .radio-card .rc-title`)].map(n => n.firstChild.textContent);
const badges = (d, id) => [...d.querySelectorAll(`#needs-you [data-post="${id}"] .radio-card`)]
  .map(n => (n.querySelector('.badge') || {}).textContent || '');
const card = (d, id, value) => d.querySelector(`#needs-you [data-post="${id}"] input[type="radio"][value="${value}"]`);
const primary = (d, id) => d.querySelector(`#needs-you [data-post="${id}"] .ny-primary`);
const button = (d, id, action) => d.querySelector(`#needs-you [data-post="${id}"] button[data-action="${action}"]`);
const pick = async (d, id) => { d.querySelector(`li[data-thread="${id}"]`).click(); await settle(10); };

test('the card sits at the top of the thread, only for items that need you, newest first, saying what each blocks', async () => {
  const q = post(10, 1, { type: 'question', agent: 'claude-code', needs_response: true });
  const d = post(12, 1, { type: 'decision', decision_status: 'proposal (NOT binding until the human finalizes it)' });
  const r = post(14, 1, { type: 'request', needs_response: true });
  const other = post(20, 2, { type: 'request', needs_response: true });
  const t1 = thread(1, [q, post(11, 1), d, post(13, 1), r]), t2 = thread(2, [other]), t3 = thread(3, [post(30, 3)]);
  const { dom, document, win } = await setup({ threads: [t1, t2, t3], needsYou: [r, d, q, other] });
  try {
    await pick(document, 1);
    const pane = document.getElementById('thread-pane');
    assert.equal(pane.firstElementChild.id, 'needs-you', 'above the thread header and posts');
    assert.equal(pane.children[1].id, 'thread-1');
    assert.deepEqual(items(document), [14, 12, 10], 'newest first, this thread only');
    assert.deepEqual(contexts(document), ['Blocking thread #1 · post #14 by codex', 'Blocking thread #1 · post #12 by codex',
      'Blocking thread #1 · post #10 by claude-code']);
    // A plain post has no structured question: its first line stands in, labelled as such.
    const first = document.querySelector('#needs-you [data-post="14"]');
    assert.equal(first.querySelector('.ask-label').textContent, 'Question (from the post)');
    assert.equal(first.querySelector('h3').textContent, 'post 14 ' + INJECTION);
    assert.equal(document.querySelector('#needs-you h2').textContent, 'Needs you (3)');
    assert.match(document.querySelector('li[data-thread="1"] .dot').getAttribute('aria-label'), /3 posts waiting on you: #10, #12, #14/);
    document.getElementById('show-completed').click();
    await settle();
    await pick(document, 3);
    assert.equal(document.getElementById('needs-you'), null, 'no card where nothing needs you');
    assert.equal(win.pwned, undefined);
    assert.equal(document.querySelector('img'), null, 'post text and titles are never parsed as HTML');
    assert.ok(document.querySelector('#needs-you, #thread-3 .body').textContent.includes('<img'), 'shown as text');
  } finally { dom.window.close(); }
});

test('hidden for an agent viewing the board', async () => {
  const r = post(14, 1, { type: 'request', needs_response: true, agent: 'claude-code' });
  const { dom, document } = await setup({ threads: [thread(1, [r])], needsYou: [r], human: false, url: 'http://127.0.0.1:8787/#thread-1' });
  assert.ok(document.getElementById('thread-1'));
  assert.equal(document.getElementById('needs-you'), null);
  dom.window.close();
});

test('cards per plain item: decisions get Finalize and Reject; Approve & launch only for a launchable author', async () => {
  const d = post(12, 1, { type: 'decision' }), sealedD = post(13, 1, { type: 'decision', sealed: true });
  const q = post(14, 1, { type: 'question', agent: 'claude-code', needs_response: true });
  const { dom, document } = await setup({ threads: [thread(1, [d, sealedD, q])], needsYou: [q, sealedD, d], launchable: ['codex'] });
  try {
    assert.deepEqual(cards(document, 12), ['Finalize the decision', 'Approve as proposed', 'Approve and allow one launch',
      'Reject the decision', 'Not now', 'Write your own reply']);
    assert.deepEqual(cards(document, 13), ['Approve as proposed', 'Approve and allow one launch', 'Reject the decision', 'Not now',
      'Write your own reply'], 'a sealed decision cannot be finalized until unsealed');
    assert.deepEqual(cards(document, 14), ['Approve as proposed', 'Not now', 'Write your own reply'], 'claude-code is not launchable');
    // Nothing is picked: one primary button, disabled, that says what to do.
    assert.equal(primary(document, 14).textContent, 'Choose an option');
    assert.equal(primary(document, 14).disabled, true);
    assert.equal(document.querySelectorAll('#needs-you [data-post="14"] .ny-primary').length, 1);
    // Plain posts offer to ask the author for options.
    assert.equal(button(document, 14, 'ask_options').textContent, 'Ask claude-code for options');
    assert.match(document.querySelector('#needs-you [data-post="14"] .ny-ask').textContent, /no structured options/);
  } finally { dom.window.close(); }
});

test('each card + primary button calls the resolve endpoint once, with no confirm, and the result line says what happened', async () => {
  const cases = [
    ['approve', 'Approve', { type: 'request', needs_response: true }, { action: 'approve', post_id: 90, resolved_post_id: 14, to: ['codex'] },
      'Approved #14; told codex to go ahead (post #90).'],
    ['not_now', 'Not now', { type: 'request', needs_response: true }, { action: 'not_now', post_id: 91, resolved_post_id: 14, to: ['codex'] },
      'Parked #14; told codex not now (post #91).'],
    ['reject', 'Reject decision', { type: 'decision' }, { action: 'reject', post_id: 92, resolved_post_id: 14, to: ['codex'] },
      'Rejected decision #14; told codex (post #92).'],
    ['approve_launch', 'Approve and allow launch', { type: 'request', needs_response: true }, { action: 'approve_launch', post_id: 93,
      resolved_post_id: 14, to: ['codex'], agent: 'codex', rule_id: 5, dispatcher_running: true, paused: false, live: false, no_runner: false },
      'Approved #14 and launched codex (dispatcher running; it starts within seconds) (post #93).'],
    ['approve_launch', 'Approve and allow launch', { type: 'request', needs_response: true }, { action: 'approve_launch', post_id: 94,
      resolved_post_id: 14, to: ['codex'], agent: 'codex', rule_id: 6, dispatcher_running: false, paused: false, live: false, no_runner: false },
      "Approved #14 (post #94). The dispatcher isn't running — start it with `board dispatch run` to launch codex."],
  ];
  for (const [action, label, extra, answer, message] of cases) {
    const item = post(14, 1, extra);
    let waiting = [item];
    const reply = u => { if (u === '/api/posts/14/resolve') { waiting = []; return ok(answer); } return fail(404, 'nf'); };
    const { dom, document, calls, prompts } = await setup({ threads: [thread(1, [item])], needsYou: () => waiting, reply,
      launchable: ['codex'] });
    try {
      card(document, 14, action).click();
      assert.equal(calls.length, 0, 'picking a card sends nothing');
      assert.equal(primary(document, 14).textContent, label, 'the button names its effect');
      assert.equal(primary(document, 14).disabled, false);
      primary(document, 14).click();
      await settle();
      assert.deepEqual(prompts, [], 'no window.confirm/prompt/alert');
      const sent = calls.filter(c => c.u === '/api/posts/14/resolve');
      assert.equal(sent.length, 1, action);
      assert.equal(sent[0].method, 'POST');
      assert.equal(sent[0].headers['X-Board-Request'], '1');
      assert.deepEqual(sent[0].body, action.startsWith('approve') ? { action, delivery_agent: 'codex' } : { action }, 'approvals name the agent the select shows');
      assert.equal(document.querySelector('#needs-you-result span').textContent, message);
      assert.deepEqual(items(document), [], 'the item leaves the card on refresh');
      assert.match(document.querySelector('#needs-you h2').textContent, /nothing left here/);
      document.querySelector('#needs-you-result button').click();
      assert.equal(document.getElementById('needs-you'), null, 'dismissed: the card goes away');
    } finally { dom.window.close(); }
  }
});

test('Finalize uses the existing endpoint; errors show inline, not in an alert; a double click sends once', async () => {
  const d = post(12, 1, { type: 'decision' });
  let release;
  const gate = new Promise(r => { release = r; });
  const reply = async u => { if (u === '/api/posts/12/finalize') { await gate; return ok({ id: 12 }); }
    return fail(409, 'post #12 no longer needs you (it was already handled)'); };
  const { dom, document, calls, prompts } = await setup({ threads: [thread(1, [d])], needsYou: [d], reply });
  try {
    card(document, 12, 'finalize').click();
    assert.equal(primary(document, 12).textContent, 'Finalize decision');
    primary(document, 12).click();
    await settle(10);
    assert.ok(primary(document, 12).disabled && card(document, 12, 'approve').disabled, 'busy while in flight');
    assert.equal(primary(document, 12).textContent, 'Sending…');
    primary(document, 12).click();
    release();
    await settle();
    assert.equal(calls.filter(c => c.u === '/api/posts/12/finalize').length, 1);
    assert.equal(document.querySelector('#needs-you-result span').textContent, 'Finalized decision #12.');
    card(document, 12, 'approve').click();
    primary(document, 12).click();
    await settle();
    const res = document.getElementById('needs-you-result');
    assert.ok(res.classList.contains('failed'));
    assert.equal(res.querySelector('span').textContent, "Couldn't resolve #12: post #12 no longer needs you (it was already handled)");
    assert.equal(card(document, 12, 'approve').checked, true, 'the choice survives a failure');
    assert.deepEqual(prompts, []);
  } finally { dom.window.close(); }
});

test('Write your own reply reveals a textarea; Send reply posts the human text; the byte limit is enforced', async () => {
  const q = post(14, 1, { type: 'question', needs_response: true });
  let waiting = [q];
  const reply = u => { if (u === '/api/posts/14/resolve') { waiting = []; return ok({ action: 'reply', post_id: 95, resolved_post_id: 14, to: ['codex'] }); }
    return fail(404, 'nf'); };
  const { dom, document, win, calls, prompts } = await setup({ threads: [thread(1, [q])], needsYou: () => waiting, reply });
  try {
    const box = () => document.querySelector('#needs-you [data-post="14"] .ny-reply');
    assert.equal(box().hidden, true, 'hidden until Write your own reply is picked');
    card(document, 14, 'custom').click();
    assert.equal(box().hidden, false);
    const area = document.querySelector('#needs-you [data-post="14"] textarea.ny-textarea');
    assert.equal(primary(document, 14).textContent, 'Send reply');
    assert.ok(primary(document, 14).disabled, 'nothing to send yet');
    area.value = 'x'.repeat(4097); area.dispatchEvent(new win.Event('input'));
    assert.ok(primary(document, 14).disabled, 'over 4 KB');
    assert.match(document.querySelector('#needs-you .ny-count').textContent, /4097 \/ 4096 bytes — too long/);
    assert.match(document.querySelector('#needs-you [data-post="14"] .hint').textContent, /too long/);
    area.value = '  Use option B, not ' + INJECTION + '  '; area.dispatchEvent(new win.Event('input'));
    assert.ok(!primary(document, 14).disabled);
    await win.refresh(); await settle(10);
    assert.equal(document.querySelector('#needs-you textarea.ny-textarea').value, '  Use option B, not ' + INJECTION + '  ', 'the draft survives a refresh');
    assert.equal(card(document, 14, 'custom').checked, true, 'so does the choice');
    primary(document, 14).click();
    await settle();
    const [sent] = calls.filter(c => c.u === '/api/posts/14/resolve');
    assert.deepEqual(sent.body, { action: 'reply', text: 'Use option B, not ' + INJECTION });
    assert.equal(document.querySelector('#needs-you-result span').textContent, 'Replied to #14 (to codex) (post #95).');
    assert.equal(document.querySelector('#needs-you textarea'), null);
    assert.equal(win.pwned, undefined);
    assert.deepEqual(prompts, []);
  } finally { dom.window.close(); }
});

test('a structured post: Recommended, Alternative, Write your own reply; Choose sends the option and an optional note', async () => {
  const s = post(30, 1, { type: 'proposal', agent: 'claude-code', needs_response: true, decision_question: QUESTION() });
  let waiting = [s];
  const reply = u => { if (u === '/api/posts/30/resolve') { waiting = []; return ok({ action: 'choose', post_id: 96,
    resolved_post_id: 30, to: ['claude-code'], option_id: 'ship' }); } return fail(404, 'nf'); };
  const { dom, document, win, calls, prompts } = await setup({ threads: [thread(1, [s])], needsYou: () => waiting, reply,
    launchable: ['claude-code'] });
  try {
    const item = document.querySelector('#needs-you [data-post="30"]');
    assert.equal(item.querySelector('.ask-context .where').textContent, 'Blocking thread #1 · post #30 by claude-code');
    assert.equal(item.querySelector('.ask-label').textContent, 'Question');
    assert.equal(item.querySelector('h3').textContent, QUESTION().question);
    assert.match(item.querySelector('.ny-body').textContent, /^Trailing commas are dropped/);
    assert.deepEqual(cards(document, 30), ['Ship it now', 'Wait for the refactor ' + INJECTION, 'Write your own reply']);
    assert.deepEqual(badges(document, 30), ['Recommended', 'Alternative', '']);
    assert.match(item.querySelector('.radio-card').textContent, /Merges today; costs a re-review/);
    assert.equal(button(document, 30, 'ask_options'), null, 'structured: nothing to ask for');
    assert.equal(card(document, 30, 'approve'), null, 'no fixed one-click answers in place of the options');
    card(document, 30, 'option:wait').click();
    assert.equal(primary(document, 30).textContent, 'Choose Alternative');
    card(document, 30, 'option:ship').click();
    assert.equal(primary(document, 30).textContent, 'Choose Recommended');
    const note = item.querySelector('textarea.ny-note');
    assert.equal(note.closest('.ny-note-box').hidden, false, 'a note can go with a chosen option');
    note.value = 'only the parser'; note.dispatchEvent(new win.Event('input'));
    assert.equal(calls.length, 0);
    primary(document, 30).click();
    await settle();
    const sent = calls.filter(c => c.u === '/api/posts/30/resolve');
    assert.equal(sent.length, 1);
    // An approved option queues work: it names the agent the select shows (here the author).
    assert.deepEqual(sent[0].body, { action: 'choose', option_id: 'ship', note: 'only the parser', delivery_agent: 'claude-code' });
    assert.equal(document.querySelector('#needs-you-result span').textContent,
      'Chose the recommended option “Ship it now” for #30; told claude-code (post #96).');
    assert.deepEqual(prompts, []);
    assert.equal(win.pwned, undefined); assert.equal(document.querySelector('img'), null);
  } finally { dom.window.close(); }
});

test('a plain post can ask its author for options, in one click', async () => {
  const p = post(40, 1, { type: 'proposal', agent: 'claude-code', needs_response: true,
    body: 'Two ways forward.\n(A, recommended) do x\n(B) do y' });
  let waiting = [p];
  const reply = u => { if (u === '/api/posts/40/resolve') { waiting = []; return ok({ action: 'ask_options', post_id: 97,
    resolved_post_id: 40, to: ['claude-code'] }); } return fail(404, 'nf'); };
  const { dom, document, calls, prompts } = await setup({ threads: [thread(1, [p])], needsYou: () => waiting, reply });
  try {
    assert.equal(document.querySelector('#needs-you [data-post="40"] h3').textContent, 'Two ways forward.');
    button(document, 40, 'ask_options').click();
    await settle();
    const sent = calls.filter(c => c.u === '/api/posts/40/resolve');
    assert.deepEqual(sent.map(c => c.body), [{ action: 'ask_options' }]);
    assert.equal(document.querySelector('#needs-you-result span').textContent,
      'Asked claude-code to restate #40 with a recommended option and an alternative (post #97).');
    assert.deepEqual(prompts, []);
  } finally { dom.window.close(); }
});

test('an answered linked issue is not shown as the blocker; the card names the post that is', async () => {
  // The thread-7 situation: issue #4 is answered for this thread, but post #221 (a plain proposal) still waits.
  const p = post(221, 7, { type: 'proposal', agent: 'claude-code', needs_response: true, body: 'Pick one: (A, recommended) x (B) y' });
  const answered = { id: 4, title: 'Shared blocker', body: 'b', status: 'open', needs_human: false, created_by: 'codex',
    created_at: MINUTES_AGO(90), updated_at: MINUTES_AGO(37), comments: [], resolution: null,
    decisions: [{ id: 9, outcome: 'approved', body: 'Approved', agent: 'human', thread_ids: [7], created_at: MINUTES_AGO(37) }],
    links: [{ thread_id: 7, post_id: null, needs_human: false, project: '/repo/app', title: 'Thread 7' }] };
  const { dom, document } = await setup({ threads: [thread(7, [p])], needsYou: [p], issues: [answered] });
  try {
    const card7 = document.getElementById('needs-you');
    assert.equal(card7.querySelector('[data-issue="4"]'), null, 'the answered issue is not offered for a decision');
    assert.equal(card7.querySelector('.ny-explain').textContent, 'Issue #4 is answered; this thread is still waiting on post #221.');
    assert.deepEqual(contexts(document), ['Blocking thread #7 · post #221 by claude-code']);
    assert.match(document.querySelector('#thread-issues').textContent, /Human answered · unresolved/);
    assert.match(document.querySelector('li[data-thread="7"] .dot').getAttribute('aria-label'), /Waiting on you: post #221/);
    assert.match(document.querySelector('li[data-thread="7"] .needs-chip').title, /post #221/);
  } finally { dom.window.close(); }
});

test('the sidebar lists every item compactly: what it blocks, the question, the options at a glance', async () => {
  const s = post(30, 1, { type: 'question', needs_response: true, decision_question: QUESTION() });
  const p = post(31, 2, { type: 'request', needs_response: true, body: '\n\n  Can I delete the cache? ' + INJECTION + '\nmore' });
  const issue = { id: 5, title: 'Detector', body: 'b', status: 'open', needs_human: true, created_by: 'codex', created_at: MINUTES_AGO(9),
    updated_at: MINUTES_AGO(9), comments: [], decisions: [], resolution: null, question_version: 1, decision_question: QUESTION(),
    links: [{ thread_id: 1, post_id: null, needs_human: true, project: '/repo/app', title: 'T1' },
      { thread_id: 2, post_id: null, needs_human: true, project: '/repo/app', title: 'T2' }] };
  const { dom, document, win } = await setup({ threads: [thread(1, [s]), thread(2, [p])], needsYou: [p, s], issues: [issue] });
  try {
    const side = document.getElementById('needs-you-side');
    assert.equal(side.querySelector('h2').textContent, 'Needs you (3)');
    const rows = [...side.querySelectorAll('.ask-compact')];
    assert.deepEqual(rows.map(r => r.dataset.issue ? 'issue-' + r.dataset.issue : 'post-' + r.dataset.post), ['issue-5', 'post-31', 'post-30']);
    assert.equal(rows[0].querySelector('.ask-context').textContent, 'Issue #5 · blocks threads #1, #2');
    assert.equal(rows[0].querySelector('a').textContent, QUESTION().question);
    assert.equal(rows[0].querySelector('.ask-options').textContent, 'Recommended: Ship it now · Alternative: Wait for the refactor ' + INJECTION);
    assert.match(rows[1].querySelector('.ask-context').textContent, /^Blocking thread #2 · post #31 by codex/);
    assert.equal(rows[1].querySelector('a').textContent, 'Can I delete the cache? ' + INJECTION);
    assert.equal(rows[1].querySelector('.ask-options').textContent, 'No structured options · approve, not now or reply');
    assert.match(rows[2].querySelector('.ask-options').textContent, /^Recommended: Ship it now/);
    assert.equal(win.pwned, undefined); assert.equal(document.querySelector('img'), null);
  } finally { dom.window.close(); }
});

test('a long first line is trimmed to about 140 characters for the heading', async () => {
  const p = post(50, 1, { type: 'question', needs_response: true, body: 'word '.repeat(60) });
  const { dom, document } = await setup({ threads: [thread(1, [p])], needsYou: [p] });
  try {
    const h = document.querySelector('#needs-you [data-post="50"] h3').textContent;
    assert.ok(h.length <= 140 && h.endsWith('…'), h);
  } finally { dom.window.close(); }
});

test('long bodies collapse with Show more; Jump to post scrolls to and highlights it in the thread', async () => {
  const long = post(14, 1, { type: 'request', needs_response: true, body: Array.from({ length: 12 }, (_, i) => 'line ' + i).join('\n'),
    refs: [{ kind: 'file', path: 'src/a.py', rev: 'abc123' }] });
  const { dom, document, win } = await setup({ threads: [thread(1, [post(10, 1), long])], needsYou: [long] });
  try {
    const body = () => document.querySelector('#needs-you .ny-body');
    assert.ok(body().classList.contains('clamped'));
    assert.equal(document.querySelector('#needs-you .refs').textContent, 'file: src/a.py @ abc123');
    button(document, 14, 'more').click();
    assert.ok(!body().classList.contains('clamped'));
    assert.equal(button(document, 14, 'more').textContent, 'Show less');
    win.scrolled = [];
    button(document, 14, 'jump').click();
    await settle(10);
    assert.ok(document.getElementById('post-14').classList.contains('highlight'));
    assert.ok(win.scrolled.includes('post-14'), 'scrolled to the post in the thread');
  } finally { dom.window.close(); }
});

test('the "needs you" chip in the list opens the thread at the callout; so do the sidebar items', async () => {
  const r = post(20, 2, { type: 'request', needs_response: true });
  const q = post(30, 3, { type: 'question', needs_response: true });
  const { dom, document, win } = await setup({ threads: [thread(1, [post(10, 1)]), thread(2, [r]), thread(3, [q])],
    needsYou: [q, r], storage: { 'agent-comms-thread': '1' } });
  try {
    assert.equal(document.getElementById('needs-you'), null);
    const chip = document.querySelector('li[data-thread="2"] button.needs-chip');
    assert.equal(chip.textContent, '1 needs you');
    win.scrolled = [];
    chip.click();
    await settle(10);
    assert.equal(document.querySelector('li[data-selected=true]').dataset.thread, '2');
    assert.deepEqual(items(document), [20]);
    assert.deepEqual(win.scrolled, ['needs-you'], 'the callout is brought into view');
    assert.equal(win.location.hash, '#thread-2');
    win.scrolled = [];
    document.querySelector('.ny-side[data-post="30"]').click();
    await settle(10);
    assert.equal(document.querySelector('li[data-selected=true]').dataset.thread, '3');
    assert.deepEqual(items(document), [30]);
    assert.deepEqual(win.scrolled, ['30'], 'scrolled to that item in the callout');
    document.querySelector('.ny-side[data-post="20"] a').click();   // the #N link does the same, not a hash jump
    await settle(10);
    assert.deepEqual(items(document), [20]);
    assert.equal(win.location.hash, '#thread-2');
  } finally { dom.window.close(); }
});

test('a #post-N deep link to a needs-you post shows the callout and lands on its block', async () => {
  const r = post(20, 2, { type: 'request', needs_response: true });
  const { dom, document, win } = await setup({ threads: [thread(1, [post(10, 1)]), thread(2, [post(19, 2), r])], needsYou: [r],
    url: 'http://127.0.0.1:8787/#post-20' });
  try {
    await settle();
    assert.equal(document.querySelector('li[data-selected=true]').dataset.thread, '2');
    assert.equal(document.getElementById('thread-pane').firstElementChild.id, 'needs-you');
    assert.deepEqual(items(document), [20]);
    assert.ok(win.scrolled.includes('20'), 'scrolled to the callout block');
    assert.ok(document.getElementById('post-20').classList.contains('highlight'), 'the post stays highlighted below');
  } finally { dom.window.close(); }
});

test('selective closeout retains original request and evidence without hiding other attention', async () => {
  const closed = post(10, 1, { type: 'question', needs_response: true,
    attention_resolution: { resolved_by: 'codex', session_id: 44, reason: 'Verified alternative browser ' + INJECTION,
      evidence_post_ids: [12], resolved_at: MINUTES_AGO(1) } });
  const pending = post(11, 1, { type: 'proposal', needs_response: true });
  const { dom, document } = await setup({ threads: [thread(1, [closed, pending, post(12, 1)])], needsYou: [pending] });
  try {
    await pick(document, 1);
    assert.deepEqual(items(document), [11]);
    const source = document.getElementById('post-10');
    assert.match(source.textContent, /Attention resolved by codex/);
    assert.match(source.textContent, /post 10/);
    assert.doesNotMatch(source.querySelector('.meta').textContent, /needs response/);
    assert.equal(source.querySelector('.attention-resolution a').getAttribute('href'), '#post-12');
    assert.equal(source.querySelector('img'), null);
  } finally { dom.window.close(); }
});

test('mechanical close exposes exact target, version and evidence before execution', async () => {
  const q = QUESTION();
  q.options[1].action = { type: 'close', post_id: 3, recipient: 'codex', expected_version: 2, evidence_post_ids: [4] };
  const p = post(11, 1, { type: 'question', needs_response: true, decision_question: q });
  const { dom, document, calls } = await setup({ threads: [thread(1, [p])], needsYou: [p] });
  try {
    await pick(document, 1);
    const node = document.querySelector('#needs-you [data-post="11"]');
    assert.match(node.textContent, /Closes request #3\/codex \(version 2\) using evidence #4/);
    card(document,11,'option:ship').click();
    assert.equal(primary(document,11).textContent, 'Choose and close');
    assert.equal(node.querySelector('.ny-assign').hidden, true);
    primary(document,11).click(); await settle();
    assert.deepEqual(calls.find(c => c.u.endsWith('/11/resolve')).body, {action:'choose',option_id:'ship'});
  } finally { dom.window.close(); }
});

test('assign approved work sends explicit selected implementer, never parses notes', async () => {
  const p = post(11,1,{type:'question',needs_response:true,decision_question:QUESTION()});
  const { dom, win, document, calls } = await setup({threads:[thread(1,[p])],needsYou:[p]});
  try {
    await pick(document,1);
    card(document,11,'option:ship').click();
    const select = document.querySelector('#needs-you [data-post="11"] select');
    select.value='claude-code'; select.dispatchEvent(new win.Event('change'));
    primary(document,11).click(); await settle();
    assert.equal(calls.find(c => c.u.endsWith('/11/resolve')).body.delivery_agent,'claude-code');
  } finally { dom.window.close(); }
});

test('an inactive author: the assignee the select shows is the one sent; with no active agent, approving is disabled', async () => {
  const p = post(11, 1, { agent: 'old-bot', type: 'request', needs_response: true });
  const inactive = [...AGENTS.map(a => a.name === 'claude-code' ? a : { ...a, active: 0 }), { name: 'old-bot', is_human: 0, active: 0 }];
  const { dom, document, calls } = await setup({ threads: [thread(1, [p])], needsYou: [p], agents: inactive });
  try {
    card(document, 11, 'approve').click();
    const select = document.querySelector('#needs-you [data-post="11"] select');
    assert.equal(select.value, 'claude-code', 'the first active agent is preselected');
    assert.equal(select.querySelector('option[selected]').value, 'claude-code');
    primary(document, 11).click(); await settle();
    assert.deepEqual(calls.find(c => c.u.endsWith('/11/resolve')).body, { action: 'approve', delivery_agent: 'claude-code' });
  } finally { dom.window.close(); }
  const none = AGENTS.map(a => ({ ...a, active: 0 }));
  const empty = await setup({ threads: [thread(1, [p])], needsYou: [p], agents: none });
  try {
    card(empty.document, 11, 'approve').click();
    assert.equal(primary(empty.document, 11).disabled, true, 'nobody could take the work');
    assert.match(empty.document.querySelector('#needs-you [data-post="11"] .hint').textContent, /No active agent/);
    card(empty.document, 11, 'not_now').click();
    assert.equal(primary(empty.document, 11).disabled, false, 'answers that assign nothing still work');
  } finally { empty.dom.window.close(); }
});

// Regression: "when I submit one answer in a group of multiple answers, the whole group closes". The page sends one
// request for exactly the item answered; the others stay, with their own controls and the human's unsent drafts.
test('answering one of several items in a thread sends only that item and keeps the others with their drafts', async () => {
  let open = [post(13, 1, { type: 'proposal', needs_response: true }), post(12, 1, { type: 'decision' }),
    post(11, 1, { type: 'question', needs_response: true, decision_question: QUESTION() }),
    post(10, 1, { type: 'question', needs_response: true })];   // newest first, as the server sends them
  const answered = id => { open = open.filter(p => p.id !== id); return ok({ post_id: 99, resolved_post_id: id, to: ['codex'] }); };
  const { dom, document, calls } = await setup({ threads: () => [thread(1, [...open].reverse())], needsYou: () => open,
    reply: (u, opts) => u.endsWith('/resolve') ? answered(Number(u.split('/')[3])) : u.endsWith('/finalize') ? answered(12) : ok({}) });
  try {
    await pick(document, 1);
    assert.deepEqual(items(document), [13, 12, 11, 10]);
    card(document, 10, 'custom').click();
    const area = document.querySelector('#ny-reply-10');
    area.value = 'my unsent reply'; area.dispatchEvent(new document.defaultView.Event('input'));
    card(document, 11, 'option:ship').click();
    primary(document, 11).click();
    await settle();
    assert.deepEqual(calls.map(c => [c.u, c.body]), [['/api/posts/11/resolve', { action: 'choose', option_id: 'ship', delivery_agent: 'codex' }]]);
    assert.deepEqual(items(document), [13, 12, 10], 'only #11 left the card');
    assert.equal(document.querySelector('#needs-you h2').textContent, 'Needs you (3)');
    assert.equal(document.querySelector('#ny-reply-10').value, 'my unsent reply', 'another item\'s draft survives');
    assert.equal(card(document, 10, 'custom').checked, true);
    for (const id of [10, 12, 13]) assert.ok(primary(document, id), `#${id} keeps its own controls`);
    assert.deepEqual([...document.querySelectorAll('#needs-you-side [data-post]')].map(n => Number(n.dataset.post)), [13, 12, 10]);
    // Answering the rest clears them, one by one.
    primary(document, 10).click(); await settle();
    card(document, 12, 'finalize').click(); primary(document, 12).click(); await settle();
    card(document, 13, 'not_now').click(); primary(document, 13).click(); await settle();
    assert.deepEqual(calls.slice(1).map(c => c.u), ['/api/posts/10/resolve', '/api/posts/12/finalize', '/api/posts/13/resolve']);
    assert.deepEqual(items(document), []);
    assert.equal(document.querySelector('#needs-you h2').textContent, 'Needs you: nothing left here');
  } finally { dom.window.close(); }
});

test('an issue answer leaves linked posts that ask their own question in Needs you, and says so', async () => {
  const own1 = post(30, 1, { type: 'question', needs_response: true, decision_question: QUESTION() });
  const own2 = post(31, 1, { type: 'question', needs_response: true, decision_question: { ...QUESTION(), question: 'Other?' } });
  let open = [own2, own1], flagged = { 1: true, 2: true };
  const issue = () => ({ id: 5, title: 'Detector', body: 'b', status: 'open', needs_human: flagged[1] || flagged[2],
    created_by: 'codex', created_at: MINUTES_AGO(9), updated_at: MINUTES_AGO(9), comments: [], decisions: [], resolution: null,
    question_version: 1, decision_question: { ...QUESTION(), question: 'Issue question?' },
    links: [{ thread_id: 1, post_id: 30, needs_human: flagged[1], covers_post: false, project: '/repo/app', title: 'T1' },
      { thread_id: 1, post_id: 31, needs_human: flagged[1], covers_post: false, project: '/repo/app', title: 'T1' },
      { thread_id: 1, post_id: 32, needs_human: flagged[1], covers_post: true, project: '/repo/app', title: 'T1' },
      { thread_id: 2, post_id: 40, needs_human: flagged[2], covers_post: true, project: '/repo/app', title: 'T2' }] });
  const { dom, document, calls } = await setup({ threads: [thread(1, [own1, own2, post(32, 1)]), thread(2, [post(40, 2)])],
    needsYou: () => open, issues: () => [issue()],
    reply: (u, opts) => {
      const body = JSON.parse(opts.body || '{}');
      if (u === '/api/issues/5/decisions') { for (const t of body.thread_ids) flagged[t] = false; return ok(issue()); }
      if (u.endsWith('/resolve')) { open = open.filter(p => p.id !== Number(u.split('/')[3])); return ok({ post_id: 99 }); }
      return ok({});
    } });
  try {
    await pick(document, 1);
    const ny = document.getElementById('needs-you');
    assert.equal(ny.querySelector('h2').textContent, 'Needs you (3)', 'the issue and both posts with their own question');
    assert.equal(ny.querySelector('[data-separate]').textContent,
      'Linked posts #30, #31 each ask their own question: this answer does not answer them. Answer each on their own (each is its own Needs you item until answered).');
    const form = ny.querySelector('[data-decision-form="5"]');
    form.querySelector('input[value="option:ship"]').click();
    form.querySelector('.scope input[value="2"]').click();     // this thread only
    form.querySelector('button[type="submit"]').click();
    await settle();
    assert.deepEqual(calls.map(c => [c.u, c.body.thread_ids]), [['/api/issues/5/decisions', [1]]]);
    assert.deepEqual(items(document).filter(Boolean), [31, 30], 'the posts keep their own items and controls');
    assert.equal(document.querySelector('#needs-you [data-issue="5"]'), null, 'the issue no longer waits here');
    assert.ok(document.querySelector('#needs-you-side [data-issue="5"]'), 'it still waits on thread #2');
    card(document, 30, 'option:wait').click(); primary(document, 30).click(); await settle();
    assert.deepEqual(items(document).filter(Boolean), [31]);
    card(document, 31, 'option:ship').click(); primary(document, 31).click(); await settle();
    assert.deepEqual(items(document).filter(Boolean), []);
    assert.deepEqual(calls.slice(1).map(c => c.u), ['/api/posts/30/resolve', '/api/posts/31/resolve']);
    // The note comes from the issue's own link data, not from the (capped) Needs you list: it still names them.
    await pick(document, 2);
    assert.equal(document.querySelector('#needs-you [data-decision-form="5"] [data-separate]').textContent, 'Linked posts #30, #31 each ask their own question: this answer does not answer them. Answer each on their own (each is its own Needs you item until answered).');
  } finally { dom.window.close(); }
});
