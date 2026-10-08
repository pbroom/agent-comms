// Shared issue interactions use a mocked API; authorization is enforced by the backend tests.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(r => setTimeout(r, 40));
const ok = body => ({ ok: true, status: 200, json: async () => body });
const at = new Date().toISOString();
const poison = '<img src=x onerror="window.pwned=true">';
const issue = (id = 1) => ({ id, title: 'Git metadata blocker ' + poison, body: 'Shared evidence', status: 'open', needs_human: true,
  created_by: 'codex', created_at: at, updated_at: at, comments: [], decisions: [], resolution: null,
  links: [{ thread_id: 10, post_id: 100, project: '/repo/one', title: 'Detector fix' },
    { thread_id: 20, post_id: 200, project: '/repo/two', title: 'Review fix' }] });
async function setup({ issues = [issue()], human = true, hash = '#thread-10', pendingIssues = [], reply, storage = {} } = {}) {
  const calls = [];
  const me = { name: human ? 'human' : 'codex', is_human: human };
  const post = { id: 100, thread_id: 10, seq: 1, agent: 'codex', session_id: 1, type: 'question', body: 'Blocked',
    to: [], needs_response: true, refs: [], sealed: false, created_at: at };
  const thread = { id: 10, title: 'Detector fix', project: '/repo/one', status: 'open', agent_posts_since_human: 1,
    thread_cap: 12, tasks: [], task_counts: {}, posts: [post], created_at: at };
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/' + hash, runScripts: 'dangerously', beforeParse(win) {
    win.HTMLElement.prototype.scrollIntoView = () => {};
    for (const [k, v] of Object.entries(storage)) win.localStorage.setItem(k, v);
    win.fetch = async (u, opts = {}) => {
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, issues, needs_you_issues: pendingIssues, threads: [thread], needs_you: [], sessions: [], paused: false,
        authorization_grants: [], task_categories: [], active_runs: [], launchable_agents: [], agents: [], limits: {} });
      calls.push({ path: u, body: opts.body && JSON.parse(opts.body), method: opts.method });
      if (reply) return reply(u, calls.at(-1).body);
      if (u.startsWith('/api/issues?query=')) {
        const q = new URL(u, 'http://localhost').searchParams.get('query').toLowerCase();
        return ok((issues || []).filter(i => `${i.title} ${i.body} ${i.links.map(l => l.project + ' ' + l.title).join(' ')}`.toLowerCase().includes(q)));
      }
      return ok({ id: 1 });
    };
  } });
  await settle();
  return { dom, win: dom.window, d: dom.window.document, calls };
}
function button(d, text) { return [...d.querySelectorAll('button')].find(b => b.textContent === text); }
// Radio groups (the outcome and contribution segmented controls) are filled by clicking the matching radio.
function fill(win, d, name, value) {
  const radio = d.querySelector(`[name="${name}"][type="radio"][value="${value}"]`);
  if (radio) { radio.click(); return radio; }
  const node = d.querySelector(`[name="${name}"]`); node.value = value;
  node.dispatchEvent(new win.Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  return node;
}
const send = (d, id = 1) => d.querySelector(`[data-decide="${id}"]`);
const scope = (root, id) => root.querySelector(`.decision .toggle-chip input[value="${id}"]`);
const checkedScopes = root => [...root.querySelectorAll('.decision .toggle-chip input:checked')].map(n => Number(n.value));
function submit(win, node) { node.closest('form').dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true })); }

test('Needs you surfaces one issue with source links and renders untrusted content as text', async () => {
  const { dom, d, win } = await setup();
  try {
    assert.equal(d.querySelectorAll('.pane-side [data-issue="1"]').length, 1);
    assert.match(d.querySelector('.pane-side').textContent, /Needs you \(1\)/);
    assert.equal(d.querySelectorAll('#thread-issues a').length, 1);
    d.getElementById('tab-issues').click(); await settle();
    assert.equal(d.getElementById('tab-issues').getAttribute('aria-selected'), 'true');
    d.querySelector('.issue-row').click();
    assert.equal(d.querySelector('#issue-1 a[href="#post-100"]').textContent, 'post #100');
    assert.equal(d.querySelector('#issue-1 a[href="#post-200"]').textContent, 'post #200');
    assert.equal(d.querySelector('img'), null); assert.equal(win.pwned, undefined);
  } finally { dom.window.close(); }
});

test('post reuse requires explicit linking and never silently merges a search match', async () => {
  const { dom, d, win, calls } = await setup();
  try {
    d.querySelector('[data-link-issue="100"]').click(); await settle();
    const search = d.querySelector('[type="search"]'); search.value = 'metadata'; search.dispatchEvent(new win.Event('input')); await settle();
    assert.equal(d.querySelectorAll('.issue-row').length, 1);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/api/issues?query=metadata');
    d.querySelector('.issue-row').click();
    assert.equal(calls.filter(c => c.method === 'POST').length, 0);
    assert.match(d.querySelector('[data-link-to="1"]').textContent, /Link post #100 to issue #1/);
    d.querySelector('[data-link-to="1"]').click(); await settle();
    assert.deepEqual(calls.find(c => c.method === 'POST'), { path: '/api/issues/1/links', method: 'POST', body: { thread_id: 10, post_id: 100 } });
  } finally { dom.window.close(); }
});

test('create retains exact originating post and drafts; failed writes keep the draft', async () => {
  const { dom, d, win, calls } = await setup({ issues: [], reply: () => ({ ok: false, status: 400, json: async () => ({ message: 'Try again' }) }) });
  try {
    d.querySelector('[data-link-issue="100"]').click(); await settle();
    fill(win, d, 'title', 'Different blocker'); const body = fill(win, d, 'body', 'Exact context'); submit(win, body); await settle();
    assert.deepEqual(calls[0].body, { title: 'Different blocker', body: 'Exact context', needs_human: true, thread_id: 10, post_id: 100 });
    assert.equal(d.querySelector('[name="title"]').value, 'Different blocker');
    assert.match(d.querySelector('[role="alert"]').textContent, /Try again/);
  } finally { dom.window.close(); }
});

test('decision requires explicit thread scope and does not resolve implementation', async () => {
  const data = issue();
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1', reply: (url, body) => {
    if (url.endsWith('/decisions')) { data.needs_human = false; data.decisions.push({ id: 1, ...body, agent: 'human', created_at: at }); }
    return ok(data);
  } });
  try {
    // No thread is flagged as awaiting, so every linked thread starts selected; the human narrows it.
    assert.deepEqual(checkedScopes(d), [10, 20]);
    scope(d, 10).click(); scope(d, 20).click();
    submit(win, fill(win, d, 'decision', 'Approved only for detector work')); await settle();
    assert.equal(calls.length, 0); assert.match(d.querySelector('[role="alert"]').textContent, /Select the threads/);
    assert.equal(send(d).disabled, true);
    fill(win, d, 'outcome', 'approved'); scope(d, 10).click();
    assert.equal(send(d).textContent, 'Approve for 1 thread'); assert.equal(send(d).disabled, false);
    submit(win, d.querySelector('[name="decision"]')); await settle();
    assert.deepEqual(calls[0].body, { body: 'Approved only for detector work', outcome: 'approved', thread_ids: [10], expected_question_version: 0 });
    assert.match(d.querySelector('#issue-1').textContent, /Human answered · unresolved/);
    assert.equal(calls.some(c => c.path.endsWith('/resolve')), false);
    assert.ok(button(d, 'Mark resolved'));
  } finally { dom.window.close(); }
});

test('comments, evidence, proposals and new human requests are separate from resolution', async () => {
  const { dom, d, win, calls } = await setup({ hash: '#issue-1' });
  try {
    for (const kind of ['comment', 'evidence', 'proposal', 'request']) {
      fill(win, d, 'kind', kind); submit(win, fill(win, d, 'body', kind + ' content')); await settle();
      assert.deepEqual(calls.at(-1).body, { body: kind + ' content', kind });
      assert.equal(calls.at(-1).path, '/api/issues/1/comments');
    }
    submit(win, fill(win, d, 'resolution', 'Verified on isolated fixture')); await settle();
    assert.deepEqual(calls.at(-1).body, { body: 'Verified on isolated fixture' });
    assert.equal(calls.at(-1).path, '/api/issues/1/resolve');
  } finally { dom.window.close(); }
});

test('agent issue view is read-only and old snapshots without issues still render', async () => {
  const { dom, d } = await setup({ human: false, hash: '#issue-1' });
  try { assert.equal(d.querySelector('.issue-form'), null); assert.ok(d.getElementById('issue-1')); }
  finally { dom.window.close(); }
  const old = await setup({ issues: null });
  old.dom.window.close();
});


test('reopened issues retain historical evidence and allow a new verified resolution', async () => {
  const data = issue(); data.status = 'resolved'; data.needs_human = false;
  data.resolution = { body: 'First verification', agent: 'human', created_at: at };
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1', reply: (url, body) => {
    if (url.endsWith('/comments') && body.kind === 'request') { data.status = 'open'; data.needs_human = true; }
    if (url.endsWith('/resolve')) { data.status = 'resolved'; data.needs_human = false; data.resolution = { ...body, agent: 'human', created_at: at }; }
    return ok(data);
  } });
  try {
    assert.equal(button(d, 'Mark resolved'), undefined);
    fill(win, d, 'kind', 'request'); submit(win, fill(win, d, 'body', 'Blocker returned')); await settle();
    assert.match(d.querySelector('#issue-1').textContent, /Previous resolution by human/);
    assert.match(d.querySelector('#issue-1').textContent, /Awaiting decision/);
    assert.ok(button(d, 'Mark resolved'));
    submit(win, fill(win, d, 'resolution', 'Second verification')); await settle();
    assert.equal(calls.at(-1).path, '/api/issues/1/resolve');
    assert.match(d.querySelector('#issue-1').textContent, /Second verification/);
    assert.equal(button(d, 'Mark resolved'), undefined);
  } finally { dom.window.close(); }
});


test('linked unresolved issues keep read threads visible without duplicating decision cards', async () => {
  const data = issue(); data.links[0].needs_human = false; data.links[1].needs_human = true;
  const { dom, d, win } = await setup({ issues: [data], hash: '' });
  try {
    assert.ok(d.querySelector('li[data-thread="10"]'));
    assert.equal(d.querySelector('li[data-thread="10"] .dot'), null, 'a shared discussion alone is neither processing nor complete');
    assert.ok(d.querySelector('#thread-issues a[href="#issue-1"]'), 'the unresolved issue remains reachable');
    assert.equal(d.querySelector('#needs-you'), null);
    d.getElementById('tab-issues').click(); await settle();
    d.getElementById('tab-threads').click(); await settle();
    assert.ok(d.querySelector('li[data-thread="10"]'));
    assert.equal(d.querySelectorAll('.pane-side [data-issue="1"]').length, 1);
  } finally { dom.window.close(); }
});


test('older pending issues outside the recent list surface once and open from Needs you', async () => {
  const data = issue(9);
  const { dom, d, win } = await setup({ issues: [], pendingIssues: [data] });
  try {
    assert.equal(d.querySelectorAll('.pane-side [data-issue="9"]').length, 1);
    assert.ok(d.querySelector('#thread-issues a[href="#issue-9"]'));
    d.querySelector('.pane-side a[href="#issue-9"]').click(); await settle();
    assert.ok(d.getElementById('issue-9'));
    assert.ok(d.querySelector('#issue-9 a[href="#post-100"]'));
  } finally { dom.window.close(); }
  const duplicate = await setup({ issues: [data], pendingIssues: [data] });
  try { assert.equal(duplicate.d.querySelectorAll('.pane-side [data-issue="9"]').length, 1); }
  finally { duplicate.dom.window.close(); }
});

test('a deep link fetches an older resolved issue outside both snapshots', async () => {
  const data = issue(7); data.status = 'resolved'; data.needs_human = false;
  const { dom, d, calls } = await setup({ issues: [], hash: '#issue-7', reply: () => ok(data) });
  try {
    assert.ok(d.getElementById('issue-7'));
    assert.deepEqual(calls[0], { path: '/api/issues/7', method: 'GET', body: undefined });
    assert.match(d.getElementById('issue-7').textContent, /Resolved/);
  } finally { dom.window.close(); }
});

const structured = () => ({ ...issue(), question_version: 3, decision_question: {
  question: 'May we update the detector?', context: 'The detector is blocking both reviews.',
  options: [{ id: 'wait', label: 'Keep the current detector', description: 'The reviews will wait.', outcome: 'declined' },
    { id: 'fix', label: 'Update the detector', description: 'Allow the proposed detector fix.', outcome: 'approved' }],
  recommended_option_id: 'fix'
} });
test('structured question leads with recommendation, native radios and explicit scoped submission', async () => {
  const data = structured();
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    assert.equal(d.querySelector('#issue-1 h2').textContent, data.decision_question.question);
    const radios = [...d.querySelectorAll('[name^="issue-answer-"]')];
    assert.deepEqual(radios.map(r => r.value), ['option:fix', 'option:wait', 'custom']);
    assert.equal(radios.some(r => r.checked), false);
    assert.match(radios[0].closest('label').textContent, /Recommended/);
    assert.match(radios[0].closest('label').textContent, /Records approval/);
    radios[0].focus(); radios[0].click();
    assert.equal(d.activeElement, radios[0]); assert.equal(calls.length, 0);
    assert.equal(d.querySelector('[name="decision"]').required, false);
    assert.deepEqual(checkedScopes(d), [10, 20]);
    scope(d, 20).click();
    submit(win, send(d)); await settle();
    assert.deepEqual(calls[0].body, { selected_option_id: 'fix', thread_ids: [10], expected_question_version: 3 });
  } finally { dom.window.close(); }
});
test('custom option retains independent answer and outcome through failed submission', async () => {
  const { dom, d, win, calls } = await setup({ issues: [structured()], hash: '#issue-1',
    reply: () => ({ ok: false, status: 409, json: async () => ({ message: 'Question changed; review it again.' }) }) });
  try {
    d.querySelector('[value="custom"]').click();
    assert.equal(d.querySelector('[name="decision"]').required, true);
    fill(win, d, 'decision', 'Proceed only after another review'); fill(win, d, 'outcome', 'answered');
    scope(d, 10).click(); submit(win, send(d)); await settle();
    assert.deepEqual(calls[0].body, { body: 'Proceed only after another review', outcome: 'answered', thread_ids: [20], expected_question_version: 3 });
    assert.equal(d.querySelector('[name="decision"]').value, 'Proceed only after another review');
    assert.equal(d.querySelector('[value="custom"]').checked, true);
    assert.match(d.querySelector('[role="alert"]').textContent, /Question changed/);
  } finally { dom.window.close(); }
});
test('new question version clears stale option selection and preserves custom draft', async () => {
  const data = structured();
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1', reply: () => {
    data.question_version++; data.decision_question.question = 'May we update only the tests?';
    return { ok: false, status: 409, json: async () => ({ message: 'Question changed' }) };
  } });
  try {
    d.querySelector('[value="custom"]').click(); fill(win, d, 'decision', 'My draft');
    d.querySelector('[value="option:fix"]').click(); scope(d, 10).click();
    submit(win, send(d)); await settle();
    assert.equal(d.querySelectorAll('[name^="issue-answer-"]:checked').length, 0);
    assert.equal(d.querySelector('[name="decision"]').value, 'My draft');
    assert.equal(d.querySelector('#issue-1 h2').textContent, 'May we update only the tests?');
    submit(win, send(d)); await settle();
    assert.equal(calls.length, 1);
  } finally { dom.window.close(); }
});
test('legacy issues offer an honest free-text fallback without fabricated presets', async () => {
  const { dom, d } = await setup({ hash: '#issue-1' });
  try {
    assert.match(d.querySelector('#issue-1 h2').textContent, /^How would you like to handle/);
    assert.equal(d.querySelector('[name^="issue-answer-"]'), null);
    assert.match(d.querySelector('.issue-answer').textContent, /No suggested options were provided/);
    assert.equal(d.querySelector('[name="decision"]').required, true);
  } finally { dom.window.close(); }
});

test('Needs you question opens its decision directly', async () => {
  const { dom, d } = await setup({ issues: [structured()] });
  try {
    const link = d.querySelector('.pane-side [data-issue="1"] a');
    assert.equal(link.textContent, 'May we update the detector?');
    link.click();
    assert.equal(d.querySelector('#issue-1 h2').textContent, link.textContent);
    assert.ok(send(d));
  } finally { dom.window.close(); }
});

test('legacy reopened issues lead with the latest request and retain original context below', async () => {
  const data = issue();
  data.comments = [
    { id: 1, kind: 'request', body: 'Earlier request', agent: 'codex', created_at: at },
    { id: 2, kind: 'request', body: 'Can we retry after the new failure?', agent: 'codex', created_at: at },
    { id: 3, kind: 'comment', body: 'Routine follow-up', agent: 'codex', created_at: at }
  ];
  const { dom, d } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    assert.equal(d.querySelector('#issue-1 h2 + .issue-discussion').textContent, 'Can we retry after the new failure?');
    assert.ok([...d.querySelectorAll('#issue-1 .issue-discussion')].some(n => n.textContent === 'Shared evidence'));
  } finally { dom.window.close(); }
  const question = structured(); question.comments = data.comments;
  const current = await setup({ issues: [question], hash: '#issue-1' });
  try {
    assert.equal(current.d.querySelector('#issue-1 h2 + .issue-discussion').textContent, question.decision_question.context);
  } finally { current.dom.window.close(); }
});

test('search finds and opens older issues outside snapshots without losing focus', async () => {
  const old = issue(999); old.needs_human = false;
  const { dom, d, win, calls } = await setup({ issues: [], hash: '#issues', reply: () => ok([old]) });
  try {
    const input = d.querySelector('[type="search"]'); input.focus(); input.value = '/repo/one';
    input.dispatchEvent(new win.Event('input')); await settle();
    assert.equal(calls[0].path, '/api/issues?query=%2Frepo%2Fone');
    assert.equal(d.activeElement, input);
    assert.equal(d.querySelectorAll('.issue-row').length, 1);
    d.querySelector('.issue-row').click();
    assert.ok(d.getElementById('issue-999'));
    assert.ok(d.querySelector('#issue-999 a[href="#post-100"]'));
  } finally { dom.window.close(); }
});

test('search ignores stale responses and clearing restores recent issues', async () => {
  const pending = new Map();
  const { dom, d, win } = await setup({ hash: '#issues', reply: url => new Promise(resolve => pending.set(url, resolve)) });
  try {
    const input = d.querySelector('[type="search"]');
    input.value = 'first'; input.dispatchEvent(new win.Event('input'));
    input.value = 'second'; input.dispatchEvent(new win.Event('input'));
    pending.get('/api/issues?query=second')(ok([issue(2)])); await settle();
    pending.get('/api/issues?query=first')(ok([issue(3)])); await settle();
    assert.match(d.querySelector('.issue-row').textContent, /^#2 /);
    input.value = ''; input.dispatchEvent(new win.Event('input'));
    assert.match(d.querySelector('.issue-row').textContent, /^#1 /);
  } finally { dom.window.close(); }
});

test('search failure is not presented as no matching issues', async () => {
  const { dom, d, win } = await setup({ hash: '#issues', reply: () => ({ ok: false, status: 503, json: async () => ({ message: 'Unavailable' }) }) });
  try {
    const input = d.querySelector('[type="search"]'); input.value = 'blocked'; input.dispatchEvent(new win.Event('input')); await settle();
    assert.match(d.querySelector('#issue-results').textContent, /Search failed/);
    assert.doesNotMatch(d.querySelector('#issue-results').textContent, /No matching/);
  } finally { dom.window.close(); }
});

test('every resolution remains visible after repeated reopen and resolve cycles', async () => {
  const data = issue(); data.status = 'resolved'; data.needs_human = false;
  data.resolutions = [
    { id: 10, body: 'First verification', agent: 'human', created_at: at },
    { id: 20, body: 'Second verification', agent: 'human', created_at: at }
  ];
  data.resolution = data.resolutions[1];
  const { dom, d } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    assert.equal(d.querySelectorAll('[data-resolution]').length, 2);
    assert.match(d.querySelector('[data-resolution="10"]').textContent, /Previous resolution by human.*First verification/);
    assert.match(d.querySelector('[data-resolution="20"]').textContent, /Resolved by human.*Second verification/);
  } finally { dom.window.close(); }
});

test('thread Needs you renders linked questions with scoped explicit submission and source history', async () => {
  const data = structured(); data.links.forEach(l => { l.needs_human = true; });
  const { dom, d, win, calls } = await setup({ issues: [data] });
  try {
    const aside = d.querySelector('aside#needs-you');
    assert.equal(aside.querySelector('h3').textContent, data.decision_question.question);
    assert.match(aside.textContent, /The detector is blocking both reviews/);
    assert.equal(aside.querySelectorAll('[data-issue="1"]').length, 1);
    assert.deepEqual([...aside.querySelectorAll('[name^="issue-answer-"]')].map(r => r.value), ['option:fix', 'option:wait', 'custom']);
    assert.ok(aside.querySelector('a[href="#post-100"]'));
    assert.ok(aside.querySelector('a[href="#post-200"]'));
    aside.querySelector('[value="option:fix"]').click();
    assert.equal(calls.length, 0);
    assert.deepEqual(checkedScopes(aside), [10, 20]);   // both threads are awaiting a decision
    scope(aside, 10).click();
    submit(win, aside.querySelector('button[type="submit"]')); await settle();
    assert.deepEqual(calls[0].body, { selected_option_id: 'fix', thread_ids: [20], expected_question_version: 3 });
    assert.equal(calls[0].path, '/api/issues/1/decisions');
  } finally { dom.window.close(); }
});

test('thread answer errors remain visible with custom text and scope retained', async () => {
  const { dom, d, win, calls } = await setup({ issues: [structured()],
    reply: () => ({ ok: false, status: 409, json: async () => ({ message: 'Question changed; review again.' }) }) });
  try {
    const aside = () => d.querySelector('#needs-you');
    submit(win, aside().querySelector('button[type="submit"]')); await settle();
    assert.match(aside().querySelector('[role="alert"]').textContent, /Choose an option/);
    aside().querySelector('[value="custom"]').click();
    fill(win, aside(), 'decision', 'Only after review');
    scope(aside(), 20).click();
    submit(win, aside().querySelector('button[type="submit"]')); await settle();
    assert.match(aside().querySelector('[role="alert"]').textContent, /Question changed/);
    assert.equal(scope(aside(), 20).checked, false);
    assert.equal(aside().querySelector('[name="decision"]').value, 'Only after review');
    assert.equal(aside().querySelector('[value="custom"]').checked, true);
    assert.equal(aside().querySelector('[type="checkbox"][value="10"]').checked, true);
    assert.equal(calls.length, 1);
  } finally { dom.window.close(); }
});

test('thread questions respect pending thread scope and older pending snapshots', async () => {
  const data = structured(); data.links[0].needs_human = false; data.links[1].needs_human = true;
  const answered = await setup({ issues: [data] });
  try { assert.equal(answered.d.querySelector('#needs-you'), null); }
  finally { answered.dom.window.close(); }
  data.links[0].needs_human = true;
  const older = await setup({ issues: [], pendingIssues: [data] });
  try { assert.equal(older.d.querySelectorAll('#needs-you [data-issue="1"]').length, 1); }
  finally { older.dom.window.close(); }
});

test('multiple thread questions keep radio choices independent', async () => {
  const first = structured(), second = { ...structured(), id: 2 };
  const { dom, d } = await setup({ issues: [first, second] });
  try {
    const a = d.querySelector('#needs-you [data-issue="1"] [value="option:fix"]');
    const b = d.querySelector('#needs-you [data-issue="2"] [value="option:wait"]');
    a.click(); b.click();
    assert.equal(a.checked, true); assert.equal(b.checked, true);
    assert.match(d.querySelector('#needs-you h2').textContent, /Needs you \(2\)/);
  } finally { dom.window.close(); }
});

// ------------------------------------------------------------------ redesigned Issues tab and decision panel
const ago = mins => new Date(Date.now() - mins * 60000).toISOString();
const noDialogs = win => { for (const k of ['alert', 'confirm', 'prompt']) win[k] = () => { throw new Error(k + ' called'); }; };

test('decision scope defaults to the threads awaiting a decision, else every linked thread', async () => {
  const data = structured(); data.links[0].needs_human = false; data.links[1].needs_human = true;
  const flagged = await setup({ issues: [data], hash: '#issue-1' });
  try { assert.deepEqual(checkedScopes(flagged.d), [20]); assert.match(send(flagged.d).getAttribute('aria-describedby'), /decide-hint/); }
  finally { flagged.dom.window.close(); }
  const none = await setup({ issues: [structured()], hash: '#issue-1' });
  try { assert.deepEqual(checkedScopes(none.d), [10, 20]); }
  finally { none.dom.window.close(); }
});

test('the send button names its effect and scope, and stays disabled until the answer is complete', async () => {
  const { dom, d, win, calls } = await setup({ issues: [structured()], hash: '#issue-1' });
  try {
    assert.equal(send(d).textContent, 'Send answer'); assert.equal(send(d).disabled, true);
    assert.match(d.getElementById(send(d).getAttribute('aria-describedby')).textContent, /Choose an option/);
    d.querySelector('[value="option:fix"]').click();
    assert.equal(send(d).textContent, 'Approve for 2 threads'); assert.equal(send(d).disabled, false);
    scope(d, 20).click();
    assert.equal(send(d).textContent, 'Approve for 1 thread');
    d.querySelector('[value="option:wait"]').click();
    assert.equal(send(d).textContent, 'Decline for 1 thread');
    d.querySelector('[value="custom"]').click();
    assert.equal(d.querySelector('.decision .custom').hidden, false);
    assert.equal(send(d).disabled, true);   // no text yet
    fill(win, d, 'decision', 'Only the detector');
    assert.equal(send(d).textContent, 'Send answer to 1 thread');
    fill(win, d, 'outcome', 'declined'); assert.equal(send(d).textContent, 'Decline for 1 thread');
    fill(win, d, 'outcome', 'approved'); assert.equal(send(d).textContent, 'Approve for 1 thread');
    assert.equal(calls.length, 0);
  } finally { dom.window.close(); }
});

test('the outcome is a segmented radio control, not a select', async () => {
  const { dom, d } = await setup({ hash: '#issue-1' });
  try {
    assert.equal(d.querySelector('.decision select'), null);
    const radios = [...d.querySelectorAll('.decision .segmented input[type="radio"][name="outcome"]')];
    assert.deepEqual(radios.map(r => r.value), ['answered', 'approved', 'declined']);
    assert.deepEqual(radios.map(r => r.closest('label').textContent), ['Answer', 'Approve', 'Decline']);
    assert.equal(radios[0].checked, true);
    radios[2].click();
    assert.equal(radios[2].checked, true); assert.equal(radios[0].checked, false);
    // Composer kinds are a segmented control too.
    assert.deepEqual([...d.querySelectorAll('.composer input[name="kind"]')].map(r => r.value), ['comment', 'evidence', 'proposal', 'request']);
  } finally { dom.window.close(); }
});

test('validation is inline and never opens a dialog', async () => {
  const { dom, d, win, calls } = await setup({ issues: [structured()], hash: '#issue-1' });
  try {
    noDialogs(win);
    submit(win, send(d)); await settle();
    assert.match(d.querySelector('#issue-1 [role="alert"]').textContent, /Choose an option or write your own answer/);
    d.querySelector('[value="custom"]').click();
    submit(win, send(d)); await settle();
    assert.match(d.querySelector('#issue-1 [role="alert"]').textContent, /Write your answer/);
    fill(win, d, 'decision', 'ok'); scope(d, 10).click(); scope(d, 20).click();
    submit(win, send(d)); await settle();
    assert.match(d.querySelector('#issue-1 [role="alert"]').textContent, /Select the threads/);
    submit(win, d.querySelector('.composer textarea')); await settle();
    assert.match(d.querySelector('[data-note="comment-1"]').textContent, /Write something/);
    assert.equal(calls.length, 0);
  } finally { dom.window.close(); }
});

test('issue list: awaiting first then recent activity, with status dots and project names', async () => {
  const resolved = { ...issue(1), status: 'resolved', needs_human: false, updated_at: ago(1) };
  const answered = { ...issue(2), needs_human: false, updated_at: ago(5), decisions: [{ id: 9, outcome: 'approved', body: 'ok', agent: 'human', thread_ids: [10], created_at: ago(5) }] };
  const waiting = { ...issue(3), updated_at: ago(60) };
  const waitingNew = { ...issue(4), updated_at: ago(30) };
  const { dom, d, win } = await setup({ issues: [resolved, answered, waiting, waitingNew], hash: '#issues' });
  try {
    const order = () => [...d.querySelectorAll('.issue-row')].map(r => Number(r.dataset.issue));
    assert.deepEqual(order(), [4, 3, 2, 1]);
    const dot = id => d.querySelector(`.issue-row[data-issue="${id}"] .dot`);
    assert.equal(dot(4).className, 'dot stalled'); assert.equal(dot(2).className, 'dot answered'); assert.equal(dot(1).className, 'dot done');
    assert.match(dot(2).getAttribute('aria-label'), /Answered/);
    assert.match(d.querySelector('.issue-row[data-issue="4"] .sub').textContent, /2 threads· one, two/);
    assert.equal(d.querySelector('.issue-row[data-selected="true"]').dataset.issue, '4');   // the first one is shown
    assert.match(d.getElementById('tab-issues').textContent, /2 awaiting/);
    const sort = d.getElementById('issue-sort'); sort.value = 'activity'; sort.dispatchEvent(new win.Event('change'));
    assert.deepEqual(order(), [1, 2, 4, 3]);
    assert.equal(win.localStorage.getItem('agent-comms-issue-sort'), 'activity');
    // Arrow keys move the selection like the thread list.
    d.querySelector('.issue-list ul.rows').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    assert.equal(d.querySelector('.issue-row[data-selected="true"]').dataset.issue, '2');
    assert.equal(win.location.hash, '#issue-2');
  } finally { dom.window.close(); }
});

test('activity merges comments, decisions and resolutions in order', async () => {
  const data = issue();
  data.comments = [{ id: 1, kind: 'created', body: 'Shared evidence', agent: 'codex', created_at: ago(30) },
    { id: 4, kind: 'request', body: 'Again?', agent: 'codex', created_at: ago(10) },
    { id: 2, kind: 'evidence', body: 'Log line', agent: 'claude', created_at: ago(25) }];
  data.decisions = [{ id: 3, outcome: 'declined', body: 'Not yet', agent: 'human', thread_ids: [10, 20], scope: [], created_at: ago(20) }];
  data.resolutions = [{ id: 5, body: 'Verified', agent: 'human', created_at: ago(5) }];
  const { dom, d } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    const items = [...d.querySelectorAll('#issue-1 .timeline > li')];
    assert.deepEqual(items.map(li => li.className), ['k-created', 'k-evidence', 'k-decision', 'k-request', 'k-resolution']);
    assert.match(items[2].textContent, /Decision.*Declined.*human.*Not yet.*Applies to #10, #20/);
    assert.ok(items[2].querySelector('a[href="#thread-20"]'));
    assert.match(items[4].textContent, /Previous resolution by human/);
    // A "created" event carries the original report, so the header does not repeat it.
    assert.equal(d.querySelector('#issue-1 .original'), null);
  } finally { dom.window.close(); }
});

test('search shows loading, empty and result states in the list', async () => {
  const pending = new Map();
  const { dom, d, win } = await setup({ hash: '#issues', reply: url => new Promise(resolve => pending.set(url, resolve)) });
  try {
    const input = d.querySelector('.issue-list input[type="search"]');
    input.value = 'nothing'; input.dispatchEvent(new win.Event('input'));
    assert.match(d.querySelector('#issue-results [role="status"]').textContent, /Searching issues/);
    pending.get('/api/issues?query=nothing')(ok([])); await settle();
    assert.match(d.querySelector('#issue-results').textContent, /No matching issues/);
  } finally { dom.window.close(); }
  const empty = await setup({ issues: [], hash: '#issues' });
  try { assert.match(empty.d.querySelector('#issue-results').textContent, /No shared issues yet/); }
  finally { empty.dom.window.close(); }
});

test('link-a-post opens the Issues tab with a banner, and can be cancelled', async () => {
  const { dom, d } = await setup();
  try {
    d.querySelector('[data-link-issue="100"]').click(); await settle();
    assert.equal(d.getElementById('tab-issues').getAttribute('aria-selected'), 'true');
    const banner = d.getElementById('link-banner');
    assert.match(banner.textContent, /Linking post #100 from thread #10/);
    assert.ok(banner.querySelector('form [name="title"]'));        // nothing chosen yet: offer to create one
    assert.equal(d.querySelector('[data-link-to]'), null);
    d.querySelector('.issue-row').click();
    assert.ok(d.querySelector('[data-link-to="1"]'));
    assert.equal(d.querySelector('#link-banner form'), null);       // collapsed once an issue is chosen
    button(d, 'Create a new issue instead').click();
    assert.ok(d.querySelector('#link-banner form [name="title"]'));
    button(d, 'Cancel linking').click();
    assert.equal(d.getElementById('link-banner'), null);
  } finally { dom.window.close(); }
});

test('tabs switch the list in place, keep the thread selection, and are remembered', async () => {
  const { dom, d, win } = await setup();
  try {
    assert.equal(d.getElementById('issues-link'), null);
    assert.equal(d.getElementById('tab-threads').getAttribute('aria-selected'), 'true');
    assert.ok(d.querySelector('li[data-thread="10"][data-selected="true"]'));
    d.getElementById('tab-issues').click(); await settle();
    assert.ok(d.querySelector('.issue-list')); assert.ok(d.querySelector('.pane-thread #issue-1'));
    assert.ok(d.querySelector('.pane-side'));                    // the sidebar stays
    assert.equal(win.localStorage.getItem('agent-comms-tab'), 'issues');
    assert.equal(win.location.hash, '#issues');
    d.getElementById('tab-threads').click(); await settle();
    assert.ok(d.querySelector('li[data-thread="10"][data-selected="true"]'));
    assert.equal(win.location.hash, '#thread-10');
  } finally { dom.window.close(); }
  const again = await setup({ hash: '', storage: { 'agent-comms-tab': 'issues' } });
  try { assert.equal(again.d.getElementById('tab-issues').getAttribute('aria-selected'), 'true'); }
  finally { again.dom.window.close(); }
});

test('thread and issue link to each other across the tabs', async () => {
  const data = structured(); data.links.forEach(l => { l.needs_human = true; });
  const { dom, d, win } = await setup({ issues: [data] });
  try {
    d.querySelector('#needs-you [data-open-issue="1"]').click(); await settle();
    assert.equal(d.getElementById('tab-issues').getAttribute('aria-selected'), 'true');
    assert.ok(d.getElementById('issue-1')); assert.equal(win.location.hash, '#issue-1');
    d.querySelector('#issue-1-threads a[href="#thread-10"]').click(); await settle();
    assert.equal(d.getElementById('tab-threads').getAttribute('aria-selected'), 'true');
    assert.ok(d.querySelector('li[data-thread="10"][data-selected="true"]'));
    assert.equal(win.location.hash, '#thread-10');
  } finally { dom.window.close(); }
  const deep = await setup({ issues: [data], hash: '#issue-1', storage: { 'agent-comms-tab': 'threads' } });
  try { assert.equal(deep.d.getElementById('tab-issues').getAttribute('aria-selected'), 'true'); assert.ok(deep.d.getElementById('issue-1')); }
  finally { deep.dom.window.close(); }
});

test('a decision from the thread card reports its result there', async () => {
  const data = structured(); data.links.forEach(l => { l.needs_human = true; });
  const { dom, d, win } = await setup({ issues: [data], reply: (url, body) => {
    data.needs_human = false; data.links.forEach(l => { l.needs_human = false; });
    data.decisions.push({ id: 7, outcome: 'approved', body: 'Update', agent: 'human', thread_ids: body.thread_ids, created_at: at });
    return ok(data);
  } });
  try {
    noDialogs(win);
    d.querySelector('#needs-you [value="option:fix"]').click();
    assert.equal(send(d).textContent, 'Approve for 2 threads');
    submit(win, send(d)); await settle();
    assert.match(d.getElementById('needs-you-result').textContent, /Issue #1: Approved for #10, #20/);
  } finally { dom.window.close(); }
});

// ------------------------------------------------------------------ answered vs waiting: the status banner
test('an answered issue says so at the top, with the answer, and Change your answer reopens the decision', async () => {
  const data = structured(); data.needs_human = false; data.links.forEach(l => { l.needs_human = false; });
  data.decisions = [{ id: 7, outcome: 'approved', body: 'Approved\nonly for the detector ' + poison, agent: 'human', thread_ids: [10],
    created_at: ago(37) }];
  const { dom, d, win, calls } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    noDialogs(win);
    const banner = d.querySelector('#issue-1 [data-banner]');
    assert.equal(d.querySelector('#issue-1').firstElementChild, banner, 'at the top');
    assert.equal(banner.dataset.banner, 'answered');
    assert.equal(banner.querySelector('.sb-title').textContent, 'You answered 37m ago — Approved · applies to #10');
    assert.equal(banner.querySelector('.sb-first').textContent, 'Approved');
    assert.match(banner.textContent, /Nothing is waiting on you here/);
    assert.equal(d.querySelector('#issue-1 .decision-card'), null, 'no amber decision card when nothing is asked');
    assert.equal(send(d), null, 'the decision panel stays folded');
    assert.ok(![...d.querySelectorAll('#issue-1 summary')].some(s => /Answer again/.test(s.textContent)));
    const toggle = banner.querySelector('[data-change-answer]');
    assert.equal(toggle.textContent, 'Change your answer');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    toggle.click();
    assert.ok(d.getElementById('issue-1-change'));
    assert.ok(send(d), 'the decision panel opens');
    assert.deepEqual([...d.querySelectorAll('#issue-1-change [name^="issue-answer-"]')].map(r => r.value), ['option:fix', 'option:wait', 'custom']);
    assert.deepEqual([...d.querySelectorAll('#issue-1-change .radio-card .badge')].map(b => b.textContent), ['Recommended', 'Alternative']);
    assert.equal(d.querySelector('#issue-1 [data-change-answer]').textContent, 'Keep my answer');
    assert.equal(calls.length, 0);
    assert.equal(d.querySelector('img'), null); assert.equal(win.pwned, undefined);
  } finally { dom.window.close(); }
});

test('an issue awaiting the human says so at the top and names the threads it blocks', async () => {
  const data = structured(); data.links[0].needs_human = true; data.links[1].needs_human = false;
  const { dom, d } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    const banner = d.querySelector('#issue-1 [data-banner]');
    assert.equal(banner.dataset.banner, 'waiting');
    assert.equal(banner.querySelector('.sb-title').textContent, 'Waiting on your decision');
    assert.equal(banner.querySelector('.sb-sub').textContent, 'Issue #1 · blocks thread #10');
    assert.equal(d.querySelector('#issue-1 .decision-card .card-description').textContent, 'Issue #1 · blocks thread #10');
    assert.equal(d.querySelector('#issue-1 [data-change-answer]'), null);
    // The thread card uses the same component, with the same context line.
    d.getElementById('tab-threads').click(); await settle();
    assert.equal(d.querySelector('#needs-you [data-issue="1"] .ask-context .where').textContent, 'Issue #1 · blocks thread #10');
    assert.equal(d.querySelector('#needs-you [data-issue="1"] .ask-label').textContent, 'Question');
  } finally { dom.window.close(); }
});

test('an open issue with no request does not look like it needs the human', async () => {
  const data = issue(); data.needs_human = false; data.links.forEach(l => { l.needs_human = false; });
  const { dom, d } = await setup({ issues: [data], hash: '#issue-1' });
  try {
    const banner = d.querySelector('#issue-1 [data-banner]');
    assert.equal(banner.dataset.banner, 'idle');
    assert.equal(banner.querySelector('.sb-title').textContent, 'Nothing is waiting on you');
    assert.equal(banner.querySelector('[data-change-answer]').textContent, 'Answer anyway');
    assert.equal(d.querySelector('#issue-1 .decision-card'), null);
    assert.equal(d.querySelector('.issue-row[data-issue="1"] .dot').className, 'dot answered');
    assert.doesNotMatch(d.getElementById('tab-issues').textContent, /awaiting/);
    d.getElementById('tab-threads').click(); await settle();
    assert.equal(d.querySelector('#needs-you'), null);
    assert.match(d.querySelector('.pane-side').textContent, /Needs you \(0\)/);
  } finally { dom.window.close(); }
});
