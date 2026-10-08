const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const html = fs.readFileSync(require('node:path').join(__dirname, '../agent_comms/dashboard.html'), 'utf8');
const settle = () => new Promise(r => setTimeout(r, 60));
async function setup(delivery, configuration) {
  const calls=[];
  const post={id:1, seq:1, thread_id:1, agent:'claude-code', type:'proposal', body:'Apply approved fix', to:[], needs_response:true,
    created_at:new Date().toISOString(), refs:[], requests:[], approval_delivery:delivery};
  const state={me:{name:'human',is_human:true},paused:false,agents:[{name:'human',is_human:1},{name:'claude-code'},{name:'codex'}],
    sessions:[],limits:{body_max_bytes:4096},authorization_grants:[],task_categories:[],active_runs:[],issues:[],needs_you_issues:[],
    needs_you:[post],configuration,threads:[{id:1,title:'Recovery',project:'/repo',status:'open',created_at:post.created_at,
      posts:[post],tasks:[],task_counts:{},agent_posts_since_human:0,thread_cap:1000}]};
  const dom=new JSDOM(html,{url:'http://127.0.0.1:8787/',runScripts:'dangerously',beforeParse(win){
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
