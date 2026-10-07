// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-login.test.cjs
// Sign-in: cookie first, a token is never kept in localStorage, and #post-<id> / #thread-<id> deep links.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const LINK = 'http://127.0.0.1:8787/login/' + 'A'.repeat(43);

const ok = body => ({ ok: true, status: 200, json: async () => body });
const fail = (status, message) => ({ ok: false, status, statusText: String(status), json: async () => ({ message }) });

function post(id, threadId, body = 'post ' + id) {
  return { id, seq: id, thread_id: threadId, agent: 'codex', session_id: 1, type: 'status', body, to: [],
    needs_response: false, task_id: null, refs: [], sealed: false, created_at: '2027-01-15T08:00:00+00:00' };
}
function thread(id, posts, status = 'open') {
  return { id, title: 'Thread ' + id, project: '/repo', status, agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, tasks: [], posts };
}
function boardState(threads, me = { name: 'human', is_human: true }) {
  return { me, paused: false, threads, sessions: [], needs_you: [], limits: {}, authorization_grants: [],
    agents: [{ name: 'human', is_human: true }, { name: 'codex', is_human: false }], task_categories: [] };
}

// The page may remember non-secret UI state (selected thread, per-thread "seen" marks), never a token.
const UI_KEYS = new Set(['agent-comms-thread', 'agent-comms-seen']);
function assertNoStoredSecrets(win) {
  for (let i = 0; i < win.localStorage.length; i++) {
    const k = win.localStorage.key(i);
    assert.ok(UI_KEYS.has(k), 'unexpected localStorage key ' + k);
    assert.doesNotMatch(win.localStorage.getItem(k), /ac_|token/i);
  }
}

// routes: (method, url, options) => response | undefined (undefined = 404)
async function setup({ url = 'http://127.0.0.1:8787/', storage = {}, routes }) {
  const calls = [], navigations = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', e => { if (/navigation/i.test(e.message)) navigations.push(e.message); });
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', virtualConsole, beforeParse(win) {
    for (const [k, v] of Object.entries(storage)) win.localStorage.setItem(k, v);
    win.confirm = () => true;
    win.fetch = async (u, options) => {
      calls.push({ url: u, ...options });
      return routes(options.method, u, options) || fail(404, 'not found');
    };
  } });
  await settle();
  return { dom, calls, navigations, win: dom.window, document: dom.window.document };
}

function signedInRoutes(threads, extra = () => undefined) {
  return (method, url, options) => {
    const r = extra(method, url, options);
    if (r) return r;
    if (url === '/api/whoami') return ok({ name: 'human', is_human: true });
    if (url.startsWith('/api/state')) return ok(boardState(typeof threads === 'function' ? threads(url) : threads));
    if (url === '/api/web-sessions/logout') return ok({ signed_out: true });
    return undefined;
  };
}

test('cookie first: whoami with credentials and the CSRF header, no token anywhere', async () => {
  const { dom, calls, document, win } = await setup({ routes: signedInRoutes([thread(3, [post(1, 3)])]) });
  assert.equal(calls[0].url, '/api/whoami');
  for (const c of calls) {
    assert.equal(c.headers['X-Board-Request'], '1');
    assert.equal(c.headers.Authorization, undefined);
    assert.equal(c.credentials, 'same-origin');
  }
  assert.ok(calls.some(c => c.url === '/api/state'));
  assert.ok(document.querySelector('header'), 'signed in');
  assert.equal(document.querySelector('#sign-in'), null);
  assertNoStoredSecrets(win);
  dom.window.close();
});

test('without a session: the sign-in screen explains board dashboard and loads nothing else', async () => {
  const { dom, calls, document } = await setup({
    routes: (m, u) => u === '/api/whoami' ? fail(401, 'not signed in') : undefined });
  const signIn = document.querySelector('#sign-in');
  assert.ok(signIn);
  assert.match(signIn.textContent, /Run board dashboard in a terminal \(or use the menu bar app\) to sign in with one click\./);
  assert.ok(signIn.querySelector('input[type=password]'), 'paste-token fallback is kept');
  assert.deepEqual(calls.map(c => c.url), ['/api/whoami']);
  dom.window.close();
});

test('migration: a stored token is exchanged once for a sign-in link and deleted', async () => {
  const { dom, calls, navigations, win } = await setup({
    url: 'http://127.0.0.1:8787/#post-41',
    storage: { 'agent-comms-token': 'ac_legacy_human' },
    routes: (m, u) => u === '/api/login-links' ? ok({ url: LINK, expires_in_seconds: 60 }) : undefined });
  assert.equal(win.localStorage.getItem('agent-comms-token'), null);
  assertNoStoredSecrets(win);
  const exchange = calls.filter(c => c.url === '/api/login-links');
  assert.equal(exchange.length, 1);
  assert.equal(exchange[0].method, 'POST');
  assert.equal(exchange[0].headers.Authorization, 'Bearer ac_legacy_human');
  assert.deepEqual(JSON.parse(exchange[0].body), { next: '/#post-41' });
  assert.equal(navigations.length, 1, 'follows the sign-in link');
  dom.window.close();
});

test('an old #token= link is exchanged, stripped from the URL and never stored', async () => {
  const { dom, calls, navigations, win } = await setup({
    url: 'http://127.0.0.1:8787/#token=ac_from_fragment',
    routes: (m, u) => u === '/api/login-links' ? ok({ url: LINK, expires_in_seconds: 60 }) : undefined });
  assert.equal(win.location.hash, '');
  assertNoStoredSecrets(win);
  const exchange = calls.find(c => c.url === '/api/login-links');
  assert.equal(exchange.headers.Authorization, 'Bearer ac_from_fragment');
  assert.deepEqual(JSON.parse(exchange.body), { next: '/' });
  assert.equal(navigations.length, 1);
  dom.window.close();
});

test('a link the server returns for another host is not followed', async () => {
  const { dom, navigations, document } = await setup({
    storage: { 'agent-comms-token': 'ac_x' },
    routes: (m, u) => u === '/api/login-links' ? ok({ url: 'https://evil.example/login/abc', expires_in_seconds: 60 })
      : u === '/api/whoami' ? fail(401, 'no') : undefined });
  assert.equal(navigations.length, 0);
  assert.match(document.querySelector('#sign-in').textContent, /unexpected sign-in link/);
  dom.window.close();
});

test('a pasted human token is exchanged for a cookie session, not stored', async () => {
  const { dom, calls, navigations, win, document } = await setup({
    routes: (m, u) => u === '/api/whoami' ? fail(401, 'no')
      : u === '/api/login-links' ? ok({ url: LINK, expires_in_seconds: 60 }) : undefined });
  const input = document.querySelector('#sign-in input');
  input.value = '  ac_pasted  ';
  [...document.querySelectorAll('#sign-in button')].find(b => b.textContent === 'Open board').click();
  await settle();
  const exchange = calls.find(c => c.url === '/api/login-links');
  assert.equal(exchange.headers.Authorization, 'Bearer ac_pasted');
  assert.equal(navigations.length, 1);
  assertNoStoredSecrets(win);
  dom.window.close();
});

test('an agent token (no sign-in link) is used in memory only', async () => {
  const { dom, calls, win, document } = await setup({
    storage: { 'agent-comms-token': 'ac_agent' },
    routes: (m, u) => u === '/api/login-links' ? fail(403, 'only the human can create sign-in links')
      : u.startsWith('/api/state') ? ok(boardState([], { name: 'codex', is_human: false })) : undefined });
  assertNoStoredSecrets(win);
  const st = calls.find(c => c.url === '/api/state');
  assert.equal(st.headers.Authorization, 'Bearer ac_agent');
  assert.match(document.querySelector('header').textContent, /signed in as codex/);
  dom.window.close();
});

test('sign out ends the server session and shows the sign-in screen', async () => {
  const { dom, calls, document } = await setup({ routes: signedInRoutes([]) });
  document.querySelector('#sign-out').click();
  await settle();
  const out = calls.find(c => c.url === '/api/web-sessions/logout');
  assert.equal(out.method, 'POST');
  assert.equal(out.headers['X-Board-Request'], '1');
  assert.ok(document.querySelector('#sign-in'));
  dom.window.close();
});

test('an expired session (401) falls back to the sign-in screen', async () => {
  let n = 0;
  const { dom, document } = await setup({ routes: (m, u) => {
    if (u === '/api/whoami') return ok({ name: 'human', is_human: true });
    if (u.startsWith('/api/state')) return ++n === 1 ? fail(401, 'your sign-in expired') : undefined;
  } });
  assert.ok(document.querySelector('#sign-in'));
  assert.match(document.querySelector('#sign-in').textContent, /sign-in ended/);
  dom.window.close();
});

test('#post-<id> highlights a post in the snapshot', async () => {
  const { dom, document } = await setup({ url: 'http://127.0.0.1:8787/#post-41',
    routes: signedInRoutes([thread(3, [post(40, 3), post(41, 3), post(42, 3)])]) });
  const node = document.getElementById('post-41');
  assert.ok(node);
  assert.ok(node.classList.contains('highlight'));
  assert.equal(document.querySelectorAll('.post.highlight').length, 1);
  dom.window.close();
});

test('#post-<id> outside the snapshot expands its (closed) thread and highlights it', async () => {
  const { dom, calls, document } = await setup({ url: 'http://127.0.0.1:8787/#post-7',
    routes: signedInRoutes(u => u.includes('closed=true') ? [thread(3, [post(90, 3)]), thread(5, [post(91, 5)], 'closed')]
                                                          : [thread(3, [post(90, 3)])],
      (m, u) => u === '/api/posts/7' ? ok(post(7, 5))
        : u.startsWith('/api/threads/5/posts') ? ok({ posts: [post(7, 5), post(8, 5)], more: false }) : undefined) });
  await settle(150);
  assert.ok(calls.some(c => c.url === '/api/posts/7'));
  assert.ok(calls.some(c => c.url.startsWith('/api/threads/5/posts?since_seq=6')));
  assert.ok(calls.some(c => c.url === '/api/state?closed=true'), 'closed threads are shown');
  const node = document.getElementById('post-7');
  assert.ok(node, 'the older post is rendered');
  assert.ok(node.classList.contains('highlight'));
  assert.ok(document.getElementById('thread-5').contains(node));
  assert.ok(document.getElementById('post-91'), 'the snapshot posts are still there');
  dom.window.close();
});

test('#thread-<id> highlights the thread card; an unknown post shows a notice', async () => {
  const a = await setup({ url: 'http://127.0.0.1:8787/#thread-3', routes: signedInRoutes([thread(3, [post(1, 3)])]) });
  assert.ok(a.document.getElementById('thread-3').classList.contains('highlight'));
  a.dom.window.close();
  const b = await setup({ url: 'http://127.0.0.1:8787/#post-999', routes: signedInRoutes([thread(3, [post(1, 3)])]) });
  await settle(150);
  assert.match(b.document.querySelector('.banner').textContent, /Post #999 is not on this board/);
  b.dom.window.close();
});
