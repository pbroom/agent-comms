const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const html = fs.readFileSync(require('node:path').join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(r => setTimeout(r, 60));
async function setup(delivery, configuration, issue) {
  const calls=[];
  const post={id:1, seq:1, thread_id:1, agent:'claude-code', type:'proposal', body:'Apply approved fix', to:[], needs_response:true,
    created_at:new Date().toISOString(), refs:[], requests:[], approval_delivery:delivery};
  const state={me:{name:'human',is_human:true},paused:false,agents:[{name:'human',is_human:1},{name:'claude-code'},{name:'codex'}],
    sessions:[],limits:{body_max_bytes:4096},authorization_grants:[],task_categories:[],active_runs:[],issues:issue?[issue]:[],needs_you_issues:issue?[issue]:[],
    needs_you:[post],configuration,threads:[{id:1,title:'Recovery',project:'/repo',status:'open',created_at:post.created_at,
      posts:[post],tasks:[],task_counts:{},agent_posts_since_human:0,thread_cap:1000}]};
  const dom=new JSDOM(html,{url:'http://127.0.0.1:8787/'+(issue?'#issue-'+issue.id:''),runScripts:'dangerously',beforeParse(win){
    win.HTMLElement.prototype.scrollIntoView=function(){};
    win.fetch=async (url,opts={})=>{
      let data;
      if(url==='/api/whoami') data=state.me;
      else if(url.startsWith('/api/state')) data=state;
      else {calls.push({url,body:opts.body?JSON.parse(opts.body):null});
        if(url==='/api/configuration/refresh'){state.configuration={state:'current'};data=state.configuration;}
        else data={post_id:2,to:['codex']};}
      return {ok:true,status:200,json:async()=>data};
    };
  }}); await settle(); return {dom, d:dom.window.document,calls};
}
test('recorded implementer is visible and sent instead of proposer',async()=>{
  const {dom,d,calls}=await setup({recipient:'codex',source:'recorded',reason:'Recorded implementer',requires_choice:false});
  try {assert.equal(d.getElementById('ny-assign-1').value,'codex');
    d.querySelector('[data-post="1"] input[value="approve"]').click();
    d.querySelector('[data-primary="1"]').click();await settle();
    assert.equal(calls.find(c=>c.url==='/api/posts/1/resolve').body.delivery_agent,'codex');
  } finally {dom.window.close();}
});
test('ambiguous owner requires choice and never silently selects the first agent',async()=>{
  const {dom,d,calls}=await setup({recipient:null,source:'recorded',reason:'Recorded owners differ; choose who should carry out this approval',requires_choice:true});
  try {d.querySelector('[data-post="1"] input[value="approve"]').click();
    assert.equal(d.getElementById('ny-assign-1').value,'');assert.equal(d.querySelector('[data-primary="1"]').disabled,true);
    const select=d.getElementById('ny-assign-1'); select.value='codex';select.dispatchEvent(new dom.window.Event('change'));
    assert.equal(d.querySelector('[data-primary="1"]').disabled,false);
    d.querySelector('[data-primary="1"]').click();await settle();assert.equal(calls[0].body.delivery_agent,'codex');
  } finally {dom.window.close();}
});
test('stale settings show effective limits and recover through supported refresh',async()=>{
  const {dom,d,calls}=await setup(null,{state:'stale',effective_limits:{daily_post_cap_per_agent:200,max_agent_posts_per_thread_without_human:1000},error:'Invalid saved setting',refresh_supported:true,recovery:'Correct the saved configuration, then refresh.'});
  try {const notice=d.getElementById('configuration-notice');assert.match(notice.textContent,/200 posts per day/);
    notice.querySelector('button').click();await settle();assert.equal(calls[0].url,'/api/configuration/refresh');
    assert.equal(d.getElementById('configuration-notice'),null);
  } finally {dom.window.close();}
});
test('source mismatch asks for reconnect without pretending settings refresh reloads code',async()=>{
  const {dom,d}=await setup(null,{state:'restart_required',runtime_source_changed:true,refresh_supported:false,recovery:'Reconnect this MCP session or restart this board process.',effective_limits:{}});
  try {const notice=d.getElementById('configuration-notice');assert.match(notice.textContent,/needs reconnecting/);assert.equal(notice.querySelector('button'),null);}
  finally {dom.window.close();}
});


test('shared approval requires only ambiguous selected sources and sends exact scoped owners',async()=>{
  const at=new Date().toISOString();
  const issue={id:9,title:'Shared recovery',body:'Scoped approval',status:'open',needs_human:true,created_by:'codex',created_at:at,updated_at:at,comments:[],decisions:[],resolution:null,
    links:[{thread_id:1,post_id:1,title:'First',project:'/repo',needs_human:true,covers_post:true,source_post:{id:1,agent:'claude-code',approval_delivery:{recipient:'codex',requires_choice:false}}},
      {thread_id:2,post_id:2,title:'Second',project:'/repo2',needs_human:true,covers_post:true,source_post:{id:2,agent:'claude-code',approval_delivery:{recipient:null,requires_choice:true}}}]};
  const {dom,d,calls}=await setup(null,null,issue);
  try {
    const form=d.querySelector('[data-decision-form="9"]');assert.ok(form);
    const area=form.querySelector('[name="decision"]');area.value='Approve scoped work';area.dispatchEvent(new dom.window.Event('input'));
    form.querySelector('input[name="outcome"][value="approved"]').click();
    assert.equal(form.querySelector('[data-decide="9"]').disabled,true);
    assert.equal(form.querySelector('.issue-assignments select').value,'codex');
    form.querySelector('.scope input[value="2"]').click();
    assert.equal(form.querySelector('[data-decide="9"]').disabled,false);
    form.dispatchEvent(new dom.window.Event('submit',{cancelable:true,bubbles:true}));await settle();
    const call=calls.find(c=>c.url==='/api/issues/9/decisions');
    assert.deepEqual(call.body.delivery_agents,{'1':'codex'});assert.deepEqual(call.body.thread_ids,[1]);
  } finally {dom.window.close();}
});
