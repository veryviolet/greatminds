// Real Chromium measurements with intercepted fixtures; no HTTP server or executor.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {execFileSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),base='src/greatminds/web/assets/';
const args=process.argv.slice(2),baseline=args.includes('--baseline')?args[args.indexOf('--baseline')+1]:null;
const count=Number(process.env.BENCH_TASKS||1000),history=Number(process.env.BENCH_RUNS||1000);
function asset(name,ref){return ref?execFileSync('git',['show',ref+':'+base+name],{cwd:root}):fs.readFileSync(path.join(root,base,name));}
async function measure(browser,ref){
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const tasks=Array.from({length:count},(_,i)=>({id:String(i+1).padStart(4,'0')+'-task',queue:['feature_plan','feature_dev','feature_test','verified'][i%4],title:'Task '+i}));
 const runs=Array.from({length:history},(_,i)=>({id:'run-'+i,sequence:i+1,role:'DEVELOPER',agent_id:'fixture',binding_id:'developer',task_id:'task-'+i,state:'completed',created_at:i+1})).reverse();
 const doc={id:'chat',closed:false,turns:{}};const events=[];
 for(let i=0;i<100;i++){const id='turn-'+i;doc.turns[id]={id,sequence:i+1,prompt:'Question '+i,status:'completed',queued_at:i+1};events.push({sequence:i+1,turn_id:id,kind:'text',text:('Answer **'+i+'**. A paragraph with `code`.\n\n').repeat(30)});}
 let bytes=0,requests=0;
 const assets=new Map();for(const name of ['index.html','app.js','style.css','i18n.js','stands.js','markdown.js','marked.js','purify.js'])assets.set(name,asset(name,ref));
 await page.route('http://greatminds.test/**',async route=>{
  const url=new URL(route.request().url());let result;
  if(!url.pathname.startsWith('/api/')){const name=url.pathname==='/'?'index.html':url.pathname.slice(1);return route.fulfill({body:assets.get(name)||'',contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});}
  requests++;
  if(url.pathname==='/api/state'){
   result={project:'/fixture',name:'Benchmark',app_version:'benchmark',tasks,runs:ref?runs:runs.slice(0,100),run_count:runs.length,runs_before:runs[Math.min(100,runs.length)-1]?.sequence,run_totals:{completed:runs.length},permissions:[],events:[],agents:[],paused:false,bindings:[{id:'planner',role:'ARCHITECT-PLANNER',agent:'fixture',scheduling:'on-demand'}],conversations:[{id:'chat',binding_id:'planner',turn_count:100}],daemon:{running:true,managed:true}};
   const body=JSON.stringify(result),etag='"'+crypto.createHash('sha256').update(body).digest('hex')+'"';
   if(route.request().headers()['if-none-match']===etag)return route.fulfill({status:304,headers:{etag}});
   bytes+=Buffer.byteLength(body);return route.fulfill({body,contentType:'application/json',headers:{etag}});
  }
  else if(url.pathname==='/api/conversations/chat')result=doc;
  else if(url.pathname.endsWith('/events')){const after=Number(url.searchParams.get('after')||0);result={events:events.filter(e=>e.sequence>after),cursor:events.length,has_more:false};}
  else if(url.pathname.endsWith('/updates')){
   if(url.searchParams.has('after')){const after=Number(url.searchParams.get('after'));result={id:'chat',closed:false,turns:{},events:events.filter(e=>e.sequence>after),cursor:events.length,has_more:false};}
   else{const before=Number(url.searchParams.get('before')||101);const selected=Object.values(doc.turns).filter(t=>t.sequence<before).slice(-50);const ids=new Set(selected.map(t=>t.id));result={id:'chat',closed:false,turns:Object.fromEntries(selected.map(t=>[t.id,t])),outputs:Object.fromEntries(events.filter(e=>ids.has(e.turn_id)).map(e=>[e.turn_id,e.text])),events:[],cursor:events.length,has_more:false,before:selected[0].sequence,has_older:selected[0].sequence>1};}
  }
  else if(url.pathname==='/api/runs'){const rows=runs.filter(r=>r.sequence<Number(url.searchParams.get('before')));const selected=rows.slice(0,100);result={runs:selected,before:selected.at(-1)?.sequence,has_more:rows.length>100};}
  else throw new Error('Unexpected fixture request '+url.pathname);
  const body=JSON.stringify(result);bytes+=Buffer.byteLength(body);return route.fulfill({body,contentType:'application/json'});
 });
 try{
  const start=performance.now();await page.goto('http://greatminds.test/');await page.waitForSelector('.stats');const initial_ms=performance.now()-start;
  const kanban_ms=await page.evaluate(()=>{const start=performance.now();dashboardMode='kanban';navigate('overview');return performance.now()-start;});
  await page.locator('.task-card').first().waitFor();
  const unchanged=[];for(let i=0;i<7;i++)unchanged.push(await page.evaluate(async()=>{while(refreshing)await new Promise(r=>setTimeout(r,5));const start=performance.now();await refresh();return performance.now()-start;}));
  const firstCard=await page.locator('.task-card').first().elementHandle();tasks[0].title='Changed task';
  const changed_ms=await page.evaluate(async()=>{const start=performance.now();await refresh();return performance.now()-start;});
  const card_preserved=await firstCard.evaluate(node=>node.isConnected);
  const run_view_ms=await page.evaluate(()=>{const start=performance.now();navigate('runs');return performance.now()-start;});
  const rendered_runs=await page.locator('.run-item').count();
  const chatStart=performance.now();await page.locator('#role-nav [data-binding=planner]').click();await page.waitForSelector('[data-turn="turn-99"] .answer p');const chat_open_ms=performance.now()-chatStart;
  const chat_idle=[];for(let i=0;i<7;i++)chat_idle.push(await page.evaluate(async()=>{while(chatLoading)await new Promise(r=>setTimeout(r,5));const start=performance.now();await loadChat();return performance.now()-start;}));
  const median=xs=>[...xs].sort((a,b)=>a-b)[Math.floor(xs.length/2)];
  if(!ref){await page.locator('[data-action=older-chat]').click();await page.waitForSelector('[data-turn="turn-0"]');}
  if(errors.length)throw new Error(errors.join('\n'));
  return {source:ref||'working-tree',initial_ms,kanban_ms,unchanged_poll_median_ms:median(unchanged),changed_card_ms:changed_ms,card_preserved,run_view_ms,rendered_runs,chat_open_ms,chat_idle_median_ms:median(chat_idle),api_bytes:bytes,api_requests:requests};
 }finally{await context.close();}
}
(async()=>{const browser=await chromium.launch({headless:true});try{const results=[];if(baseline)results.push(await measure(browser,baseline));results.push(await measure(browser,null));console.log(JSON.stringify({fixture:{tasks:count,runs:history,turns:100},results},null,2));}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
