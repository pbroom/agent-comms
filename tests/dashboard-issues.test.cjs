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
async function setup({ issues = [issue()], human = true, hash = '#thread-10', pendingIssues = [], reply } = {}) {
  const calls = [];
  const me = { name: human ? 'human' : 'codex', is_human: human };
  const post = { id: 100, thread_id: 10, seq: 1, agent: 'codex', session_id: 1, type: 'question', body: 'Blocked',
    to: [], needs_response: true, refs: [], sealed: false, created_at: at };
  const thread = { id: 10, title: 'Detector fix', project: '/repo/one', status: 'open', agent_posts_since_human: 1,
    thread_cap: 12, tasks: [], task_counts: {}, posts: [post], created_at: at };
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/' + hash, runScripts: 'dangerously', beforeParse(win) {
    win.HTMLElement.prototype.scrollIntoView = () => {};
    win.fetch = async (u, opts = {}) => {
      if (u === '/api/whoami') return ok(me);
      if (u.startsWith('/api/state')) return ok({ me, issues, needs_you_issues: pendingIssues, threads: [thread], needs_you: [], sessions: [], paused: false,
        authorization_grants: [], task_categories: [], active_runs: [], launchable_agents: [], agents: [], limits: {} });
      calls.push({ path: u, body: opts.body && JSON.parse(opts.body), method: opts.method });
      return reply ? reply(u, calls.at(-1).body) : ok({ id: 1 });
    };
  } });
  await settle();
  return { dom, win: dom.window, d: dom.window.document, calls };
}
function button(d, text) { return [...d.querySelectorAll('button')].find(b => b.textContent === text); }
function fill(win, d, name, value) {
  const node = d.querySelector(`[name="${name}"]`); node.value = value;
  node.dispatchEvent(new win.Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  return node;
}
function submit(win, node) { node.closest('form').dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true })); }

test('Needs you surfaces one issue with source links and renders untrusted content as text', async () => {
  const { dom, d, win } = await setup();
  try {
    assert.equal(d.querySelectorAll('.pane-side [data-issue="1"]').length, 1);
    assert.match(d.querySelector('.pane-side').textContent, /Needs you \(1\)/);
    assert.equal(d.querySelectorAll('#thread-issues a').length, 1);
    d.getElementById('issues-link').click(); await settle();
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
    const search = d.querySelector('[type="search"]'); search.value = 'metadata'; search.dispatchEvent(new win.Event('input'));
    assert.equal(d.querySelectorAll('.issue-row').length, 1);
    assert.equal(calls.length, 0);
    d.querySelector('.issue-row').click();
    assert.equal(calls.length, 0);
    button(d, 'Link this post').click(); await settle();
    assert.deepEqual(calls[0], { path: '/api/issues/1/links', method: 'POST', body: { thread_id: 10, post_id: 100 } });
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
    assert.equal(d.querySelectorAll('fieldset input:checked').length, 0);
    submit(win, fill(win, d, 'decision', 'Approved only for detector work')); await settle();
    assert.equal(calls.length, 0); assert.match(d.querySelector('[role="alert"]').textContent, /Select the threads/);
    fill(win, d, 'outcome', 'approved'); d.querySelector('fieldset input[value="10"]').click();
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
    assert.ok(d.querySelector('tr[data-thread="10"]'));
    assert.match(d.querySelector('tr[data-thread="10"]').textContent + d.querySelector('tr[data-thread="10"]').innerHTML, /Shared issue unresolved/);
    assert.equal(d.querySelector('#needs-you'), null);
    d.getElementById('issues-link').click(); await settle();
    d.getElementById('issues-link').click(); await settle();
    assert.ok(d.querySelector('tr[data-thread="10"]'));
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
    const radios = [...d.querySelectorAll('[name="issue-answer"]')];
    assert.deepEqual(radios.map(r => r.value), ['option:fix', 'option:wait', 'custom']);
    assert.equal(radios.some(r => r.checked), false);
    assert.match(radios[0].closest('label').textContent, /Recommended/);
    assert.match(radios[0].closest('label').textContent, /Records approval/);
    radios[0].focus(); radios[0].click();
    assert.equal(d.activeElement, radios[0]); assert.equal(calls.length, 0);
    assert.equal(d.querySelector('[name="decision"]').required, false);
    assert.equal(d.querySelectorAll('[type="checkbox"]:checked').length, 0);
    d.querySelector('input[value="10"]').click();
    submit(win, button(d, 'Submit answer')); await settle();
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
    d.querySelector('input[value="20"]').click(); submit(win, button(d, 'Submit answer')); await settle();
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
    d.querySelector('[value="option:fix"]').click(); d.querySelector('input[value="10"]').click();
    submit(win, button(d, 'Submit answer')); await settle();
    assert.equal(d.querySelectorAll('[name="issue-answer"]:checked').length, 0);
    assert.equal(d.querySelector('[name="decision"]').value, 'My draft');
    assert.equal(d.querySelector('#issue-1 h2').textContent, 'May we update only the tests?');
    submit(win, button(d, 'Submit answer')); await settle();
    assert.equal(calls.length, 1);
  } finally { dom.window.close(); }
});
test('legacy issues offer an honest free-text fallback without fabricated presets', async () => {
  const { dom, d } = await setup({ hash: '#issue-1' });
  try {
    assert.match(d.querySelector('#issue-1 h2').textContent, /^How would you like to handle/);
    assert.equal(d.querySelector('[name="issue-answer"]'), null);
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
    assert.ok(button(d, 'Submit answer'));
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
