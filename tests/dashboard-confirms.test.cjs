// Requires jsdom >=23. Run with jsdom available: node --test tests/dashboard-confirms.test.cjs
// Every action happens on click, except Pause agents and the browser sign-outs, which take a second click: the
// first turns the button into "Confirm …?" for 4 seconds. Never window.confirm/alert/prompt: the human's embedded
// browser blocks them, which would make those buttons do nothing.
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

function page({ url = 'http://127.0.0.1:8787/' } = {}) {
  const calls = [], dialogs = [];
  const t = { id: 1, title: 'T', project: '/repo', status: 'open', agent_posts_since_human: 0, thread_cap: 12,
    pinned_summary: null, task_counts: { working: 1 }, created_at: new Date().toISOString(),
    tasks: [{ id: 4, title: 'Task', status: 'working', lease_state: 'active', lease_seconds_left: 900, owner_agent: 'codex',
              owner_session: 1, intends_files: [], events: [] }],
    posts: [post(1, { sealed: true }), post(2, { type: 'decision', decision_status: 'proposed' })] };
  const browsers = { session_days: 30, session_max_days: 90, sessions: [
    { id: 'aaaaaaaaaaaaaaaa', label: 'Safari', created_at: '2027-01-15T08:00:00+00:00', last_seen: '2027-01-15T08:00:00+00:00',
      expires_at: '2027-02-14T08:00:00+00:00', current: true }] };
  const dom = new JSDOM(html, { url, runScripts: 'dangerously', pretendToBeVisual: true, beforeParse(win) {
    for (const name of ['confirm', 'alert', 'prompt']) win[name] = text => { dialogs.push([name, text]); return false; };
    win.fetch = async (u, opts = {}) => {
      if (u === '/api/whoami') return ok({ name: 'human', is_human: true });
      if (u.startsWith('/api/state')) return ok({ me: { name: 'human', is_human: true }, paused: false, threads: [t],
        sessions: [], needs_you: [], limits: {}, authorization_grants: [], task_categories: [], active_runs: [], agents: [] });
      if (u === '/api/settings') return ok({ settings: [], files: { writable: true }, agents: [], audit: [] });
      if (u === '/api/admin/notifications') return ok({ deliverable: false, rules: [] });
      if (u === '/api/admin/dispatch') return ok({ status: { running: false }, rules: [], runs: [], runners: {}, threads: [], agents: [] });
      if (u === '/api/web-sessions') return ok(browsers);
      calls.push(`${opts.method} ${u}`);
      return ok({});
    };
  } });
  const d = dom.window.document;
  const button = label => [...d.querySelectorAll('button')].find(b => b.textContent === label);
  return { dom, win: dom.window, d, calls, dialogs, button };
}

test('unseal, finalize and release act on click; pause takes a second click and never calls window.confirm', async () => {
  const { dom, calls, dialogs, button } = page();
  await settle();
  for (const label of ['Unseal', 'Finalize', 'Release']) { button(label).click(); await settle(20); }
  assert.deepEqual(calls, ['POST /api/posts/1/unseal', 'POST /api/posts/2/finalize', 'POST /api/tasks/4/release']);
  button('Pause agents').click(); await settle(20);
  assert.ok(!calls.includes('POST /api/admin/pause'), 'the first click only arms the button');
  const armed = button('Confirm pause?');
  assert.ok(armed, 'the button now asks to confirm');
  assert.ok(armed.classList.contains('btn-confirm'), 'in the destructive style');
  assert.match(dom.window.document.getElementById('confirm-live').textContent, /again within 4 seconds/);
  assert.equal(dom.window.document.getElementById('confirm-live').getAttribute('aria-live'), 'polite');
  await settle(520);   // the confirming click must come at least 500 ms after arming
  armed.click(); await settle(20);
  assert.equal(calls.filter(c => c === 'POST /api/admin/pause').length, 1, 'the second click pauses');
  assert.deepEqual(dialogs, [], 'no window.confirm/alert/prompt');
  dom.window.close();
});

test('an armed confirm resets after 4 seconds, on Escape and on blur, and survives a re-render', async () => {
  const { dom, win, d, calls, button } = page();
  await settle();
  // Escape
  button('Pause agents').click();
  assert.ok(button('Confirm pause?'));
  button('Confirm pause?').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.ok(button('Pause agents') && !button('Confirm pause?'), 'Escape disarms');
  assert.equal(d.getElementById('confirm-live').textContent, '');
  // Blur (keyboard: focus the button, arm it with a click, then move away)
  button('Pause agents').focus();
  button('Pause agents').click();
  assert.equal(d.activeElement.textContent, 'Confirm pause?');
  win.refresh(); await settle(20);   // a re-render keeps it armed and focused
  assert.equal(d.activeElement.textContent, 'Confirm pause?', 'focus and the armed state survive a re-render');
  d.getElementById('sign-out').focus();
  assert.ok(button('Pause agents') && !button('Confirm pause?'), 'leaving the button disarms');
  // Timeout
  button('Pause agents').click();
  await settle(4100);
  assert.ok(button('Pause agents') && !button('Confirm pause?'), 'it resets after 4 seconds');
  button('Pause agents').click(); await settle(20);
  assert.ok(!calls.includes('POST /api/admin/pause'), 'a click after the reset arms again; nothing was paused');
  dom.window.close();
});

test('revoke a browser session and sign out all browsers take a second click', async () => {
  const { dom, d, calls, dialogs, button } = page({ url: 'http://127.0.0.1:8787/#settings' });
  await settle();
  const revoke = d.querySelector('#settings-browsers tr[data-session="aaaaaaaaaaaaaaaa"] button');
  revoke.click(); await settle(20);
  assert.deepEqual(calls, [], 'the first click only arms Revoke');
  const armed = d.querySelector('#settings-browsers tr[data-session="aaaaaaaaaaaaaaaa"] button');
  assert.equal(armed.textContent, 'Confirm revoke?');
  await settle(520); armed.click(); await settle(40);
  assert.deepEqual(calls, ['POST /api/web-sessions/aaaaaaaaaaaaaaaa/revoke']);
  d.getElementById('sign-out-all').click(); await settle(20);
  assert.equal(d.getElementById('sign-out-all').textContent, 'Confirm sign out?');
  assert.equal(calls.length, 1);
  await settle(520); d.getElementById("sign-out-all").click(); await settle(40);
  assert.deepEqual(calls, ['POST /api/web-sessions/aaaaaaaaaaaaaaaa/revoke', 'POST /api/web-sessions/revoke-all']);
  assert.ok(button('Pause agents'));
  assert.deepEqual(dialogs, [], 'no window.confirm/alert/prompt');
  dom.window.close();
});

test('the page has no window.confirm, alert or prompt calls left', () => {
  assert.doesNotMatch(html, /\b(?:window\.)?(?:confirm|alert|prompt)\(/);
});


test('a double-click or a fast second click never arms and confirms in one go', async () => {
  const { dom, win, calls, button } = page();
  await settle();
  const pause = button('Pause agents');
  pause.dispatchEvent(new win.MouseEvent('click', { bubbles: true, detail: 1 }));
  button('Confirm pause?').dispatchEvent(new win.MouseEvent('click', { bubbles: true, detail: 2 }));   // the dblclick's 2nd click
  assert.ok(!calls.includes('POST /api/admin/pause'), 'a double-click does not pause');
  button('Confirm pause?').click();                                                                   // < 500 ms after arming
  assert.ok(!calls.includes('POST /api/admin/pause'), 'nor does a second click right away');
  const held = new win.KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true, cancelable: true });
  button('Confirm pause?').dispatchEvent(held);
  assert.ok(held.defaultPrevented, 'a held Enter is swallowed, so key repeat cannot confirm');
  await settle(520);
  button('Confirm pause?').click(); await settle(20);
  assert.equal(calls.filter(c => c === 'POST /api/admin/pause').length, 1, 'a deliberate second click still pauses');
  dom.window.close();
});
