'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => esc(JSON.stringify(value, null, 2));
const names = {'ARCHITECT-PLANNER':'Планировщик','LIVE-DEVELOPER':'Разработка в диалоге','DEVELOPER':'Разработчик','TESTER':'Тестировщик','ARCHITECT-REVIEWER':'Ревьюер','UI-DEVELOPER':'UI-разработчик','TECHNICAL-WRITER':'Техписатель','READER':'Читатель','EXPLORER':'Исследователь','MAINTAINER':'Сопровождение'};
const statuses = {running:'Работает',completed:'Завершён',failed:'Ошибка',cancelled:'Отменён',interrupted:'Прерван',waiting_input:'Ждёт решения',waiting_auth:'Нужен вход',starting:'Запускается',claimed:'Назначен',idle:'Готов',paused:'На паузе',queued:'В очереди',succeeded:'Пройдено',received:'Получен',applied:'Применён',rejected:'Отклонён'};
const queues = {feature_plan:'Планирование',feature_dev:'Разработка',feature_test:'Тестирование',feature_review:'Ревью',verified:'Готово',blocked:'Заблокировано',inbox:'Входящие'};
const terminal = new Set(['completed','failed','cancelled','interrupted']);
const state = {data:null, view:'overview', binding:null, conversation:null, run:null, draft:{}, chats:new Map(), filter:'', runFilter:'', busy:false, settings:null};
let toastTimer, refreshing=false, lastMarkup='';
function count(n,forms){const last=n%10;return n+' '+forms[n%100>=11&&n%100<=14?2:last===1?0:last>=2&&last<=4?1:2]}
function short(id){return String(id ?? '').slice(0,8)}
function when(at){return at ? new Date(at*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—'}
function badge(status){return `<span class="pill ${esc(status)}">${esc(statuses[status] || status || '—')}</span>`}
function roleName(role){return names[role] || role}
function initials(role){return (role || 'AG').split('-').map(x=>x[0]).join('').slice(0,2)}
function toast(text){$('toast').textContent=text;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),7000)}
async function api(path, body){
 const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const result=await response.json();if(!response.ok)throw new Error(result.error || `HTTP ${response.status}`);return result;
}
function interactive(){return (state.data?.bindings || []).filter(b=>b.scheduling==='on-demand')}
function binding(){return state.data?.bindings.find(b=>b.id===state.binding)}
function empty(title,description=''){return `<div class="empty"><span class="empty-icon">◇</span><strong>${esc(title)}</strong>${description?`<br>${esc(description)}`:''}</div>`}
function heading(title,description,actions=''){return `<div class="page-heading"><div><span class="eyebrow">YOUR AGENTS. ONE WORKSPACE.</span><h1>${esc(title)}</h1><p class="subtitle">${esc(description)}</p></div>${actions}</div>`}
function renderChrome(){
 const d=state.data; $('project-name').textContent=d.name;$('project-path').textContent=d.project;$('project-path').title=d.project;
 $('task-count').textContent=d.tasks.length;$('run-count').textContent=d.runs.length;
 const title=state.view==='chat'?roleName(binding()?.role):({overview:'Обзор',tasks:'Задачи',runs:'Запуски'}[state.view]);$('page-name').textContent=title || 'Диалог';
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view));
 $('role-nav').innerHTML=interactive().map(b=>`<button class="nav role-button ${state.binding===b.id&&state.view==='chat'?'active':''}" data-binding="${esc(b.id)}"><span>${initials(b.role)}</span>${esc(roleName(b.role))}</button>`).join('') || '<p class="subtitle">Добавьте интерактивные роли в настройках.</p>';
 $('tabs').innerHTML=`<button class="tab ${state.view==='overview'?'active':''}" role="tab" aria-selected="${state.view==='overview'}" data-view="overview">▦ Обзор</button>`+interactive().map(b=>`<button class="tab ${state.binding===b.id&&state.view==='chat'?'active':''}" role="tab" aria-selected="${state.binding===b.id&&state.view==='chat'}" data-binding="${esc(b.id)}">${esc(roleName(b.role))}<small>${esc(b.agent)}</small></button>`).join('');
 $('pause-button').textContent=d.paused?'▷ Продолжить':'Ⅱ Пауза';$('pause-button').disabled=Boolean(d.configuration_error);
 const ds=d.daemon;const db=$('daemon-button');db.textContent=ds.running?(ds.owned?'■ Остановить демон':'● Демон подключён'):(ds.starting?'Запускается…':'▷ Запустить демон');db.disabled=(ds.running&&!ds.owned)||ds.starting||Boolean(d.configuration_error);
 $('connection-dot').classList.toggle('online',ds.running);$('connection-text').textContent=ds.running?'Демон подключён':'Интерфейс подключён · демон остановлен';
 let notice=d.configuration_error?`Настройте исполнителей: ${d.configuration_error}`:ds.exit_code&& !ds.running?`Демон завершился с кодом ${ds.exit_code}. Проверьте конфигурацию и доступность исполнителей.`:ds.restart_required?'Настройки сохранены. Остановите и запустите демон, чтобы применить их.':!ds.running?'Демон остановлен. Сообщения останутся в очереди до его запуска.':d.paused?'Новые запуски на паузе. Уже открытые сессии продолжают работать.':'';
 $('notice').textContent=notice;$('notice').classList.toggle('hidden',!notice);
 $('refresh-time').textContent='Обновлено '+new Date().toLocaleTimeString('ru-RU');
}
function permissionPanel(runIds=null){
 const rows=state.data.permissions.filter(p=>!runIds||runIds.includes(p.run_id));if(!rows.length)return '';
 return `<section class="permissions"><h3>◎ Нужно ваше решение · ${rows.length}</h3>`+rows.map(p=>`<div class="permission"><strong>${esc(p.tool.title || 'Запрос инструмента')}</strong><small class="muted">Запуск ${short(p.run_id)}</small><details id="permission-${esc(p.id)}"><summary>Посмотреть запрос</summary><pre>${pretty(p.tool)}</pre></details><div class="permission-actions">${p.options.filter(o=>['allow_once','reject_once'].includes(o.kind)).map(o=>`<button class="button ${o.kind==='allow_once'?'primary':''}" data-permission="${esc(p.id)}" data-option="${esc(o.optionId)}">${o.kind==='allow_once'?'Разрешить один раз':'Отклонить'}</button>`).join('')}</div></div>`).join('')+'</section>';
}
function overview(){
 const d=state.data;const active=d.runs.filter(r=>!terminal.has(r.state));const done=d.runs.filter(r=>r.state==='completed');
 const stats=[['Активные запуски',active.length,'↗','В работе и ожидают решения'],['Задачи в работе',d.tasks.filter(t=>t.queue!=='verified').length,'☷','В очередях проекта'],['Завершённые запуски',done.length,'✓','За всю сохранённую историю'],['Ждут решения',d.permissions.length,'◎','Разрешения инструментов']];
 return heading('Всё под контролем','Задачи, исполнители и решения — в одном рабочем пространстве.',`<span class="date-label">${new Date().toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'})}</span>`)+permissionPanel()+`<div class="stats">${stats.map(([n,v,s,f])=>`<div class="stat"><div class="stat-top">${n}<span class="stat-symbol">${s}</span></div><div class="stat-value">${v}</div><div class="stat-foot">${f}</div></div>`).join('')}</div><div class="grid"><div><section class="panel"><div class="panel-head"><h3>Команда исполнителей</h3><small>${count(d.bindings.length,['роль','роли','ролей'])} настроено</small></div>${d.agents.map(a=>`<div class="agent-row"><div class="avatar ${a.state==='running'?'green':''}">${initials(a.role)}</div><div class="row-main"><strong>${esc(roleName(a.role))}</strong><small>${esc(a.agent_id)} · ${esc(a.task_id || a.binding_id)}</small></div>${badge(a.state)}${a.run_id?`<button class="text-button" data-run="${esc(a.run_id)}">Открыть ↗</button>`:''}</div>`).join('')||empty('Команда пока не настроена','Откройте настройки и добавьте исполнителей и роли.')}</section><section class="panel"><div class="panel-head"><h3>Последние запуски</h3><button class="text-button" data-view="runs">Все запуски →</button></div>${runTable(d.runs.slice(0,5))}</section></div><section class="panel"><div class="panel-head"><h3>Лента событий</h3><small>Последние 12</small></div>${d.events.filter(e=>!['run_stage_observed','protocol_observed','usage_observed','usage_prompt_finish','queue_observations_changed'].includes(e.kind)).slice(-12).reverse().map(e=>`<div class="feed-item"><i></i><div><strong>${esc(eventName(e.kind))}</strong><small>${when(e.at)}${e.run_id?' · '+short(e.run_id):''}</small></div></div>`).join('')||empty('Пока тихо','Новые события появятся здесь автоматически.')}</section></div>`;
}
function eventName(kind){return ({claimed:'Задача назначена',starting:'Исполнитель запускается',running:'Исполнитель начал работу',completed:'Запуск завершён',failed:'Ошибка запуска',permission_requested:'Запрошено разрешение',permission_answered:'Решение оператора получено',permission_consumed:'Разрешение передано агенту',result_received:'Результат получен',result_applied:'Результат применён',result_applying:'Применяется результат',result_validation_started:'Проверка результата началась',result_validation_completed:'Результат проверен',workspace_ready:'Рабочая директория готова',command_succeeded:'Проверка пройдена',cancelled:'Запуск отменён',paused:'Выдача задач приостановлена',resumed:'Выдача задач продолжена'}[kind]||kind.replaceAll('_',' '))}
function runTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>ИСПОЛНИТЕЛЬ / РОЛЬ</th><th>ЗАДАЧА</th><th>СТАТУС</th><th>ВРЕМЯ</th></tr></thead><tbody>${rows.map(r=>`<tr data-run="${esc(r.id)}" tabindex="0"><td>${esc(r.agent_id)}<br><small class="muted">${esc(roleName(r.role))}</small></td><td class="mono">${esc(r.conversation_id?'Диалог':r.task_id)}</td><td>${badge(r.state)}</td><td class="muted">${when(r.created_at)}</td></tr>`).join('')}</tbody></table></div>`:empty('Запусков пока нет','Запустите задачу или напишите интерактивной роли.')}
function tasks(){
 const rows=state.data.tasks.filter(t=>`${t.id} ${t.title} ${t.queue}`.toLowerCase().includes(state.filter.toLowerCase()));
 const groups=[...new Set(rows.map(t=>t.queue))];
 return heading('Задачи проекта','Очереди и этапы отражают состояние, которое хранит демон.')+`<div class="filter-row"><input id="task-search" class="search" placeholder="Поиск по задачам…" aria-label="Поиск по задачам" value="${esc(state.filter)}"><span class="muted">${count(rows.length,['задача','задачи','задач'])}</span></div>`+(groups.length?`<div class="queue-board">${groups.map(q=>`<section class="queue"><div class="queue-title"><span>${esc(queues[q]||q)}</span><span>${rows.filter(t=>t.queue===q).length}</span></div>${rows.filter(t=>t.queue===q).map(t=>`<button class="task-card" data-task="${esc(t.id)}"><small>${esc(t.id)}</small><strong>${esc(t.title || 'Без названия')}</strong>${t.error?badge('failed'):''}</button>`).join('')}</section>`).join('')}</div>`:empty('Задачи не найдены','Начните с диалога с планировщиком или создайте задачу через CLI.'));
}
function runs(){
 const rows=state.data.runs.filter(r=>state.runFilter==='batch'?!r.conversation_id:state.runFilter==='chat'?!!r.conversation_id:true);
 return heading('Запуски и ходы агентов','Сообщения, инструменты, проверки и результаты каждого запуска.')+`<div class="filter-row"><select id="run-filter" aria-label="Тип запусков"><option value="">Все запуски</option><option value="batch" ${state.runFilter==='batch'?'selected':''}>Пакетные</option><option value="chat" ${state.runFilter==='chat'?'selected':''}>Интерактивные</option></select></div><div class="split"><section class="panel run-list">${rows.map(r=>`<button class="run-item ${r.id===state.run?'selected':''}" data-run="${esc(r.id)}"><strong>${esc(roleName(r.role))}</strong><small>${esc(r.agent_id)} · ${esc(r.conversation_id?'Диалог':r.task_id)}</small>${badge(r.state)} <small>${when(r.created_at)} · ${short(r.id)}</small></button>`).join('')||empty('Нет запусков')}</section><section class="panel" id="run-detail">${empty(state.run?'Загружаем ход…':'Выберите запуск','Здесь появятся его сообщения и события.')}</section></div>`;
}
function chat(){
 const b=binding();if(!b)return empty('Роль не настроена','Откройте настройки исполнения.');
 const convs=state.data.conversations.filter(c=>c.binding_id===b.id);
 if(!state.conversation || !convs.some(c=>c.id===state.conversation)){state.conversation=convs.filter(c=>!c.closed&&!c.close_requested).at(-1)?.id || convs.at(-1)?.id || null;}
 const runs=state.data.runs.filter(r=>r.conversation_id===state.conversation);
 const options=convs.map((c,i)=>`<option value="${esc(c.id)}" ${c.id===state.conversation?'selected':''}>Диалог ${i+1} · ${count(c.turn_count,['ход','хода','ходов'])}${c.closed?' · закрыт':''}</option>`).join('');
 return permissionPanel(runs.map(r=>r.id))+`<section class="chat-shell"><div class="chat-head"><div class="avatar">${initials(b.role)}</div><div class="row-main"><strong>${esc(roleName(b.role))}</strong><small>${esc(b.agent)}${b.model?' · '+esc(b.model):' · модель по умолчанию'}</small></div><div class="right"><select id="conversation-select" aria-label="Выбрать диалог">${options||'<option>Новый диалог</option>'}</select><button class="button" data-action="new-chat">＋ Новый</button>${state.conversation?'<button class="button" data-action="close-chat">Закрыть</button>':''}</div></div><div id="conversation-status" class="conversation-state">${state.data.daemon.running?'Контекст и история сохраняются между ходами.':'Сообщения будут выполнены после запуска демона.'}</div><div class="messages" id="messages">${empty('С чего начнём?','Опишите задачу, задайте вопрос или продолжите работу.')}</div><form id="composer" class="composer"><textarea id="message-input" placeholder="Напишите ${esc(roleName(b.role).toLowerCase())}…" aria-label="Сообщение агенту" maxlength="65536">${esc(state.draft[b.id]||'')}</textarea><div class="composer-bottom"><span>Ctrl / ⌘ + Enter — отправить</span><div><button type="button" class="button danger hidden" id="interrupt-button" data-action="interrupt">■ Прервать ход</button> <button type="submit" id="send-button" class="button primary" ${state.busy?'disabled':''}>Отправить ↑</button></div></div></form></section>`;
}
function render(force=false){
 if(!state.data)return;
 const focused=document.activeElement?.id;const selection=document.activeElement?.selectionStart;
 const scroll=$('messages');const scrollTop=scroll?.scrollTop;const stick=scroll?scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<80:true;
 const openDetails=[...document.querySelectorAll('#main details[open]')].map(x=>x.id).filter(Boolean);
 const markup=state.view==='overview'?overview():state.view==='tasks'?tasks():state.view==='runs'?runs():chat();
 if(force||markup!==lastMarkup){$('main').innerHTML=markup;lastMarkup=markup;
  if(focused&&$(focused)){ $(focused).focus({preventScroll:true});if(selection!==null&&selection!==undefined&&$(focused).setSelectionRange)$(focused).setSelectionRange(selection,selection);}
 }
 if(state.view==='chat')loadChat(stick,scrollTop).catch(e=>toast(e.message));
 if(state.view==='runs'&&state.run)loadRun(openDetails).catch(e=>toast(e.message));
}
async function loadChat(stick=true,scrollTop=0){
 const id=state.conversation;if(!id)return;
 const document=await api('/api/conversations/'+id);
 let cached=state.chats.get(id)||{cursor:0,events:[]};let page;
 do {page=await api(`/api/conversations/${id}/events?after=${cached.cursor}`);cached.events=[...new Map([...cached.events,...page.events].map(e=>[e.sequence,e])).values()].sort((a,b)=>a.sequence-b.sequence);cached.cursor=Math.max(cached.cursor,page.cursor);} while(page.has_more);
 state.chats.set(id,cached);if(state.view!=='chat'||state.conversation!==id||!$('messages'))return;
 const turns=Object.values(document.turns).sort((a,b)=>a.sequence-b.sequence);
 const messages=turns.slice(-50).map(t=>{
  const output=cached.events.filter(e=>e.turn_id===t.id&&e.kind==='text').map(e=>e.text).join('');
  return `<article class="message user"><div class="avatar">ВЫ</div><div class="message-content"><div class="message-label">Вы <small>${when(t.queued_at)}</small></div><div class="message-text">${esc(t.prompt)}</div></div></article><article class="message"><div class="avatar">${initials(binding()?.role)}</div><div class="message-content"><div class="message-label">${esc(binding()?.agent)} <small>${esc(statuses[t.status]||t.status)}</small></div><div class="message-text">${esc(output)||'<span class="muted">'+(t.status==='queued'?'В очереди…':t.status==='running'?'Агент работает…':'Нет текстового ответа.')+'</span>'}</div>${t.output_truncated?'<small class="muted">Достигнут лимит сохранённого ответа.</small>':''}${t.reason&&t.reason!=='end_turn'?`<small class="muted">${esc(t.reason==='operator_cancelled'?'Отменено пользователем':t.reason)}</small>`:''}</div></article>`;
 }).join('');
 $('messages').innerHTML=(turns.length>50?'<p class="muted">Последние 50 ходов.</p>':'')+(messages||empty('Диалог готов','Первое сообщение задаст направление работы.'));
 if(stick)$('messages').scrollTop=$('messages').scrollHeight;else $('messages').scrollTop=scrollTop;
 const current=turns.find(t=>['running','queued'].includes(t.status));$('interrupt-button').classList.toggle('hidden',!current);$('interrupt-button').dataset.turn=current?.id||'';
 const closed=document.closed||document.close_requested;$('send-button').disabled=closed||state.busy;$('message-input').disabled=closed;
 $('conversation-status').textContent=closed?'Диалог закрыт. Создайте новый, чтобы продолжить.':current?(current.status==='queued'?'Сообщение в очереди.':'Агент выполняет ход. Можно отправить следующее сообщение в очередь.'):'История сохранена. Можно продолжать.';
 if(current?.status==='queued'&&document.dispatch?.reason)$('conversation-status').textContent+=' '+document.dispatch.reason;
}
function commandView(c){
 const streams=Object.entries(c.preview||{}).map(([name,output])=>`<small class="muted">${esc(name)}${output.truncated?' · показан фрагмент':''}</small><pre>${esc(output.text)||'Нет вывода.'}</pre>`).join('');
 return `<div class="evidence"><strong>${esc(c.command_id)}</strong> ${badge(c.status)}${c.preview_error?`<p>${esc(c.preview_error)}</p>`:streams||'<p class="muted">Вывод ещё не записан.</p>'}<details id="command-${esc(c.id)}"><summary>Сведения о проверке</summary><pre>${pretty({exit_code:c.exit_code,reason:c.reason,output:c.output})}</pre></details></div>`;
}
async function loadRun(openDetails=[]){
 const id=state.run;const d=await api('/api/runs/'+id);if(state.view!=='runs'||state.run!==id||!$('run-detail'))return;
 const r=d.run;const events=[];for(const event of d.activity.events){const previous=events.at(-1);if(event.kind==='message'&&previous?.kind==='message'){previous.text+=event.text;previous.truncated||=event.truncated;}else events.push({...event});}
 $('run-detail').innerHTML=`<div class="panel-head"><div><h3>${esc(roleName(r.role))}</h3><small>${esc(r.agent_id)} · ${short(r.id)}</small></div><div>${badge(r.state)} ${!terminal.has(r.state)?`<button class="button danger" data-control="cancel" data-id="${esc(id)}">Прервать</button>`:`<button class="button" data-control="retry" data-id="${esc(id)}">Повторить</button>`}</div></div><div class="trace">${permissionPanel([id])}${r.reason&&r.reason!=='turn_ended'?`<p class="subtitle">${esc(r.reason)}</p>`:''}${r.conversation_id?`<p><button class="text-button" data-open-conversation="${esc(r.conversation_id)}" data-binding-id="${esc(r.binding_id)}">Открыть диалог →</button></p>`:''}${d.activity.discarded_through?'<p class="muted">Начало журнала удалено по лимиту хранения.</p>':''}${events.map(e=>e.kind==='message'?`<div class="trace-event"><small>${when(e.at)} · сообщение${e.truncated?' · показан фрагмент':''}</small><div class="message-text">${esc(e.text)}</div></div>`:`<details id="event-${e.sequence}" class="trace-event"><summary>${esc(e.title||e.tool_kind||'Инструмент')} ${e.status?badge(e.status):''}</summary><pre>${pretty(e)}</pre></details>`).join('')||empty('Текстовых ходов пока нет',r.conversation_id?'Ответы находятся в диалоге.':r.outcome?.activity_unavailable?'Журнал не удалось записать. Состояние запуска и результаты доступны ниже.':'Для запусков до включения журнала доступна техническая сводка ниже.')}<h3>Проверки и результаты</h3>${d.commands.map(commandView).join('')||'<p class="subtitle">Запросов проверок пока нет.</p>'}${d.results.map(x=>`<details class="evidence" id="result-${esc(x.id)}"><summary>Результат ${badge(x.status)}</summary><pre>${pretty(x)}</pre></details>`).join('')}<details class="evidence" id="run-metadata"><summary>Состояние, протокол и события</summary><pre>${pretty({run:r,events:d.events})}</pre></details></div>`;
 for(const identity of openDetails)if($(identity))$(identity).open=true;
}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{state.data=await api('/api/state');renderChrome();render();}
 catch(e){$('connection-dot').classList.remove('online');$('connection-text').textContent='Связь потеряна · переподключаемся';$('notice').textContent=e.message;$('notice').classList.remove('hidden');}
 finally{refreshing=false;}
}
function navigate(view,b=null,r=null){state.view=view;state.binding=b;state.run=r;if(view!=='chat')state.conversation=null;lastMarkup='';renderChrome();render(true);}
async function sendMessage(){
 const input=$('message-input');const message=input.value;if(!message.trim()||state.busy)return;
 state.busy=true;$('send-button').disabled=true;
 try{
  if(!state.conversation){state.conversation=(await api('/api/conversations',{binding_id:state.binding})).id;}
  const key='greatminds-delivery:'+state.data.project+':'+state.conversation;
  let delivery;try{delivery=JSON.parse(localStorage.getItem(key))}catch{}
  if(!delivery||delivery.message!==message)delivery={message,request_id:crypto.randomUUID()};
  localStorage.setItem(key,JSON.stringify(delivery));
  await api('/api/conversations/'+state.conversation+'/send',delivery);
  localStorage.removeItem(key);state.draft[state.binding]='';input.value='';
 }catch(e){toast(e.message)}finally{state.busy=false;await refresh();}
}
async function openSettings(staged=null){
 if(staged){state.settings.document=staged;state.settings.text=JSON.stringify(staged,null,2);}else state.settings=await api('/api/settings');
 const valid=state.settings.document&&typeof state.settings.document==='object'&&!Array.isArray(state.settings.document);
 const doc=valid?state.settings.document:{version:1,agents:{},bindings:{}};
 $('settings-error').textContent='';
 const agents=Object.keys(doc.agents||{});
 $('settings-content').innerHTML=`<p class="subtitle">Исполнители и роли независимы. Интерактивные роли появятся во вкладках; пакетные получают задачи из очередей.</p><div class="form-row"><label class="field">Одновременные запуски<input id="max-running" type="number" min="1" value="${esc(doc.max_running||4)}"></label><label class="field">Лимит событий<input id="max-events" type="number" min="100" value="${esc(doc.max_runtime_events||10000)}"></label></div><section class="binding-settings"><h3>Исполнители</h3><p class="subtitle">${agents.map(a=>esc(a)+' · '+esc((doc.agents[a].argv||[]).join(' '))).join('<br>')||'Добавьте первый ACP-исполнитель.'}</p><div class="form-row"><label class="field">Харнес<select id="add-harness"><option value="codex">Codex</option><option value="claude">Claude</option><option value="grok">Grok</option></select></label><label class="field">Имя<input id="add-agent-name" placeholder="codex"></label><div class="field"><span>Новый манифест</span><button type="button" class="button" data-action="add-agent">＋ Добавить</button></div></div><label class="field">Команда запуска ACP — массив аргументов JSON<input id="add-agent-argv" value='["codex-acp"]'></label><div class="form-row"><label class="field">Версия ACP-адаптера<input id="add-adapter-version" placeholder="Установленная версия"></label><label class="field">Версия харнеса<input id="add-harness-version" placeholder="Установленная версия"></label></div></section><h3>Привязки ролей</h3><div id="binding-settings">${Object.entries(doc.bindings||{}).map(([id,b])=>`<section class="binding-settings" data-settings-binding="${esc(id)}"><h3>${esc(roleName(b.role))} <small class="muted">/ ${esc(id)}</small></h3><div class="form-row"><label class="field">Исполнитель<select data-field="agent">${agents.map(a=>`<option ${a===b.agent?'selected':''}>${esc(a)}</option>`).join('')}</select></label><label class="field">Модель<input data-field="model" placeholder="По умолчанию" value="${esc(b.model||'')}"></label><label class="field">Режим агента<input data-field="mode" placeholder="По умолчанию" value="${esc(b.mode||'')}"></label></div><div class="form-row"><label class="field">Работа<select data-field="scheduling"><option value="on-demand" ${b.scheduling!=='queue'?'selected':''}>Интерактивная</option><option value="queue" ${b.scheduling==='queue'?'selected':''}>Из очереди</option></select></label><label class="field">Разрешения<select data-field="permission">${['ask','deny','allow-workspace'].map(v=>`<option value="${v}" ${(b.permission||'ask')===v?'selected':''}>${({ask:'Спрашивать',deny:'Отклонять', 'allow-workspace':'Внутри рабочего каталога'})[v]}</option>`).join('')}</select></label><label class="field">Таймаут, секунды<input data-field="timeout_seconds" type="number" min="1" value="${esc(b.timeout_seconds||1800)}"></label></div></section>`).join('')||empty('Роли пока не настроены','Добавьте манифесты и привязки в полном конфиге ниже.')}<div class="form-row"><label class="field">Новая роль<select id="new-role">${state.settings.roles.filter(r=>!['USER','SYSTEM'].includes(r)).map(r=>`<option>${esc(r)}</option>`).join('')}</select></label><label class="field">Исполнитель<select id="new-agent">${agents.map(a=>`<option>${esc(a)}</option>`).join('')}</select></label><div class="field"><span>Новая интерактивная вкладка</span><button type="button" class="button" data-action="add-binding" ${!agents.length?'disabled':''}>＋ Добавить роль</button></div></div></div><details id="advanced-settings"><summary>Полный конфиг · исполнители, команды и дополнительные параметры</summary><p class="subtitle">Редактор YAML использует существующий контракт Greatminds. Значения секретов храните в окружении исполнителя.</p><textarea id="settings-yaml" class="settings-yaml" aria-label="Конфигурация YAML">${esc(state.settings.text)}</textarea><label class="field"><span><input type="checkbox" id="use-yaml" ${valid?'':'checked'}> Сохранить содержимое YAML вместо формы</span></label></details>`;
 if(!valid)$('advanced-settings').open=true;
 if(!$('settings-dialog').open)$('settings-dialog').showModal();
}
function formDocument(){
 const original=state.settings.document;const doc=structuredClone(original&&typeof original==='object'&&!Array.isArray(original)?original:{version:1});doc.agents||={};doc.bindings||={};doc.max_running=Number($('max-running').value);doc.max_runtime_events=Number($('max-events').value);
 document.querySelectorAll('[data-settings-binding]').forEach(section=>{const b=doc.bindings[section.dataset.settingsBinding];section.querySelectorAll('[data-field]').forEach(input=>{const k=input.dataset.field;if(['model','mode'].includes(k)&&!input.value.trim())delete b[k];else b[k]=k==='timeout_seconds'?Number(input.value):input.value;});});return doc;
}
document.addEventListener('click',async event=>{
 const t=event.target.closest('button,[data-run]');if(!t)return;
 try{
  if(t.dataset.close){$(t.dataset.close).close();return;}
  if(t.dataset.view){navigate(t.dataset.view);return;}
  if(t.dataset.binding){state.conversation=null;navigate('chat',t.dataset.binding);return;}
  if(t.dataset.run){navigate('runs',null,t.dataset.run);return;}
  if(t.dataset.openConversation){state.conversation=t.dataset.openConversation;navigate('chat',t.dataset.bindingId);return;}
  if(t.dataset.task){const d=await api('/api/tasks/'+t.dataset.task);$('detail-title').textContent=d.id;$('detail-body').innerHTML=`<p>${esc(queues[d.queue]||d.queue)}</p><pre>${esc(d.text)}</pre>`;$('detail-dialog').showModal();return;}
  if(t.id==='settings-button'){await openSettings();return;}
  if(t.id==='pause-button'){await api('/api/dispatch',{paused:!state.data.paused});await refresh();return;}
  if(t.id==='daemon-button'){t.disabled=true;await api('/api/daemon/'+(state.data.daemon.owned?'stop':'start'),{});await refresh();return;}
  if(t.dataset.permission){t.disabled=true;await api('/api/permissions/'+t.dataset.permission,{option_id:t.dataset.option});await refresh();return;}
  if(t.dataset.control){t.disabled=true;await api('/api/runs/'+t.dataset.id+'/'+t.dataset.control,{});await refresh();return;}
  if(t.dataset.action==='new-chat'){state.conversation=(await api('/api/conversations',{binding_id:state.binding})).id;await refresh();return;}
  if(t.dataset.action==='close-chat'){await api('/api/conversations/'+state.conversation+'/close',{});await refresh();return;}
  if(t.dataset.action==='interrupt'){await api('/api/conversations/'+state.conversation+'/interrupt',{request_id:t.dataset.turn});await refresh();return;}
  if(t.dataset.action==='add-agent'){
   const doc=formDocument();const id=$('add-agent-name').value.trim()||$('add-harness').value;
   if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/.test(id))throw new Error('Используйте простое имя исполнителя.');
   if(doc.agents[id])throw new Error('Исполнитель с таким именем уже существует.');
   const argv=JSON.parse($('add-agent-argv').value);if(!Array.isArray(argv)||!argv.length||argv.some(x=>typeof x!=='string'||!x.trim()))throw new Error('Укажите массив аргументов команды ACP.');
   const adapter_version=$('add-adapter-version').value.trim();const harness_version=$('add-harness-version').value.trim();if(!adapter_version||!harness_version)throw new Error('Укажите установленные версии адаптера и харнеса.');
   doc.agents[id]={transport:'acp',argv,adapter_version,harness_version};
   await openSettings(doc);return;
  }
  if(t.dataset.action==='add-binding'){
   const doc=formDocument();const role=$('new-role').value;let id=role.toLowerCase();let n=2;while(doc.bindings[id])id=role.toLowerCase()+'-'+n++;
   doc.bindings[id]={role,agent:$('new-agent').value,scheduling:'on-demand',permission:'ask'};
   await openSettings(doc);return;
  }
 }catch(e){toast(e.message);if(t.disabled)t.disabled=false;}
});
document.addEventListener('input',event=>{if(event.target.id==='message-input')state.draft[state.binding]=event.target.value;if(event.target.id==='task-search'){state.filter=event.target.value;render();}});
document.addEventListener('change',event=>{if(event.target.id==='add-harness'){const h=event.target.value;$('add-agent-name').value=h;$('add-agent-argv').value=JSON.stringify(h==='codex'?['codex-acp']:h==='claude'?['claude-agent-acp']:['grok','--no-auto-update','--permission-mode','default','agent','--no-leader','stdio']);}if(event.target.id==='conversation-select'){state.conversation=event.target.value;render(true);}if(event.target.id==='run-filter'){state.runFilter=event.target.value;render(true);}});
document.addEventListener('submit',async event=>{
 if(event.target.id==='composer'){event.preventDefault();await sendMessage();}
 if(event.target.id==='settings-form'){event.preventDefault();const submit=event.target.querySelector('[type=submit]');submit.disabled=true;try{await api('/api/settings', $('use-yaml').checked?{text:$('settings-yaml').value,revision:state.settings.revision}:{document:formDocument(),revision:state.settings.revision});$('settings-dialog').close();toast('Настройки сохранены. Для применения перезапустите демон.');await refresh();}catch(e){$('settings-error').textContent=e.message;}finally{submit.disabled=false;}}
});
document.addEventListener('keydown',event=>{if(event.target.id==='message-input'&&event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();sendMessage();}if(event.key==='Enter'&&event.target.matches('tr[data-run]'))navigate('runs',null,event.target.dataset.run);});
refresh();setInterval(refresh,1500);
