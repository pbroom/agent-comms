// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-confirms.test.cjs
// Only Pause agents and the browser sign-outs ask first; every other action happens on click (embedded browsers
// block window.confirm, which would otherwise make those buttons do nothing).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const ok = body => ({ ok: true, status: 200, json: async () => body });

const post = (id, extra = {}) => ({ id, seq: id, thread_id: 1, agent: 'codex', session_id: 1, type: 'status', body: 'p' + id,
  to: [], needs_response: false, task_id: null, refs: [], sealed: false, created_at: new Date().toISOString(), ...extra });

test('unseal, finalize and release act on click; pause still asks', async () => {
  const calls = [], asked = [];
  const t = { id: 1, title: 'T', project: '/repo', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, task_counts: { working: 1 }, created_at: new Date().toISOString(),
    tasks: [{ id: 4, title: 'Task', status: 'working', lease_state: 'active', lease_seconds_left: 900, owner_agent: 'codex',
              owner_session: 1, intends_files: [], events: [] }],
    posts: [post(1, { sealed: true }), post(2, { type: 'decision', decision_status: 'proposed' })] };
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8787/', runScripts: 'dangerously', beforeParse(win) {
    win.confirm = text => { asked.push(text); return false; };   // like an embedded browser that blocks dialogs
    win.fetch = async (u, opts = {}) => {
      if (u === '/api/whoami') return ok({ name: 'human', is_human: true });
      if (u.startsWith('/api/state')) return ok({ me: { name: 'human', is_human: true }, paused: false, threads: [t],
        sessions: [], needs_you: [], limits: {}, authorization_grants: [], task_categories: [], active_runs: [], agents: [] });
      calls.push(`${opts.method} ${u}`);
      return ok({});
    };
  } });
  await settle();
  const d = dom.window.document;
  const click = label => [...d.querySelectorAll('button')].find(b => b.textContent === label).click();
  for (const label of ['Unseal', 'Finalize', 'Release']) { click(label); await settle(20); }
  assert.deepEqual(calls, ['POST /api/posts/1/unseal', 'POST /api/posts/2/finalize', 'POST /api/tasks/4/release']);
  assert.deepEqual(asked, [], 'no confirmation for these');
  click('Pause agents'); await settle(20);
  assert.equal(asked.length, 1, 'pausing the board still asks');
  assert.ok(!calls.includes('POST /api/admin/pause'), 'and a blocked or declined confirm does not pause');
  dom.window.close();
});
