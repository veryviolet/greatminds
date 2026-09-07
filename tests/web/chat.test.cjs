const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=path.resolve(__dirname,'../../src/greatminds/web/assets');
const html=fs.readFileSync(path.join(assets,'index.html'),'utf8');
const script=fs.readFileSync(path.join(assets,'app.js'),'utf8');
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
async function fixture(t,{theme,systemDark=false,reduced=false}={}){
 const dom=new JSDOM(html,{url:'http://localhost:8767',runScripts:'outside-only',pretendToBeVisual:true});
 t.after(()=>dom.window.close());const w=dom.window;
 w.matchMedia=q=>({matches:q.includes('reduced-motion')?reduced:systemDark});
 if(theme)w.localStorage.setItem('greatminds-theme',theme);
 const intervals=[];w.setInterval=cb=>{intervals.push(cb);return intervals.length;};
 const frames=new Map();let next=0;w.requestAnimationFrame=cb=>{frames.set(++next,cb);return next;};w.cancelAnimationFrame=id=>frames.delete(id);
 const doc={id:'c1',closed:false,turns:{t1:{id:'t1',sequence:1,prompt:'Первое сообщение',queued_at:1,status:'completed'},t2:{id:'t2',sequence:2,prompt:'Второе сообщение',queued_at:2,status:'running'}}};
 const events=[{sequence:1,turn_id:'t1',kind:'text',text:'Первый ответ'}];
 const data={project:'/tmp/test',name:'test',tasks:[],runs:[],permissions:[],events:[],agents:[],paused:false,bindings:[{id:'planner',role:'ARCHITECT-PLANNER',agent:'fixture',scheduling:'on-demand'}],conversations:[{id:'c1',binding_id:'planner',turn_count:2}],daemon:{running:true,managed:true}};
 const posts=[];
 w.fetch=async(url,options={})=>{
  let result;if(options.method==='POST'){posts.push({url,body:JSON.parse(options.body)});result={id:'c1'};}
  else if(url==='/api/state')result=data;
  else if(url==='/api/conversations/c1')result=doc;
  else if(url.startsWith('/api/conversations/c1/events?')){const after=Number(new URL(url,w.location).searchParams.get('after'));result={events:events.filter(e=>e.sequence>after),cursor:events.at(-1)?.sequence||0,has_more:false};}
  else throw new Error('Unexpected API: '+url);
  return {ok:true,json:async()=>JSON.parse(JSON.stringify(result))};
 };
 w.eval(script);await flush();w.document.querySelector('[data-binding="planner"]').click();await flush();
 return {w,doc,events,data,posts,intervals,async poll(){await intervals[0]();await flush();},frame(time){const current=[...frames.values()];frames.clear();for(const cb of current)cb(time);}};
}
test('typing and polling preserve history nodes, draft, selection and scroll',async t=>{
 const f=await fixture(t);const d=f.w.document;const input=d.getElementById('message-input');const messages=d.getElementById('messages');const old=d.querySelector('.answer').firstChild;
 Object.defineProperties(messages,{scrollHeight:{value:2000,configurable:true},clientHeight:{value:300,configurable:true}});messages.scrollTop=200;
 input.value='Черновик, который я набираю';input.focus();input.setSelectionRange(2,9);input.dispatchEvent(new f.w.Event('input',{bubbles:true}));
 for(let i=0;i<4;i++)await f.poll();
 assert.equal(d.getElementById('message-input'),input);assert.equal(d.getElementById('messages'),messages);assert.equal(d.querySelector('.answer').firstChild,old);
 assert.equal(input.selectionStart,2);assert.equal(input.selectionEnd,9);assert.equal(input.value,'Черновик, который я набираю');assert.equal(d.activeElement,input);assert.equal(messages.scrollTop,200);
 assert.equal(old.data,'Первый ответ');assert.equal(d.querySelectorAll('.chat-turn').length,2);
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
 const f=await fixture(t);const d=f.w.document;const answer=d.querySelector('[data-turn="t2"] .answer');const node=answer.firstChild;
 assert.equal(d.querySelector('[data-turn="t2"] .agent-activity').classList.contains('hidden'),false);
 const output='Плавный ответ 😀 без обрыва символов.';f.events.push({sequence:2,turn_id:'t2',kind:'text',text:output});await f.poll();
 f.frame(0);f.frame(140);assert.ok(answer.textContent.length>0&&answer.textContent.length<output.length);assert.ok(output.startsWith(answer.textContent));
 f.frame(300);assert.equal(answer.textContent,output);assert.equal(answer.firstChild,node);
 await f.poll();assert.equal(answer.textContent,output);assert.equal(answer.firstChild,node);
 f.doc.turns.t2.status='completed';await f.poll();assert.ok(d.querySelector('[data-turn="t2"] .agent-activity').classList.contains('hidden'));
 assert.equal(d.querySelector('[data-turn="t1"] .answer').textContent,'Первый ответ');
});
test('reduced motion shows complete text immediately',async t=>{
 const f=await fixture(t,{reduced:true});f.events.push({sequence:2,turn_id:'t2',kind:'text',text:'Ответ без анимации'});await f.poll();
 assert.equal(f.w.document.querySelector('[data-turn="t2"] .answer').textContent,'Ответ без анимации');
});
test('theme follows system once, toggles and persists across page loads',async t=>{
 const f=await fixture(t,{systemDark:true});assert.equal(f.w.document.documentElement.dataset.theme,'dark');
 f.w.document.getElementById('theme-button').click();assert.equal(f.w.document.documentElement.dataset.theme,'light');assert.equal(f.w.localStorage.getItem('greatminds-theme'),'light');
 const next=await fixture(t,{systemDark:true,theme:f.w.localStorage.getItem('greatminds-theme')});assert.equal(next.w.document.documentElement.dataset.theme,'light');
 next.w.document.getElementById('theme-button').click();assert.equal(next.w.document.documentElement.dataset.theme,'dark');
});
