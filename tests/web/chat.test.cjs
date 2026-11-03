const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=path.resolve(__dirname,'../../src/greatminds/web/assets');
const html=fs.readFileSync(path.join(assets,'index.html'),'utf8');
const script=fs.readFileSync(path.join(assets,'app.js'),'utf8');
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
async function fixture(t,{theme,systemDark=false,reduced=false,capabilities=false,sharedBindings=false,scale,dashboard,older=false}={}){
 const dom=new JSDOM(html,{url:'http://localhost:8767',runScripts:'outside-only',pretendToBeVisual:true});
 t.after(()=>dom.window.close());const w=dom.window;w.structuredClone=structuredClone;w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 w.matchMedia=q=>({matches:q.includes('reduced-motion')?reduced:systemDark});
 if(scale)w.localStorage.setItem('greatminds-scale',scale);
 if(dashboard)w.localStorage.setItem('greatminds-dashboard',dashboard);
 if(theme)w.localStorage.setItem('greatminds-theme',theme);
 const intervals=[];w.setInterval=cb=>{intervals.push(cb);return intervals.length;};
 const frames=new Map();let next=0;w.requestAnimationFrame=cb=>{frames.set(++next,cb);return next;};w.cancelAnimationFrame=id=>frames.delete(id);
 const doc={id:'c1',closed:false,turns:{t1:{id:'t1',sequence:1,prompt:'Первое сообщение',queued_at:1,status:'completed'},t2:{id:'t2',sequence:2,prompt:'Второе сообщение',queued_at:2,status:'running'}}};
 const events=[{sequence:1,turn_id:'t1',kind:'text',text:'Первый ответ'}];
 const data={app_version:'9.8.7',project:'/tmp/test',name:'test',tasks:[],runs:[],permissions:[],events:[],agents:[],paused:false,bindings:[{id:'planner',role:'ARCHITECT-PLANNER',agent:'fixture',scheduling:'on-demand'}],conversations:[{id:'c1',binding_id:'planner',turn_count:2}],daemon:{running:true,managed:true}};
 const posts=[],gets=[],runHistory=[];
 const runDetail={run:{id:'r1',role:'DEVELOPER',agent_id:'fixture',state:'running'},activity:{events:[],cursor:0,discarded_through:0},commands:[],results:[],events:[]};
 w.fetch=async(url,options={})=>{
  let result;if(options.method==='POST'){posts.push({url,body:JSON.parse(options.body)});result={id:'c1'};}
  else if(url==='/api/settings')result={document:{version:1,agents:{fixture:{argv:['fixture-acp']}},bindings:{planner:{role:'ARCHITECT-PLANNER',agent:'fixture'},...(sharedBindings?{developer:{role:'DEVELOPER',agent:'fixture'}}:{})}},roles:['ARCHITECT-PLANNER'],text:'{}',revision:'v1'};
  else if(url==='/api/state')result=data;
  else if(url.startsWith('/api/runs?')){const before=Number(new URL(url,w.location).searchParams.get('before'));const rows=runHistory.filter(r=>r.sequence<before);const page=rows.slice(0,100);result={runs:page,before:page.at(-1)?.sequence,has_more:rows.length>100};}
  else if(url.startsWith('/api/runs/r1?')){const after=Number(new URL(url,w.location).searchParams.get('after'));result={...runDetail,activity:{...runDetail.activity,events:runDetail.activity.events.filter(e=>e.sequence>after)}};}
  else if(url==='/api/conversations/c1')result=doc;
  else if(url.startsWith('/api/conversations/c1/updates')){
   const query=new URL(url,w.location).searchParams;
   if(query.has('before'))result={...doc,outputs:{t1:'Stale overlap'},events:[],cursor:1,has_more:false,before:1,has_older:false};
   else if(query.has('after')){const after=Number(query.get('after'));result={...doc,events:events.filter(e=>e.sequence>after),cursor:events.at(-1)?.sequence||0,has_more:false};}
   else{const outputs={};for(const e of events)if(e.kind==='text')outputs[e.turn_id]=(outputs[e.turn_id]||'')+e.text;result={...doc,outputs,events:[],cursor:events.at(-1)?.sequence||0,has_more:false,before:older?2:1,has_older:older};}
  }
  else if(url.startsWith('/api/conversations/c1/events?')){const after=Number(new URL(url,w.location).searchParams.get('after'));result={events:events.filter(e=>e.sequence>after),cursor:events.at(-1)?.sequence||0,has_more:false};}
  else throw new Error('Unexpected API: '+url);
  if(url==='/api/executor-options'&&capabilities){const body=JSON.parse(options.body);result={model:{current:body.model||'small',options:[{value:'small',name:'Small'},{value:'large',name:'Large'}]},reasoning:body.model==='large'?{current:'high',options:[{value:'high',name:'High'}]}:null};}
  if(options.method!=='POST')gets.push({url,headers:options.headers});
  const etag=url==='/api/state'?JSON.stringify(data):null;
  if(etag&&options.headers?.['If-None-Match']===etag)return {status:304,ok:false,json:async()=>{throw new Error('304 has no body');}};
  return {ok:true,headers:{get:()=>etag},json:async()=>JSON.parse(JSON.stringify(result))};
 };
 for(const name of ['marked','purify'])w.eval(fs.readFileSync(path.join(assets,name+'.js'),'utf8'));
 w.eval(['markdown','i18n'].map(name=>fs.readFileSync(path.join(assets,name+'.js'),'utf8')).join('\n')+'\n'+script+'\n'+fs.readFileSync(path.join(assets,'stands.js'),'utf8'));await flush();w.document.querySelector('[data-binding="planner"]').click();await flush();
 return {w,doc,events,data,posts,gets,runHistory,runDetail,intervals,async poll(){await intervals[0]();await flush();},frame(time){const current=[...frames.values()];frames.clear();for(const cb of current)cb(time);}};
}
test('typing and polling preserve history nodes, draft, selection and scroll',async t=>{
 const f=await fixture(t);const d=f.w.document;const input=d.getElementById('message-input');const messages=d.getElementById('messages');const old=d.querySelector('.answer').firstChild;
 Object.defineProperties(messages,{scrollHeight:{value:2000,configurable:true},clientHeight:{value:300,configurable:true}});messages.scrollTop=200;
 input.value='Черновик, который я набираю';input.focus();input.setSelectionRange(2,9);input.dispatchEvent(new f.w.Event('input',{bubbles:true}));
 for(let i=0;i<4;i++)await f.poll();
 assert.equal(d.getElementById('message-input'),input);assert.equal(d.getElementById('messages'),messages);assert.equal(d.querySelector('.answer').firstChild,old);
 assert.equal(input.selectionStart,2);assert.equal(input.selectionEnd,9);assert.equal(input.value,'Черновик, который я набираю');assert.equal(d.activeElement,input);assert.equal(messages.scrollTop,200);
 assert.equal(old.textContent.trimEnd(),'Первый ответ');assert.equal(d.querySelectorAll('.chat-turn').length,2);
 f.data.bindings[0].agent='another-agent';await f.poll();assert.match(d.querySelector('.chat-head .row-main small').textContent,/another-agent/);assert.equal(d.getElementById('message-input'),input);
});
test('Enter sends once; Shift+Enter, composition and key-repeat do not send',async t=>{
 const f=await fixture(t);const input=f.w.document.getElementById('message-input');input.value='Две\nстроки';
 for(const extras of [{shiftKey:true},{isComposing:true},{repeat:true}]){
  const e=new f.w.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true,...extras});input.dispatchEvent(e);assert.equal(e.defaultPrevented,false);
 }
 assert.equal(f.posts.length,0);
 const e=new f.w.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true});input.dispatchEvent(e);await flush();
 assert.equal(e.defaultPrevented,true);assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.message,'Две\nстроки');assert.equal(input.value,'');
});
test('new output is appended smoothly, historical text stays stable, activity ends',async t=>{
 const f=await fixture(t);const d=f.w.document;const answer=d.querySelector('[data-turn="t2"] .answer');let node;
 assert.equal(d.querySelector('[data-turn="t2"] .agent-activity').classList.contains('hidden'),false);
 const output='Плавный ответ 😀 без обрыва символов.';f.events.push({sequence:2,turn_id:'t2',kind:'text',text:output});await f.poll();
 f.frame(0);f.frame(140);assert.ok(answer.textContent.length>0&&answer.textContent.length<output.length);assert.ok(output.startsWith(answer.textContent.trimEnd()));
 f.frame(300);assert.equal(answer.textContent.trimEnd(),output);node=answer.firstChild;
 await f.poll();assert.equal(answer.textContent.trimEnd(),output);assert.equal(answer.firstChild,node);
 f.doc.turns.t2.status='completed';await f.poll();assert.ok(d.querySelector('[data-turn="t2"] .agent-activity').classList.contains('hidden'));
 assert.equal(d.querySelector('[data-turn="t1"] .answer').textContent.trimEnd(),'Первый ответ');
});
test('reduced motion shows complete text immediately',async t=>{
 const f=await fixture(t,{reduced:true});f.events.push({sequence:2,turn_id:'t2',kind:'text',text:'Ответ без анимации'});await f.poll();
 assert.equal(f.w.document.querySelector('[data-turn="t2"] .answer').textContent.trimEnd(),'Ответ без анимации');
});
test('theme follows system once, toggles and persists across page loads',async t=>{
 const f=await fixture(t,{systemDark:true});assert.equal(f.w.document.documentElement.dataset.theme,'dark');
 f.w.document.getElementById('theme-button').click();assert.equal(f.w.document.documentElement.dataset.theme,'light');assert.equal(f.w.localStorage.getItem('greatminds-theme'),'light');
 const next=await fixture(t,{systemDark:true,theme:f.w.localStorage.getItem('greatminds-theme')});assert.equal(next.w.document.documentElement.dataset.theme,'light');
 next.w.document.getElementById('theme-button').click();assert.equal(next.w.document.documentElement.dataset.theme,'dark');
});
test('Markdown renders GFM tables and code, sanitizes HTML and keeps completed blocks',async t=>{
 const f=await fixture(t,{reduced:true});const d=f.w.document;
 f.events.push({sequence:2,turn_id:'t2',kind:'text',text:'**Bold** and `code`\n\n| Model | Use |\n| --- | --- |\n| X | Y |\n\n```py\nprint("ok")\n```\n\n<script>alert(1)</script><img src=x onerror="alert(2)">[bad](javascript:alert(3))'});await f.poll();
 const answer=d.querySelector('[data-turn="t2"] .answer');assert.equal(answer.querySelector('strong').textContent,'Bold');assert.equal(answer.querySelector('table td').textContent,'X');assert.match(answer.querySelector('pre code').textContent,/print/);assert.equal(answer.querySelector('script,img,[onerror],a[href^="javascript:"]'),null);
 const paragraph=answer.querySelector('p');f.events.push({sequence:3,turn_id:'t2',kind:'text',text:'\n\nNext paragraph'});await f.poll();assert.equal(answer.querySelector('p'),paragraph);
});
test('English is default; Russian and Chinese switch without translating agent text or losing draft',async t=>{
 const f=await fixture(t);const d=f.w.document;
 assert.equal(d.documentElement.lang,'en');assert.equal(d.querySelector('.chat-head strong').textContent,'Planner');
 const input=d.getElementById('message-input');input.value='Unsent draft';input.dispatchEvent(new f.w.Event('input',{bubbles:true}));
 const language=d.getElementById('language-select');language.value='ru';language.dispatchEvent(new f.w.Event('change',{bubbles:true}));await flush();
 assert.equal(d.documentElement.lang,'ru');assert.equal(d.querySelector('.chat-head strong').textContent,'Планировщик');assert.equal(d.getElementById('message-input').value,'Unsent draft');assert.equal(d.querySelector('.answer').textContent.trim(),'Первый ответ');
 language.value='zh';language.dispatchEvent(new f.w.Event('change',{bubbles:true}));await flush();assert.equal(d.documentElement.lang,'zh');assert.equal(d.querySelector('.chat-head strong').textContent,'规划师');assert.equal(d.getElementById('message-input').value,'Unsent draft');assert.equal(f.w.localStorage.getItem('greatminds-language'),'zh');assert.equal(d.querySelector('.answer').textContent.trim(),'Первый ответ');
});
test('ACP model combobox refreshes model-specific reasoning and saves selected values',async t=>{
 const f=await fixture(t,{capabilities:true});const d=f.w.document;
 d.getElementById('settings-button').click();await flush();
 assert.equal(f.posts.filter(p=>p.url==='/api/executor-options').length,1,'settings load advertised choices automatically');
 let model=d.querySelector('[data-field=model]');assert.equal(model.tagName,'SELECT');assert.match(model.textContent,/Small.*Large/);assert.ok(d.querySelector('.reasoning-field').classList.contains('hidden'));
 model.value='large';model.dispatchEvent(new f.w.Event('change',{bubbles:true}));await flush();
 const reasoning=d.querySelector('[data-field=reasoning]');assert.equal(reasoning.tagName,'SELECT');assert.ok(!reasoning.closest('label').classList.contains('hidden'));assert.match(reasoning.textContent,/High/);reasoning.value='high';
 d.getElementById('settings-form').dispatchEvent(new f.w.Event('submit',{bubbles:true,cancelable:true}));await flush();
 const saved=f.posts.find(p=>p.url==='/api/settings').body.document.bindings.planner;assert.equal(saved.model,'large');assert.equal(saved.reasoning,'high');
});

test('roles sharing an executor reuse one ACP discovery and the cache when reopening settings',async t=>{
 const f=await fixture(t,{capabilities:true,sharedBindings:true});const d=f.w.document;
 d.getElementById('settings-button').click();await flush();
 assert.equal(d.querySelectorAll('select[data-field=model]').length,2);
 assert.equal(f.posts.filter(p=>p.url==='/api/executor-options').length,1);
 d.getElementById('settings-dialog').close();d.getElementById('settings-button').click();await flush();
 assert.equal(f.posts.filter(p=>p.url==='/api/executor-options').length,1);
 d.querySelector('[data-discover]').click();await flush();
 assert.equal(f.posts.filter(p=>p.url==='/api/executor-options').length,2,'explicit reload refreshes cached options');
});
test('product version comes from the server state',async t=>{
 const f=await fixture(t);assert.equal(f.w.document.getElementById('app-version').textContent,'v9.8.7');
});
test('interface size applies without closing settings or losing a chat draft',async t=>{
 const f=await fixture(t);const d=f.w.document;const input=d.getElementById('message-input');input.value='Keep this draft';input.dispatchEvent(new f.w.Event('input',{bubbles:true}));
 d.getElementById('settings-button').click();await flush();const select=d.getElementById('interface-scale');select.value='large';select.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 assert.equal(d.documentElement.dataset.scale,'large');assert.equal(f.w.localStorage.getItem('greatminds-scale'),'large');assert.equal(d.getElementById('message-input'),input);assert.equal(input.value,'Keep this draft');assert.equal(d.getElementById('settings-dialog').open,true);
});
test('dashboard Kanban persists through polling and shows escaped task cards and empty queues',async t=>{
 const f=await fixture(t);const d=f.w.document;f.data.tasks=[{id:'TASK-1',title:'<script>bad()</script>',queue:'feature_dev'}];await f.poll();
 d.querySelector('[data-view="overview"]').click();await flush();const radio=d.querySelector('[name="dashboard-mode"][value="kanban"]');radio.click();await flush();
 assert.equal(f.w.localStorage.getItem('greatminds-dashboard'),'kanban');assert.equal(d.querySelectorAll('.queue').length,7);assert.equal(d.querySelector('[data-task="TASK-1"] strong').textContent,'<script>bad()</script>');assert.equal(d.querySelector('.queue-board script'),null);
 await f.poll();assert.equal(d.querySelector('[value="kanban"]').checked,true);
 d.querySelector('[name="dashboard-mode"][value="summary"]').click();await flush();assert.ok(d.querySelector('.stats'));assert.equal(d.querySelector('.queue-board'),null);
});

test('saved size and dashboard choices restore on the next page load',async t=>{
 const f=await fixture(t,{scale:'large',dashboard:'kanban'});const d=f.w.document;
 assert.equal(d.documentElement.dataset.scale,'large');d.querySelector('[data-view="overview"]').click();await flush();assert.equal(d.querySelector('[value="kanban"]').checked,true);
 const board=d.querySelector('.queue-board');board.scrollLeft=210;f.data.tasks.push({id:'NEW',queue:'feature_dev',title:'New task'});await f.poll();assert.equal(d.querySelector('.queue-board').scrollLeft,210);
});

test('conditional polling retains cards, chat uses cursors, hidden pages stop polling',async t=>{
 const f=await fixture(t,{dashboard:'kanban'});const d=f.w.document;
 assert.ok(f.gets.some(r=>r.url.includes('/updates')));
 await f.poll();assert.ok(f.gets.some(r=>r.url.includes('/updates?after=1')));
 assert.equal(f.gets.some(r=>r.url.includes('/events?')),false);
 f.data.tasks=[{id:'one',queue:'feature_dev',title:'First'},{id:'two',queue:'feature_dev',title:'Second'}];
 await f.poll();d.querySelector('[data-view="overview"]').click();await flush();
 const first=d.querySelector('[data-task="one"]'),second=d.querySelector('[data-task="two"]');
 await f.poll();assert.equal(d.querySelector('[data-task="one"]'),first);
 assert.ok(f.gets.some(r=>r.url==='/api/state'&&r.headers['If-None-Match']));
 f.data.tasks[0].title='Changed';await f.poll();
 assert.equal(d.querySelector('[data-task="one"]'),first);assert.equal(first.querySelector('strong').textContent,'Changed');
 assert.equal(d.querySelector('[data-task="two"]'),second);
 const before=f.gets.length;Object.defineProperty(d,'hidden',{value:true,configurable:true});
 await f.poll();assert.equal(f.gets.length,before);
 Object.defineProperty(d,'hidden',{value:false,configurable:true});d.dispatchEvent(new f.w.Event('visibilitychange'));await flush();
 assert.ok(f.gets.length>before);
});

test('run pagination does not skip history behind an old active run',async t=>{
 const f=await fixture(t);const d=f.w.document;
 for(let sequence=260;sequence>0;sequence--)f.runHistory.push({id:'run-'+sequence,sequence,role:'DEVELOPER',agent_id:'fixture',state:sequence===1?'running':'completed'});
 f.data.runs=[...f.runHistory.slice(0,100),f.runHistory.at(-1)];f.data.run_count=260;f.data.runs_before=161;
 await f.poll();d.querySelector('[data-view="runs"]').click();await flush();
 assert.equal(d.querySelectorAll('.run-item').length,101);
 d.querySelector('[data-action="older-runs"]').click();await flush();
 assert.equal(d.querySelectorAll('.run-item').length,201);
 d.querySelector('[data-action="older-runs"]').click();await flush();
 assert.equal(d.querySelectorAll('.run-item').length,260);
 assert.equal(d.querySelector('[data-action="older-runs"]'),null);
});

test('older chat windows do not overwrite already streamed overlapping turns',async t=>{
 const f=await fixture(t,{older:true,reduced:true});const d=f.w.document;
 const answer=d.querySelector('[data-turn="t1"] .answer');
 f.events.push({sequence:2,turn_id:'t1',kind:'text',text:' new text'});await f.poll();
 const text=answer.textContent;
 d.querySelector('[data-action="older-chat"]').click();await flush();
 assert.equal(answer.textContent,text);assert.equal(d.querySelector('[data-action="older-chat"]'),null);
});

test('selected run loads new activity after a 304 while preserving expanded details',async t=>{
 const f=await fixture(t);const d=f.w.document;
 f.data.runs=[{...f.runDetail.run,sequence:1}];await f.poll();
 d.querySelector('[data-view="runs"]').click();await flush();d.querySelector('[data-run="r1"]').click();await flush();
 const metadata=d.getElementById('run-metadata');metadata.open=true;
 f.runDetail.activity.events.push({sequence:1,kind:'message',text:'Incremental output',at:1});f.runDetail.activity.cursor=1;
 await f.poll();assert.equal(d.getElementById('run-metadata'),metadata);assert.equal(metadata.open,true);
 assert.match(d.getElementById('run-detail').textContent,/Incremental output/);
 await f.poll();assert.ok(f.gets.some(r=>r.url==='/api/runs/r1?after=1'));
 assert.equal(d.querySelectorAll('#run-detail .message-text').length,1);
});

test('older snapshot output is not replayed by the next incremental poll',async t=>{
 const f=await fixture(t,{older:true,reduced:true});const d=f.w.document;const fetch=f.w.fetch;
 f.doc.turns.early={id:'early',sequence:0,prompt:'Older queued request',status:'completed'};
 f.events.push({sequence:2,turn_id:'early',kind:'text',text:'Exactly once'});
 f.w.fetch=async(url,opts)=>url.includes('/updates?before=')?{ok:true,json:async()=>({id:'c1',turns:{early:f.doc.turns.early},outputs:{early:'Exactly once'},cursor:2,snapshot_cursor:2,before:1,has_older:false})}:fetch(url,opts);
 d.querySelector('[data-action="older-chat"]').click();await flush();await f.poll();
 assert.equal(d.querySelector('[data-turn="early"] .answer').textContent.trim(),'Exactly once');
});

for(const added of [5,250])test(`run pagination stays complete after ${added} new executions`,async t=>{
 const f=await fixture(t);const d=f.w.document;
 const row=sequence=>({id:'run-'+sequence,sequence,role:'DEVELOPER',agent_id:'fixture',state:'completed'});
 for(let sequence=260;sequence>0;sequence--)f.runHistory.push(row(sequence));
 const sync=()=>{f.data.runs=f.runHistory.slice(0,100);f.data.run_count=f.runHistory.length;f.data.runs_before=f.data.runs.at(-1).sequence;};
 sync();await f.poll();d.querySelector('[data-view="runs"]').click();await flush();
 d.querySelector('[data-action="older-runs"]').click();await flush();
 f.runHistory.unshift(...Array.from({length:added},(_,i)=>row(260+added-i)));sync();await f.poll();
 assert.equal(d.querySelectorAll('.run-item').length,200+Math.min(added,100),'already loaded runs must remain visible');
 for(let i=0;i<8&&d.querySelector('[data-action="older-runs"]');i++){d.querySelector('[data-action="older-runs"]').click();await flush();}
 assert.equal(d.querySelectorAll('.run-item').length,260+added);
 assert.equal(d.querySelector('[data-action="older-runs"]'),null);
});

test('late incremental metadata cannot roll a newer historical snapshot backwards',async t=>{
 const f=await fixture(t,{older:true,reduced:true});const d=f.w.document;const original=f.w.fetch;let release,started;
 const requested=new Promise(resolve=>started=resolve);
 f.w.fetch=async(url,opts)=>{
  if(url.includes('/updates?after=')){started();return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({id:'c1',closed:false,turns:{early:{id:'early',sequence:0,prompt:'Earlier',status:'running'}},events:[{sequence:2,turn_id:'early',kind:'text',text:'Complete'}],cursor:2,snapshot_cursor:2,has_more:false})});});}
  if(url.includes('/updates?before='))return {ok:true,json:async()=>({id:'c1',turns:{early:{id:'early',sequence:0,prompt:'Earlier',status:'completed'}},outputs:{early:'Complete'},cursor:3,snapshot_cursor:3,before:1,has_older:false})};
  return original(url,opts);
 };
 const poll=f.poll();await requested;
 d.querySelector('[data-action="older-chat"]').click();await flush();release();await poll;
 assert.equal(d.querySelector('[data-turn="early"] .turn-status').textContent,'Completed');
 assert.equal(d.querySelector('[data-turn="early"] .answer').textContent.trim(),'Complete');
});

test('inserting a card keeps keyboard focus on the same task',async t=>{
 const f=await fixture(t,{dashboard:'kanban'});const d=f.w.document;
 f.data.tasks=[{id:'b',queue:'feature_dev',title:'Existing task'}];await f.poll();d.querySelector('[data-view="overview"]').click();await flush();
 const card=d.querySelector('[data-task="b"]');card.focus();
 f.data.tasks.unshift({id:'a',queue:'feature_dev',title:'New task'});await f.poll();
 assert.equal(d.querySelector('[data-task="b"]'),card);assert.equal(d.activeElement,card);
 assert.equal(d.activeElement.dataset.task,'b');
});

test('finished active runs outside the recent window update their retained history row',async t=>{
 const f=await fixture(t);const d=f.w.document;
 for(let sequence=260;sequence>0;sequence--)f.runHistory.push({id:sequence===1?'r1':'run-'+sequence,sequence,role:'DEVELOPER',agent_id:'fixture',state:sequence===1?'running':'completed'});
 f.data.runs=[...f.runHistory.slice(0,100),f.runHistory.at(-1)];f.data.run_count=260;f.data.runs_before=161;
 await f.poll();d.querySelector('[data-view="runs"]').click();await flush();d.querySelector('[data-action="older-runs"]').click();await flush();
 f.runDetail.run={...f.runHistory.at(-1),state:'completed'};f.data.runs=f.runHistory.slice(0,100);await f.poll();
 assert.match(d.querySelector('[data-run="r1"]').textContent,/Completed/);
 assert.doesNotMatch(d.querySelector('[data-run="r1"]').textContent,/Running/);
});

test('an older run page arriving after a new window does not discard the intervening gap',async t=>{
 const f=await fixture(t);const d=f.w.document;const row=sequence=>({id:'run-'+sequence,sequence,role:'DEVELOPER',agent_id:'fixture',state:'completed'});
 for(let sequence=260;sequence>0;sequence--)f.runHistory.push(row(sequence));
 const sync=()=>{f.data.runs=f.runHistory.slice(0,100);f.data.run_count=f.runHistory.length;f.data.runs_before=f.data.runs.at(-1).sequence;};
 sync();await f.poll();d.querySelector('[data-view="runs"]').click();await flush();d.querySelector('[data-action="older-runs"]').click();await flush();
 const original=f.w.fetch;let release,started;const requested=new Promise(resolve=>started=resolve);
 f.w.fetch=async(url,opts)=>{const response=await original(url,opts);if(url==='/api/runs?before=61'){started();return new Promise(resolve=>release=()=>resolve(response));}return response;};
 d.querySelector('[data-action="older-runs"]').click();await requested;
 f.runHistory.unshift(...Array.from({length:250},(_,i)=>row(510-i)));sync();await f.poll();release();await flush();
 for(let i=0;i<2;i++){d.querySelector('[data-action="older-runs"]').click();await flush();}
 assert.equal(d.querySelectorAll('.run-item').length,510);assert.equal(d.querySelector('[data-action="older-runs"]'),null);
});

test('an old active run finishing while the first history page loads gets its final state',async t=>{
 const f=await fixture(t);const d=f.w.document;
 for(let sequence=260;sequence>0;sequence--)f.runHistory.push({id:sequence===1?'r1':'run-'+sequence,sequence,role:'DEVELOPER',agent_id:'fixture',state:sequence===1?'running':'completed'});
 f.data.runs=[...f.runHistory.slice(0,100),f.runHistory.at(-1)];f.data.run_count=260;f.data.runs_before=161;
 await f.poll();d.querySelector('[data-view="runs"]').click();await flush();
 const original=f.w.fetch;let release,started;const requested=new Promise(resolve=>started=resolve);
 f.w.fetch=async(url,opts)=>{const response=await original(url,opts);if(url.startsWith('/api/runs?')){started();return new Promise(resolve=>release=()=>resolve(response));}return response;};
 d.querySelector('[data-action="older-runs"]').click();await requested;
 f.runDetail.run={...f.runHistory.at(-1),state:'completed'};f.data.runs=f.runHistory.slice(0,100);await f.poll();
 release();await flush();assert.match(d.querySelector('[data-run="r1"]').textContent,/Completed/);
});
