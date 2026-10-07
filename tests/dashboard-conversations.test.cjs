// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-conversations.test.cjs
// Conversation links: "Open in Claude/ChatGPT" and "Copy resume" for the human, validated again in the page.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });

const CLAUDE = '3f2c8a5e-1b7d-4c9e-a0f1-6d5e4c3b2a19';
const CODEX = '01a11421-ce71-7370-a005-a5179018a42d';
const claudeConv = { app: 'Claude', url: `claude://resume?session=${CLAUDE}`, resume_command: `claude --resume ${CLAUDE}`, cwd: '/work/wt' };
const codexConv = { app: 'ChatGPT', url: `codex://threads/${CODEX}`, resume_command: `codex resume ${CODEX}`, cwd: '/work/repo' };

function session(id, agent, conversation) {
  const s = { id, agent, runtime: agent === 'codex' ? 'codex-cli' : 'claude-code', project: '/work/repo', worktree: null,
    started_at: '2027-01-15T08:00:00+00:00', last_seen: new Date().toISOString() };
  if (conversation !== undefined) s.conversation = conversation;
  return s;
}
function task(id, owner, sid, status, conversation, lease = 'active') {
  return { id, thread_id: 1, title: 'Task ' + id, acceptance: '', status, owner_agent: owner, owner_session: sid,
    lease_state: owner ? lease : 'none', lease_seconds_left: 900, owner_may_work: true, intends_files: [], depends_on: [],
    created_by: 'human', events: [], category: null, authorization: { source: 'human', active: true },
    ...(conversation !== undefined ? { owner_conversation: conversation } : {}) };
}
function stateOf({ human = true, sessions = [], tasks = [] }) {
  return { me: { name: human ? 'human' : 'codex', is_human: human }, paused: false, limits: {}, authorization_grants: [],
    agents: [], task_categories: [], needs_you: [], sessions,
    threads: [{ id: 1, title: 'Thread 1', project: '/work/repo', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
      pinned_summary: null, tasks, task_counts: {}, posts: [], created_at: '2027-01-15T07:00:00+00:00' }] };
}

async function setup(state, { clipboard = true } = {}) {
  const copied = [];
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', beforeParse(win) {
    if (clipboard) Object.defineProperty(win.navigator, 'clipboard', { value: { writeText: async t => { copied.push(t); } } });
    win.fetch = async u => {
      if (u === '/api/whoami') return ok({ name: state.me.name, is_human: state.me.is_human });
      if (u.startsWith('/api/state')) return ok(state);
      return { ok: false, status: 404, statusText: '404', json: async () => ({ message: 'not found' }) };
    };
  } });
  await settle();
  return { dom, win: dom.window, document: dom.window.document, copied };
}
const links = (root) => [...root.querySelectorAll('.convo a')].map(a => [a.textContent, a.getAttribute('href'), a.getAttribute('rel')]);
const sessionsCard = d => [...d.querySelectorAll('.pane-side section')].find(s => s.querySelector('h2').textContent === 'Sessions');

test('sessions panel links each session to its own conversation for the human', async () => {
  const { dom, document } = await setup(stateOf({ sessions: [session(1, 'claude', claudeConv), session(2, 'codex', codexConv),
    session(3, 'grok', null)] }));
  assert.deepEqual(links(sessionsCard(document)), [
    ['Open in Claude', `claude://resume?session=${CLAUDE}`, 'noopener noreferrer'],
    ['Open in ChatGPT', `codex://threads/${CODEX}`, 'noopener noreferrer']]);
  const buttons = [...sessionsCard(document).querySelectorAll('button.copy-resume')];
  assert.deepEqual(buttons.map(b => b.dataset.command), [`claude --resume ${CLAUDE}`, `codex resume ${CODEX}`]);
  assert.match(buttons[0].title, /run it in \/work\/wt/);
  dom.window.close();
});

test('the copy button copies only the resume command', async () => {
  const { dom, document, copied } = await setup(stateOf({ sessions: [session(1, 'claude', { ...claudeConv, cwd: "/x'; rm -rf ~; '" })] }));
  const button = sessionsCard(document).querySelector('button.copy-resume');
  button.click();
  await settle(20);
  assert.deepEqual(copied, [`claude --resume ${CLAUDE}`]);
  assert.equal(button.textContent, 'Copied');
  dom.window.close();
});

test('invalid or unexpected URLs render no link', async () => {
  const bad = [
    { ...claudeConv, url: 'javascript:alert(1)' },
    { ...claudeConv, url: `claude://resume?session=${CLAUDE}&x=1` },
    { ...claudeConv, url: `https://example.com/?session=${CLAUDE}` },
    { ...codexConv, url: `codex://threads/${CODEX.toUpperCase()}` },
    { ...codexConv, url: `codex://threads/${CODEX}/../x` },
    { ...claudeConv, url: ` claude://resume?session=${CLAUDE}` },
    { app: 'Claude', url: 42 }, 'claude://resume', null,
  ];
  const { dom, document } = await setup(stateOf({ sessions: bad.map((c, i) => session(i + 1, 'claude', c)) }));
  assert.equal(document.querySelectorAll('.convo').length, 0);
  assert.equal(document.querySelectorAll('a[href^="javascript"], a[href^="https"]').length, 0);
  dom.window.close();
});

test('the app name comes from the URL, not from the data', async () => {
  const { dom, document } = await setup(stateOf({ sessions: [session(1, 'codex', { ...codexConv, app: 'Totally Safe App' })] }));
  assert.deepEqual(links(document).map(l => l[0]), ['Open in ChatGPT']);
  dom.window.close();
});

test('agent views render no links even if the data had them', async () => {
  const { dom, document } = await setup(stateOf({ human: false, sessions: [session(1, 'claude', claudeConv)],
    tasks: [task(7, 'claude', 1, 'working', claudeConv)] }));
  assert.equal(document.querySelectorAll('.convo, button.copy-resume').length, 0);
  dom.window.close();
});

test('task rows link the owner while it works or holds the lease', async () => {
  const tasks = [task(7, 'claude', 1, 'working', claudeConv), task(8, 'codex', 2, 'blocked', codexConv),
    task(9, 'codex', 2, 'accepted', codexConv, 'expired'), task(10, null, null, 'accepted', null),
    task(11, 'codex', 2, 'done', { ...codexConv, url: 'javascript:alert(1)' }, 'none')];
  const { dom, document } = await setup(stateOf({ tasks }));
  const rows = [...document.querySelectorAll('#thread-pane table tr')].slice(1);
  assert.deepEqual(rows.map(r => r.querySelectorAll('.convo a').length), [1, 1, 0, 0, 0]);
  // A blocked task makes the thread "stalled", not "being worked on": no header list.
  assert.equal(document.getElementById('working-conversations'), null);
  dom.window.close();
});

test('the header of a thread being worked on lists each working conversation once', async () => {
  const tasks = [task(7, 'claude', 1, 'working', claudeConv), task(9, 'claude', 1, 'working', claudeConv),
    task(12, 'codex', 2, 'working', codexConv), task(13, 'grok', 3, 'working', null)];
  const { dom, document } = await setup(stateOf({ tasks }));
  const header = document.getElementById('working-conversations');
  assert.ok(header, 'a thread being worked on lists the working agents');
  assert.deepEqual(links(header).map(l => l[1]), [`claude://resume?session=${CLAUDE}`, `codex://threads/${CODEX}`]);
  assert.match(header.textContent, /Working:\s*claude/);
  dom.window.close();
});

test('a subagent session links its parent conversation, and says so', async () => {
  const parentConv = { ...claudeConv, subagent: true };
  const { dom, document } = await setup(stateOf({ sessions: [session(1, 'claude', parentConv), session(2, 'claude', claudeConv)] }));
  const anchors = [...sessionsCard(document).querySelectorAll('.convo a')];
  assert.deepEqual(anchors.map(a => a.textContent), ['Open parent conversation in Claude', 'Open in Claude']);
  assert.equal(anchors[0].getAttribute('aria-label'),
    'claude: open the parent conversation (this session is one of its subagents) in the Claude app');
  assert.equal(anchors[0].getAttribute('href'), `claude://resume?session=${CLAUDE}`);
  dom.window.close();
  // Only a real `true` is the flag.
  const odd = await setup(stateOf({ sessions: [session(1, 'claude', { ...claudeConv, subagent: 'yes' })] }));
  assert.equal(sessionsCard(odd.document).querySelector('.convo a').textContent, 'Open in Claude');
  odd.dom.window.close();
});

test('the sessions panel keeps the server order (last seen first) and never re-sorts', async () => {
  const at = m => new Date(Date.now() - m * 60000).toISOString();
  // Deliberately not in id, start-time or name order: the page must show exactly the server's order.
  const sessions = [{ ...session(7, 'codex'), last_seen: at(1) }, { ...session(2, 'claude'), last_seen: at(3) },
    { ...session(9, 'grok'), last_seen: at(8) }, { ...session(4, 'claude'), last_seen: at(20) }];
  const { dom, document } = await setup(stateOf({ sessions }));
  assert.deepEqual([...sessionsCard(document).querySelectorAll('[data-session]')].map(n => n.dataset.session),
    ['7', '2', '9', '4']);
  dom.window.close();
});
