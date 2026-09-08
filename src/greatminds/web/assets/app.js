'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => esc(JSON.stringify(value, null, 2));
const names = {'ARCHITECT-PLANNER':'Планировщик','LIVE-DEVELOPER':'Разработка в диалоге','DEVELOPER':'Разработчик','TESTER':'Тестировщик','ARCHITECT-REVIEWER':'Ревьюер','UI-DEVELOPER':'UI-разработчик','TECHNICAL-WRITER':'Техписатель','READER':'Читатель','EXPLORER':'Исследователь','MAINTAINER':'Сопровождение'};
const statuses = {needs_review:'Нужен разбор',started:'Начато',command_finished:'Команда завершена',needs_recovery:'Нужно восстановление',resolved:'Последствия разобраны',selected:'Выбрано',running:'Работает',completed:'Завершён',failed:'Ошибка',cancelled:'Отменён',interrupted:'Прерван',waiting_input:'Ждёт решения',waiting_auth:'Нужен вход',starting:'Запускается',claimed:'Назначен',idle:'Готов',paused:'На паузе',queued:'В очереди',succeeded:'Пройдено',received:'Получен',applied:'Применён',rejected:'Отклонён'};
const queues = {feature_plan:'Планирование',feature_dev:'Разработка',feature_test:'Тестирование',feature_review:'Ревью',verified:'Готово',blocked:'Заблокировано',inbox:'Входящие'};
const terminal = new Set(['completed','failed','cancelled','interrupted']);
const state = {data:null, view:'overview', binding:null, conversation:null, run:null, draft:{}, chats:new Map(), filter:'', runFilter:'', busy:false, settings:null};
let toastTimer, refreshing=false, lastMarkup='';
let chatLoading=false, chatDocumentId=null, chatActive=false;
const turnNodes=new Map();
const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
function savedChoice(key,allowed,fallback){try{const value=localStorage.getItem(key);return allowed.includes(value)?value:fallback;}catch{return fallback;}}
let dashboardMode=savedChoice('greatminds-dashboard',['summary','kanban'],'summary');
let interfaceScale=savedChoice('greatminds-scale',['compact','normal','large'],document.documentElement.dataset.defaultScale||'normal');
function applyScale(value){interfaceScale=['compact','normal','large'].includes(value)?value:'normal';document.documentElement.dataset.scale=interfaceScale;}
applyScale(interfaceScale);
function dashboardControls(){return tr`<fieldset class="view-switch"><legend>Вид дашборда</legend>${[['summary',tr('Сводка')],['kanban','Kanban']].map(([value,label])=>`<label><input id="dashboard-${value}" type="radio" name="dashboard-mode" value="${value}" ${dashboardMode===value?'checked':''}>${label}</label>`).join('')}</fieldset>`;}
function setMarkup(node,markup){if(node.innerHTML!==markup)node.innerHTML=markup;}
function setText(node,text){if(node.textContent!==text)node.textContent=text;}
function applyTheme(theme){document.documentElement.dataset.theme=theme;const dark=theme==='dark';$('theme-button').textContent=dark?tr('☀ Светлая'):tr('☾ Тёмная');$('theme-button').setAttribute('aria-label',dark?tr('Включить светлую тему'):tr('Включить тёмную тему'));$('theme-button').setAttribute('aria-pressed',String(dark));}
let savedTheme;try{savedTheme=localStorage.getItem('greatminds-theme')}catch{}
applyTheme(['light','dark'].includes(savedTheme)?savedTheme:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
function count(n,forms){if(uiLanguage!=='ru')return n+' '+forms[n===1?0:1];const last=n%10;return n+' '+forms[n%100>=11&&n%100<=14?2:last===1?0:last>=2&&last<=4?1:2]}
function short(id){return String(id ?? '').slice(0,8)}
function when(at){return at ? new Date(at*1000).toLocaleTimeString(uiLocale(),{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—'}
function badge(status){return tr`<span class="pill ${esc(status)}">${esc(tr(statuses[status] || status) || '—')}</span>`}
function roleName(role){return tr(names[role] || role)}
function initials(role){return (role || 'AG').split('-').map(x=>x[0]).join('').slice(0,2)}
function toast(text){$('toast').textContent=text;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),7000)}
async function api(path, body){
 const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 const result=await response.json();if(!response.ok)throw new Error(result.error || tr`HTTP ${response.status}`);return result;
}
function interactive(){return (state.data?.bindings || []).filter(b=>b.scheduling==='on-demand')}
function binding(){return state.data?.bindings.find(b=>b.id===state.binding)}
function empty(title,description=''){return tr`<div class="empty"><span class="empty-icon">◇</span><strong>${esc(title)}</strong>${description?tr`<br>${esc(description)}`:''}</div>`}
function heading(title,description,actions=''){return tr`<div class="page-heading"><div><span class="eyebrow">${tr('Ваши агенты. Одно рабочее пространство.')}</span><h1>${esc(title)}</h1><p class="subtitle">${esc(description)}</p></div>${actions}</div>`}
function renderChrome(){
 const d=state.data;if(d.app_version)setText($('app-version'),'v'+d.app_version); $('project-name').textContent=d.name;$('project-path').textContent=d.project;$('project-path').title=d.project;
 $('task-count').textContent=d.tasks.length;$('run-count').textContent=d.runs.length;
 const title=state.view==='chat'?roleName(binding()?.role):({overview:tr('Обзор'),tasks:tr('Задачи'),runs:tr('Запуски'),stands:tr('Стенды')}[state.view]);$('page-name').textContent=title || tr('Диалог');
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view));
 $('role-nav').innerHTML=interactive().map(b=>tr`<button class="nav role-button ${state.binding===b.id&&state.view==='chat'?'active':''}" data-binding="${esc(b.id)}"><span>${initials(b.role)}</span>${esc(roleName(b.role))}</button>`).join('') || tr('<p class="subtitle">Добавьте интерактивные роли в настройках.</p>');
 $('tabs').innerHTML=tr`<button class="tab ${state.view==='overview'?'active':''}" role="tab" aria-selected="${state.view==='overview'}" data-view="overview">▦ Обзор</button>`+interactive().map(b=>tr`<button class="tab ${state.binding===b.id&&state.view==='chat'?'active':''}" role="tab" aria-selected="${state.binding===b.id&&state.view==='chat'}" data-binding="${esc(b.id)}">${esc(roleName(b.role))}<small>${esc(b.agent)}</small></button>`).join('');
 $('pause-button').textContent=d.paused?tr('▷ Продолжить'):tr('Ⅱ Пауза');$('pause-button').disabled=Boolean(d.configuration_error);
 const ds=d.daemon;const db=$('daemon-button');db.textContent=ds.running?tr('■ Остановить демон'):(ds.starting?tr('Запускается…'):tr('▷ Запустить демон'));db.disabled=(ds.running&&!ds.managed)||(!ds.running&&Boolean(d.configuration_error));
 $('connection-dot').classList.toggle('online',ds.running);$('connection-text').textContent=ds.running?tr('Демон подключён'):tr('Интерфейс подключён · демон остановлен');
 let notice=d.configuration_error?tr`Настройте исполнителей: ${d.configuration_error}`:ds.exit_code&& !ds.running?tr`Демон завершился с кодом ${ds.exit_code}. Проверьте конфигурацию и доступность исполнителей.`:ds.restart_required?tr('Настройки сохранены. Остановите и запустите демон, чтобы применить их.'):!ds.running?tr('Демон остановлен. Сообщения останутся в очереди до его запуска.'):d.paused?tr('Новые запуски на паузе. Уже открытые сессии продолжают работать.'):'';
 $('notice').textContent=notice;$('notice').classList.toggle('hidden',!notice);
 $('refresh-time').textContent=tr('Обновлено ')+new Date().toLocaleTimeString(uiLocale());
}
function permissionPanel(runIds=null){
 const rows=state.data.permissions.filter(p=>!runIds||runIds.includes(p.run_id));if(!rows.length)return '';
 return tr`<section class="permissions"><h3>◎ Нужно ваше решение · ${rows.length}</h3>`+rows.map(p=>tr`<div class="permission"><strong>${esc(p.tool.title || tr('Запрос инструмента'))}</strong><small class="muted">Запуск ${short(p.run_id)}</small><details id="permission-${esc(p.id)}"><summary>Посмотреть запрос</summary><pre>${pretty(p.tool)}</pre></details><div class="permission-actions">${p.options.filter(o=>['allow_once','reject_once'].includes(o.kind)).map(o=>tr`<button class="button ${o.kind==='allow_once'?'primary':''}" data-permission="${esc(p.id)}" data-option="${esc(o.optionId)}">${o.kind==='allow_once'?tr('Разрешить один раз'):tr('Отклонить')}</button>`).join('')}</div></div>`).join('')+'</section>';
}
function overview(){
 if(dashboardMode==='kanban')return heading(tr('Задачи проекта'),tr('Очереди и этапы отражают состояние, которое хранит демон.'))+dashboardControls()+permissionPanel()+taskBoard(state.data.tasks);
 const d=state.data;const active=d.runs.filter(r=>!terminal.has(r.state));const done=d.runs.filter(r=>r.state==='completed');
 const stats=[[tr('Активные запуски'),active.length,'↗',tr('В работе и ожидают решения')],[tr('Задачи в работе'),d.tasks.filter(t=>t.queue!=='verified').length,'☷',tr('В очередях проекта')],[tr('Завершённые запуски'),done.length,'✓',tr('За всю сохранённую историю')],[tr('Ждут решения'),d.permissions.length,'◎',tr('Разрешения инструментов')]];
 return heading(tr('Всё под контролем'),tr('Задачи, исполнители и решения — в одном рабочем пространстве.'),tr`<span class="date-label">${new Date().toLocaleDateString(uiLocale(),{day:'numeric',month:'long',year:'numeric'})}</span>`)+dashboardControls()+permissionPanel()+tr`<div class="stats">${stats.map(([n,v,s,f])=>tr`<div class="stat"><div class="stat-top">${n}<span class="stat-symbol">${s}</span></div><div class="stat-value">${v}</div><div class="stat-foot">${f}</div></div>`).join('')}</div><div class="grid"><div><section class="panel"><div class="panel-head"><h3>Команда исполнителей</h3><small>${count(d.bindings.length,[tr('роль'),tr('роли'),tr('ролей')])} настроено</small></div>${d.agents.map(a=>tr`<div class="agent-row"><div class="avatar ${a.state==='running'?'green':''}">${initials(a.role)}</div><div class="row-main"><strong>${esc(roleName(a.role))}</strong><small>${esc(a.agent_id)} · ${esc(a.task_id?.startsWith('chat-')?tr('Диалог'):a.task_id || a.binding_id)}</small></div>${badge(a.state)}${a.run_id?tr`<button class="text-button" data-run="${esc(a.run_id)}">Открыть ↗</button>`:''}</div>`).join('')||empty(tr('Команда пока не настроена'),tr('Откройте настройки и добавьте исполнителей и роли.'))}</section><section class="panel"><div class="panel-head"><h3>Последние запуски</h3><button class="text-button" data-view="runs">Все запуски →</button></div>${runTable(d.runs.slice(0,5))}</section></div><section class="panel"><div class="panel-head"><h3>Лента событий</h3><small>Последние 12</small></div>${d.events.filter(e=>!/^(run_stage_|protocol_|usage_|prompt_input_|queue_observations_)/.test(e.kind)).slice(-12).reverse().map(e=>tr`<div class="feed-item"><i></i><div><strong>${esc(eventName(e.kind))}</strong><small>${when(e.at)}${e.run_id?' · '+short(e.run_id):''}</small></div></div>`).join('')||empty(tr('Пока тихо'),tr('Новые события появятся здесь автоматически.'))}</section></div>`;
}
function eventName(kind){return ({claimed:tr('Задача назначена'),starting:tr('Исполнитель запускается'),running:tr('Исполнитель начал работу'),completed:tr('Запуск завершён'),failed:tr('Ошибка запуска'),permission_requested:tr('Запрошено разрешение'),permission_answered:tr('Решение оператора получено'),permission_consumed:tr('Разрешение передано агенту'),result_received:tr('Результат получен'),result_applied:tr('Результат применён'),result_applying:tr('Применяется результат'),result_validation_started:tr('Проверка результата началась'),result_validation_completed:tr('Результат проверен'),workspace_ready:tr('Рабочая директория готова'),command_succeeded:tr('Проверка пройдена'),cancelled:tr('Запуск отменён'),cancelling:tr('Запуск останавливается'),dispatch_paused:tr('Новые запуски приостановлены'),dispatch_resumed:tr('Новые запуски разрешены'),paused:tr('Выдача задач приостановлена'),resumed:tr('Выдача задач продолжена')}[kind]||kind.replaceAll('_',' '))}
function runTable(rows){return rows.length?tr`<div class="table-wrap"><table><thead><tr><th>ИСПОЛНИТЕЛЬ / РОЛЬ</th><th>ЗАДАЧА</th><th>СТАТУС</th><th>ВРЕМЯ</th></tr></thead><tbody>${rows.map(r=>tr`<tr data-run="${esc(r.id)}" tabindex="0"><td>${esc(r.agent_id)}<br><small class="muted">${esc(roleName(r.role))}</small></td><td class="mono">${esc(r.conversation_id?tr('Диалог'):r.task_id)}</td><td>${badge(r.state)}</td><td class="muted">${when(r.created_at)}</td></tr>`).join('')}</tbody></table></div>`:empty(tr('Запусков пока нет'),tr('Запустите задачу или напишите интерактивной роли.'))}
function tasks(){
 const rows=state.data.tasks.filter(t=>tr`${t.id} ${t.title} ${t.queue}`.toLowerCase().includes(state.filter.toLowerCase()));
 return heading(tr('Задачи проекта'),tr('Очереди и этапы отражают состояние, которое хранит демон.'))+tr`<div class="filter-row"><input id="task-search" class="search" placeholder="Поиск по задачам…" aria-label="Поиск по задачам" value="${esc(state.filter)}"><span class="muted">${count(rows.length,[tr('задача'),tr('задачи'),tr('задач')])}</span></div>`+taskBoard(rows);
}
function taskBoard(rows){
 const order=['feature_inbox','feature_plan','feature_dev','feature_test','feature_review','feature_blocked','verified'];
 const groups=[...new Set([...order,...rows.map(t=>t.queue)])];
 return groups.length?tr`<div class="queue-board">${groups.map(q=>tr`<section class="queue"><div class="queue-title"><span>${esc(tr(({feature_inbox:'Входящие',feature_blocked:'Заблокировано'})[q]||queues[q]||q))}</span><span>${rows.filter(t=>t.queue===q).length}</span></div>${rows.filter(t=>t.queue===q).map(t=>tr`<button class="task-card" data-task="${esc(t.id)}"><small>${esc(t.id)}</small><strong>${esc(t.title || tr('Без названия'))}</strong>${t.error?badge('failed'):''}</button>`).join('')}</section>`).join('')}</div>`:empty(tr('Задачи не найдены'),tr('Начните с диалога с планировщиком или создайте задачу через CLI.'));
}

function runs(){
 const rows=state.data.runs.filter(r=>state.runFilter==='batch'?!r.conversation_id:state.runFilter==='chat'?!!r.conversation_id:true);
 return heading(tr('Запуски и ходы агентов'),tr('Сообщения, инструменты, проверки и результаты каждого запуска.'))+tr`<div class="filter-row"><select id="run-filter" aria-label="Тип запусков"><option value="">Все запуски</option><option value="batch" ${state.runFilter==='batch'?'selected':''}>Пакетные</option><option value="chat" ${state.runFilter==='chat'?'selected':''}>Интерактивные</option></select></div><div class="split"><section class="panel run-list">${rows.map(r=>tr`<button class="run-item ${r.id===state.run?'selected':''}" data-run="${esc(r.id)}"><strong>${esc(roleName(r.role))}</strong><small>${esc(r.agent_id)} · ${esc(r.conversation_id?tr('Диалог'):r.task_id)}</small>${badge(r.state)} <small>${when(r.created_at)} · ${short(r.id)}</small></button>`).join('')||empty(tr('Нет запусков'))}</section><section class="panel" id="run-detail">${empty(state.run?tr('Загружаем ход…'):tr('Выберите запуск'),tr('Здесь появятся его сообщения и события.'))}</section></div>`;
}
function chat(){
 const b=binding();if(!b)return empty(tr('Роль не настроена'),tr('Откройте настройки исполнения.'));
 const convs=state.data.conversations.filter(c=>c.binding_id===b.id);
 if(!state.conversation || !convs.some(c=>c.id===state.conversation)){state.conversation=convs.filter(c=>!c.closed&&!c.close_requested).at(-1)?.id || convs.at(-1)?.id || null;}
 const runs=state.data.runs.filter(r=>r.conversation_id===state.conversation);
 const options=convs.map((c,i)=>tr`<option value="${esc(c.id)}" ${c.id===state.conversation?'selected':''}>Диалог ${i+1} · ${count(c.turn_count,[tr('ход'),tr('хода'),tr('ходов')])}${c.closed?tr(' · закрыт'):''}</option>`).join('');
 return tr`<div id="chat-permissions">${permissionPanel(runs.map(r=>r.id))}</div><section class="chat-shell"><div class="chat-head"><div class="avatar">${initials(b.role)}</div><div class="row-main"><strong>${esc(roleName(b.role))}</strong><small>${esc(b.agent)}${b.model?' · '+esc(b.model):tr(' · модель по умолчанию')}</small></div><div class="right"><select id="conversation-select" aria-label="Выбрать диалог">${options||tr('<option>Новый диалог</option>')}</select><button class="button" data-action="new-chat">＋ Новый</button><button id="close-chat-button" class="button ${state.conversation?'':'hidden'}" data-action="close-chat">Закрыть</button></div></div><div id="conversation-status" class="conversation-state">${state.data.daemon.running?tr('Контекст и история сохраняются между ходами.'):tr('Сообщения будут выполнены после запуска демона.')}</div><div class="messages" id="messages">${empty(tr('С чего начнём?'),tr('Опишите задачу, задайте вопрос или продолжите работу.'))}</div><form id="composer" class="composer"><textarea id="message-input" placeholder="Напишите ${esc(roleName(b.role).toLowerCase())}…" aria-label="Сообщение агенту" maxlength="65536">${esc(state.draft[b.id]||'')}</textarea><div class="composer-bottom"><span>Enter — отправить · Shift+Enter — новая строка</span><div><button type="button" class="button danger hidden" id="interrupt-button" data-action="interrupt">■ Прервать ход</button> <button type="submit" id="send-button" class="button primary" ${state.busy?'disabled':''}>Отправить ↑</button></div></div></form></section>`;
}
function render(force=false){
 if(!state.data)return;
 const boardScroll=document.querySelector('.queue-board')?.scrollLeft;
 const focused=document.activeElement?.id;const selection=document.activeElement?.selectionStart;
 const scroll=$('messages');const scrollTop=scroll?.scrollTop;const stick=scroll?scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<80:true;
 const openDetails=[...document.querySelectorAll('#main details[open]')].map(x=>x.id).filter(Boolean);
 if(state.view==='chat'){
  const key='chat:'+state.binding;
  if(lastMarkup!==key){$('main').innerHTML=chat();lastMarkup=key;chatDocumentId=null;turnNodes.clear();}
  updateChatChrome();loadChat().catch(e=>toast(e.message));return;
 }
 if(state.view==='stands'){if(lastMarkup!=='stands'){$('main').innerHTML=stands();lastMarkup='stands';}loadStands().catch(e=>toast(e.message));return;}
 const markup=state.view==='overview'?overview():state.view==='tasks'?tasks():state.view==='runs'?runs():chat();
 if(force||markup!==lastMarkup){$('main').innerHTML=markup;lastMarkup=markup;
  const board=document.querySelector('.queue-board');if(board&&boardScroll!==undefined)board.scrollLeft=boardScroll;
  if(focused&&$(focused)){ $(focused).focus({preventScroll:true});if(selection!==null&&selection!==undefined&&$(focused).setSelectionRange)$(focused).setSelectionRange(selection,selection);}
 }
 if(state.view==='chat')loadChat(stick,scrollTop).catch(e=>toast(e.message));
 if(state.view==='runs'&&state.run)loadRun(openDetails).catch(e=>toast(e.message));
}
function updateChatChrome(){
 const b=binding();if(!b||!$('conversation-select'))return;
 setText(document.querySelector('.chat-head .avatar'),initials(b.role));
 setText(document.querySelector('.chat-head .row-main strong'),roleName(b.role));
 setText(document.querySelector('.chat-head .row-main small'),b.agent+(b.model?' · '+b.model:tr(' · модель по умолчанию')));
 const convs=state.data.conversations.filter(c=>c.binding_id===b.id);
 if(!state.conversation||!convs.some(c=>c.id===state.conversation))state.conversation=convs.filter(c=>!c.closed&&!c.close_requested).at(-1)?.id||convs.at(-1)?.id||null;
 const options=convs.map((c,i)=>tr`<option value="${esc(c.id)}">Диалог ${i+1} · ${count(c.turn_count,[tr('ход'),tr('хода'),tr('ходов')])}${c.closed?tr(' · закрыт'):''}</option>`).join('')||tr('<option value="">Новый диалог</option>');
 setMarkup($('conversation-select'),options);$('conversation-select').value=state.conversation||'';
 $('close-chat-button').classList.toggle('hidden',!state.conversation);
 const ids=state.data.runs.filter(r=>r.conversation_id===state.conversation).map(r=>r.id);
 const open=[...$('chat-permissions').querySelectorAll('details[open]')].map(n=>n.id);
 setMarkup($('chat-permissions'),permissionPanel(ids));for(const id of open)if($(id))$(id).open=true;
}
function atBottom(node){return node.scrollHeight-node.scrollTop-node.clientHeight<80;}
function appendOutput(row,output,animate){
 if(row.target===output)return;
 row.target=output;
 cancelAnimationFrame(row.frame);
 if(!animate||reducedMotion.matches||!output.startsWith(row.text.data)){
  const stick=atBottom($('messages'));row.text.data=output;renderMarkdown(row.answer,output);if(stick)$('messages').scrollTop=$('messages').scrollHeight;return;
 }
 const chars=Array.from(output.slice(row.text.data.length));let shown=0;let started=null;
 function frame(now){
  if(!row.node.isConnected)return;
  started??=now;
  const next=Math.min(chars.length,Math.max(shown,Math.ceil(chars.length*Math.min(1,(now-started)/280))));
  if(next>shown){const pane=$('messages');const stick=atBottom(pane);row.text.appendData(chars.slice(shown,next).join(''));shown=next;renderMarkdown(row.answer,row.text.data);if(stick)pane.scrollTop=pane.scrollHeight;}
  if(shown<chars.length)row.frame=requestAnimationFrame(frame);
 }
 row.frame=requestAnimationFrame(frame);
}
function updateTurns(doc,cached){
 const pane=$('messages');
 // The caller sets the conversation identity; history nodes survive all polls.
 const allTurns=Object.values(doc.turns).sort((a,b)=>a.sequence-b.sequence);
 const turns=allTurns.slice(-50);
 let limit=pane.querySelector('.history-limit');
 if(allTurns.length>50&&!limit){limit=document.createElement('p');limit.className='muted history-limit';limit.textContent=tr('Последние 50 ходов.');pane.prepend(limit);}
 if(allTurns.length<=50)limit?.remove();
 const live=new Set(turns.map(t=>t.id));const stick=atBottom(pane);
 for(const [id,row] of turnNodes)if(!live.has(id)){cancelAnimationFrame(row.frame);row.node.remove();turnNodes.delete(id);}
 if(!turns.length){if(!pane.querySelector('.empty'))pane.innerHTML=empty(tr('Диалог готов'),tr('Первое сообщение задаст направление работы.'));return;}
 pane.querySelector('.empty')?.remove();
 const outputs=new Map();for(const e of cached.events)if(e.kind==='text')outputs.set(e.turn_id,(outputs.get(e.turn_id)||'')+e.text);
 for(const t of turns){
  let row=turnNodes.get(t.id);const fresh=!row;
  if(!row){
   const node=document.createElement('section');node.className='chat-turn';node.dataset.turn=t.id;
   node.innerHTML=tr`<article class="message user"><div class="avatar">ВЫ</div><div class="message-content"><div class="message-label">Вы <small class="queued-at"></small></div><div class="message-text prompt"></div></div></article><article class="message"><div class="avatar">${esc(initials(binding()?.role))}</div><div class="message-content"><div class="message-label">${esc(binding()?.agent)} <small class="turn-status"></small></div><div class="message-text answer"></div><div class="agent-activity hidden" role="status"><span class="activity-spinner" aria-hidden="true"></span><span class="activity-label"></span></div><small class="muted turn-note"></small></div></article>`;
   const text=document.createTextNode('');const answer=node.querySelector('.answer');answer.classList.add('markdown');
   row={node,text,answer,target:null,frame:null};turnNodes.set(t.id,row);pane.append(node);
  }
  setText(row.node.querySelector('.prompt'),t.prompt);
  setText(row.node.querySelector('.queued-at'),when(t.queued_at));
  setText(row.node.querySelector('.turn-status'),tr(statuses[t.status]||t.status));
  const output=outputs.get(t.id)||'';appendOutput(row,output,!fresh);
  const active=['running','queued'].includes(t.status);
  const run=state.data.runs.find(r=>r.conversation_id===state.conversation&&!terminal.has(r.state));
  const waiting=run?.state==='waiting_input'||run?.state==='waiting_auth';
  const label=t.status==='queued'?tr('В очереди…'):run?.state==='waiting_input'?tr('Ожидает вашего решения'):run?.state==='waiting_auth'?tr('Ожидает входа в аккаунт'):tr('Агент работает…');
  const activity=row.node.querySelector('.agent-activity');activity.classList.toggle('hidden',!active);activity.classList.toggle('waiting',waiting||t.status==='queued');
  setText(row.node.querySelector('.activity-label'),label);
  setText(row.node.querySelector('.turn-note'),t.output_truncated?tr('Достигнут лимит сохранённого ответа.'):t.reason&&t.reason!=='end_turn'?(t.reason==='operator_cancelled'?tr('Отменено пользователем'):t.reason):!active&&!output?tr('Нет текстового ответа.'):'');
 }
 if(stick)pane.scrollTop=pane.scrollHeight;
}
async function loadChat(){
 const id=state.conversation;if(!id||!$('messages'))return;
 if(chatDocumentId!==id){for(const row of turnNodes.values())cancelAnimationFrame(row.frame);turnNodes.clear();$('messages').innerHTML=empty(tr('Загружаем диалог…'));chatDocumentId=id;}
 if(chatLoading)return;
 chatLoading=true;
 try{
  const doc=await api('/api/conversations/'+id);
  let cached=state.chats.get(id)||{cursor:0,events:[]};let page;
  do {page=await api(tr`/api/conversations/${id}/events?after=${cached.cursor}`);cached.events=[...new Map([...cached.events,...page.events].map(e=>[e.sequence,e])).values()].sort((a,b)=>a.sequence-b.sequence);cached.cursor=Math.max(cached.cursor,page.cursor);} while(page.has_more);
  state.chats.set(id,cached);if(state.view!=='chat'||state.conversation!==id||!$('messages'))return;
  updateTurns(doc,cached);
  const turns=Object.values(doc.turns);const current=turns.find(t=>['running','queued'].includes(t.status));chatActive=Boolean(current);
  $('interrupt-button').classList.toggle('hidden',!current);$('interrupt-button').dataset.turn=current?.id||'';
  const closed=doc.closed||doc.close_requested;$('send-button').disabled=closed||state.busy;$('message-input').disabled=closed;
  let message=closed?tr('Диалог закрыт. Создайте новый, чтобы продолжить.'):current?(current.status==='queued'?tr('Сообщение в очереди.'):tr('Агент выполняет ход. Можно отправить следующее сообщение в очередь.')):tr('История сохранена. Можно продолжать.');
  if(current?.status==='queued'&&doc.dispatch?.reason)message+=' '+doc.dispatch.reason;
  setText($('conversation-status'),message);
 }finally{chatLoading=false;}
}
function commandView(c){
 const streams=Object.entries(c.preview||{}).map(([name,output])=>tr`<small class="muted">${esc(name)}${output.truncated?tr(' · показан фрагмент'):''}</small><pre>${esc(output.text)||tr('Нет вывода.')}</pre>`).join('');
 return tr`<div class="evidence"><strong>${esc(c.command_id)}</strong> ${badge(c.status)}${c.preview_error?tr`<p>${esc(c.preview_error)}</p>`:streams||tr('<p class="muted">Вывод ещё не записан.</p>')}<details id="command-${esc(c.id)}"><summary>Сведения о проверке</summary><pre>${pretty({exit_code:c.exit_code,reason:c.reason,output:c.output})}</pre></details></div>`;
}
async function loadRun(openDetails=[]){
 const id=state.run;const d=await api('/api/runs/'+id);if(state.view!=='runs'||state.run!==id||!$('run-detail'))return;
 const r=d.run;const events=[];for(const event of d.activity.events){const previous=events.at(-1);if(event.kind==='message'&&previous?.kind==='message'){previous.text+=event.text;previous.truncated||=event.truncated;}else events.push({...event});}
 $('run-detail').innerHTML=tr`<div class="panel-head"><div><h3>${esc(roleName(r.role))}</h3><small>${esc(r.agent_id)} · ${short(r.id)}</small></div><div>${badge(r.state)} ${!terminal.has(r.state)?tr`<button class="button danger" data-control="cancel" data-id="${esc(id)}">Прервать</button>`:tr`<button class="button" data-control="retry" data-id="${esc(id)}">Повторить</button>`}</div></div><div class="trace">${permissionPanel([id])}${r.reason&&r.reason!=='turn_ended'?tr`<p class="subtitle">${esc(r.reason)}</p>`:''}${r.conversation_id?tr`<p><button class="text-button" data-open-conversation="${esc(r.conversation_id)}" data-binding-id="${esc(r.binding_id)}">Открыть диалог →</button></p>`:''}${d.activity.discarded_through?tr('<p class="muted">Начало журнала удалено по лимиту хранения.</p>'):''}${events.map(e=>e.kind==='message'?tr`<div class="trace-event"><small>${when(e.at)} · сообщение${e.truncated?tr(' · показан фрагмент'):''}</small><div class="message-text markdown">${markdown(e.text)}</div></div>`:tr`<details id="event-${e.sequence}" class="trace-event"><summary>${esc(e.title||e.tool_kind||tr('Инструмент'))} ${e.status?badge(e.status):''}</summary><pre>${pretty(e)}</pre></details>`).join('')||empty(tr('Текстовых ходов пока нет'),r.conversation_id?tr('Ответы находятся в диалоге.'):(d.activity.unavailable||r.outcome?.activity_unavailable)?tr('Журнал не удалось записать. Состояние запуска и результаты доступны ниже.'):tr('Для запусков до включения журнала доступна техническая сводка ниже.'))}<h3>Проверки и результаты</h3>${d.commands.map(commandView).join('')||tr('<p class="subtitle">Запросов проверок пока нет.</p>')}${d.results.map(x=>tr`<details class="evidence" id="result-${esc(x.id)}"><summary>Результат ${badge(x.status)}</summary><pre>${pretty(x)}</pre></details>`).join('')}<details class="evidence" id="run-metadata"><summary>Состояние, протокол и события</summary><pre>${pretty({run:r,events:d.events})}</pre></details></div>`;
 for(const identity of openDetails)if($(identity))$(identity).open=true;
}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{state.data=await api('/api/state');renderChrome();render();}
 catch(e){$('connection-dot').classList.remove('online');$('connection-text').textContent=tr('Связь потеряна · переподключаемся');$('notice').textContent=e.message;$('notice').classList.remove('hidden');}
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
 $('settings-content').innerHTML=tr`<label class="field scale-field">Размер интерфейса<select id="interface-scale">${[['compact',tr('Компактный')],['normal',tr('Обычный')],['large',tr('Крупный')]].map(([value,label])=>`<option value="${value}" ${interfaceScale===value?'selected':''}>${label}</option>`).join('')}</select><small>Применяется сразу и сохраняется в этом браузере.</small></label><p class="subtitle">Исполнители и роли независимы. Интерактивные роли появятся во вкладках; пакетные получают задачи из очередей.</p><div class="form-row"><label class="field">Одновременные запуски<input id="max-running" type="number" min="1" value="${esc(doc.max_running||4)}"></label><label class="field">Лимит событий<input id="max-events" type="number" min="100" value="${esc(doc.max_runtime_events||10000)}"></label></div><section class="binding-settings"><h3>Исполнители</h3><p class="subtitle">${agents.map(a=>esc(a)+' · '+esc((doc.agents[a].argv||[]).join(' '))).join('<br>')||tr('Добавьте первый ACP-исполнитель.')}</p><div class="form-row"><label class="field">Харнес<select id="add-harness"><option value="codex">Codex</option><option value="claude">Claude</option><option value="grok">Grok</option></select></label><label class="field">Имя<input id="add-agent-name" placeholder="codex"></label><div class="field"><span>Новый манифест</span><button type="button" class="button" data-action="add-agent">＋ Добавить</button></div></div><label class="field">Команда запуска ACP — массив аргументов JSON<input id="add-agent-argv" value='["codex-acp"]'></label><div class="form-row"><label class="field">Версия ACP-адаптера<input id="add-adapter-version" placeholder="Установленная версия"></label><label class="field">Версия харнеса<input id="add-harness-version" placeholder="Установленная версия"></label></div></section><h3>Привязки ролей</h3><div id="binding-settings">${Object.entries(doc.bindings||{}).map(([id,b])=>tr`<section class="binding-settings" data-settings-binding="${esc(id)}"><h3>${esc(roleName(b.role))} <small class="muted">/ ${esc(id)}</small></h3><div class="form-row"><label class="field">Исполнитель<select data-field="agent">${agents.map(a=>tr`<option ${a===b.agent?'selected':''}>${esc(a)}</option>`).join('')}</select></label><label class="field model-field">Модель<input data-field="model" placeholder="По умолчанию" value="${esc(b.model||'')}"></label><label class="field reasoning-field hidden">${tr('Уровень рассуждения')}<input data-field="reasoning" value="${esc(b.reasoning||'')}"></label><label class="field">Режим агента<input data-field="mode" placeholder="По умолчанию" value="${esc(b.mode||'')}"></label></div><button type="button" class="button" data-discover="${esc(id)}">Загрузить модели и reasoning</button><p class="subtitle capability-status" role="status"></p><div class="form-row"><label class="field">Работа<select data-field="scheduling"><option value="on-demand" ${b.scheduling!=='queue'?'selected':''}>Интерактивная</option><option value="queue" ${b.scheduling==='queue'?'selected':''}>Из очереди</option></select></label><label class="field">Разрешения<select data-field="permission">${['ask','deny','allow-workspace'].map(v=>tr`<option value="${v}" ${(b.permission||'ask')===v?'selected':''}>${({ask:tr('Спрашивать'),deny:tr('Отклонять'), 'allow-workspace':tr('Внутри рабочего каталога')})[v]}</option>`).join('')}</select></label><label class="field">Таймаут, секунды<input data-field="timeout_seconds" type="number" min="1" value="${esc(b.timeout_seconds||1800)}"></label></div></section>`).join('')||empty(tr('Роли пока не настроены'),tr('Добавьте манифесты и привязки в полном конфиге ниже.'))}<div class="form-row"><label class="field">Новая роль<select id="new-role">${state.settings.roles.filter(r=>!['USER','SYSTEM'].includes(r)).map(r=>tr`<option>${esc(r)}</option>`).join('')}</select></label><label class="field">Исполнитель<select id="new-agent">${agents.map(a=>tr`<option>${esc(a)}</option>`).join('')}</select></label><div class="field"><span>Новая интерактивная вкладка</span><button type="button" class="button" data-action="add-binding" ${!agents.length?'disabled':''}>＋ Добавить роль</button></div></div></div><details id="advanced-settings"><summary>Полный конфиг · исполнители, команды и дополнительные параметры</summary><p class="subtitle">Редактор YAML использует существующий контракт Greatminds. Значения секретов храните в окружении исполнителя.</p><textarea id="settings-yaml" class="settings-yaml" aria-label="Конфигурация YAML">${esc(state.settings.text)}</textarea><label class="field"><span><input type="checkbox" id="use-yaml" ${valid?'':'checked'}> Сохранить содержимое YAML вместо формы</span></label></details>`;
 if(!valid)$('advanced-settings').open=true;
 if(!$('settings-dialog').open)$('settings-dialog').showModal();
 if(valid)for(const section of document.querySelectorAll('[data-settings-binding]'))discoverOptions(section);
}
function formDocument(){
 const original=state.settings.document;const doc=structuredClone(original&&typeof original==='object'&&!Array.isArray(original)?original:{version:1});doc.agents||={};doc.bindings||={};doc.max_running=Number($('max-running').value);doc.max_runtime_events=Number($('max-events').value);
 document.querySelectorAll('[data-settings-binding]').forEach(section=>{const b=doc.bindings[section.dataset.settingsBinding];section.querySelectorAll('[data-field]').forEach(input=>{const k=input.dataset.field;if(['model','mode','reasoning'].includes(k)&&!input.value.trim())delete b[k];else b[k]=k==='timeout_seconds'?Number(input.value):input.value;});});return doc;
}
document.addEventListener('click',async event=>{
 const t=event.target.closest('button,[data-run]');if(!t)return;
 try{
  if(t.dataset.close){$(t.dataset.close).close();return;}
  if(t.dataset.view){navigate(t.dataset.view);return;}
  if(t.dataset.binding){state.conversation=null;navigate('chat',t.dataset.binding);return;}
  if(t.dataset.run){navigate('runs',null,t.dataset.run);return;}
  if(t.dataset.openConversation){state.conversation=t.dataset.openConversation;navigate('chat',t.dataset.bindingId);return;}
  if(t.dataset.task){const d=await api('/api/tasks/'+t.dataset.task);$('detail-title').textContent=d.id;$('detail-body').innerHTML=tr`<p>${esc(tr(queues[d.queue]||d.queue))}</p><pre>${esc(d.text)}</pre>`;$('detail-dialog').showModal();return;}
  if(t.id==='theme-button'){const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';applyTheme(theme);try{localStorage.setItem('greatminds-theme',theme)}catch{}return;}
  if(t.dataset.discover){await discoverOptions(t.closest('[data-settings-binding]'),{force:true});return;}
  if(t.id==='settings-button'){await openSettings();return;}
  if(t.id==='pause-button'){await api('/api/dispatch',{paused:!state.data.paused});await refresh();return;}
  if(t.id==='daemon-button'){t.disabled=true;await api('/api/daemon/'+(state.data.daemon.running?'stop':'start'),{});await refresh();return;}
  if(t.dataset.permission){t.disabled=true;await api('/api/permissions/'+t.dataset.permission,{option_id:t.dataset.option});await refresh();return;}
  if(t.dataset.control){t.disabled=true;await api('/api/runs/'+t.dataset.id+'/'+t.dataset.control,{});await refresh();return;}
  if(t.dataset.action==='new-chat'){state.conversation=(await api('/api/conversations',{binding_id:state.binding})).id;await refresh();return;}
  if(t.dataset.action==='close-chat'){await api('/api/conversations/'+state.conversation+'/close',{});await refresh();return;}
  if(t.dataset.action==='interrupt'){await api('/api/conversations/'+state.conversation+'/interrupt',{request_id:t.dataset.turn});await refresh();return;}
  if(t.dataset.action==='add-agent'){
   const doc=formDocument();const id=$('add-agent-name').value.trim()||$('add-harness').value;
   if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/.test(id))throw new Error(tr('Используйте простое имя исполнителя.'));
   if(doc.agents[id])throw new Error(tr('Исполнитель с таким именем уже существует.'));
   const argv=JSON.parse($('add-agent-argv').value);if(!Array.isArray(argv)||!argv.length||argv.some(x=>typeof x!=='string'||!x.trim()))throw new Error(tr('Укажите массив аргументов команды ACP.'));
   const adapter_version=$('add-adapter-version').value.trim();const harness_version=$('add-harness-version').value.trim();if(!adapter_version||!harness_version)throw new Error(tr('Укажите установленные версии адаптера и харнеса.'));
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
document.addEventListener('change',event=>{if(event.target.dataset.field==='model'&&event.target.tagName==='SELECT'){const section=event.target.closest('[data-settings-binding]');section.querySelector('[data-field=reasoning]').value='';section.querySelector('.reasoning-field').classList.add('hidden');discoverOptions(section).catch(e=>toast(e.message));}if(event.target.dataset.field==='agent'){const section=event.target.closest('[data-settings-binding]');section.querySelector('[data-field=model]').value='';section.querySelector('[data-field=reasoning]').value='';discoverOptions(section).catch(e=>toast(e.message));}if(event.target.id==='add-harness'){const h=event.target.value;$('add-agent-name').value=h;$('add-agent-argv').value=JSON.stringify(h==='codex'?['codex-acp']:h==='claude'?['claude-agent-acp']:['grok','--no-auto-update','--permission-mode','default','agent','--no-leader','stdio']);}if(event.target.id==='conversation-select'){state.conversation=event.target.value;render(true);}if(event.target.id==='run-filter'){state.runFilter=event.target.value;render(true);}});
document.addEventListener('submit',async event=>{
 if(event.target.id==='composer'){event.preventDefault();await sendMessage();}
 if(event.target.id==='settings-form'){event.preventDefault();const submit=event.target.querySelector('[type=submit]');submit.disabled=true;try{await api('/api/settings', $('use-yaml').checked?{text:$('settings-yaml').value,revision:state.settings.revision}:{document:formDocument(),revision:state.settings.revision});$('settings-dialog').close();toast(tr('Настройки сохранены. Для применения перезапустите демон.'));await refresh();}catch(e){$('settings-error').textContent=e.message;}finally{submit.disabled=false;}}
});
document.addEventListener('keydown',event=>{if(event.target.id==='message-input'&&event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229&&!event.repeat){event.preventDefault();sendMessage();}if(event.key==='Enter'&&event.target.matches('tr[data-run]'))navigate('runs',null,event.target.dataset.run);});
refresh();setInterval(refresh,1500);
setInterval(()=>{if(state.view==='chat'&&chatActive)loadChat().catch(e=>toast(e.message));},350);

const capabilityCache=new Map();
let capabilityQueue=Promise.resolve();
function executorOptions(document,bindingId,model,force){
 const binding=document.bindings[bindingId];
 const key=JSON.stringify([document.agents[binding.agent],binding.workspace||'.',binding.mode||'',model]);
 const cached=capabilityCache.get(key);
 if(!force&&cached&&Date.now()-cached.at<60000)return cached.promise;
 const promise=capabilityQueue.then(()=>api('/api/executor-options',{document,binding_id:bindingId,model}));
 capabilityQueue=promise.catch(()=>{});
 const entry={at:Date.now(),promise};capabilityCache.set(key,entry);
 promise.catch(()=>{if(capabilityCache.get(key)===entry)capabilityCache.delete(key);});
 return promise;
}
async function discoverOptions(section,{force=false}={}){
 const button=section.querySelector('[data-discover]'),status=section.querySelector('.capability-status');
 button.disabled=true;setText(status,tr('Загружаем возможности ACP…'));
 const agent=section.querySelector('[data-field=agent]').value;
 const selected=section.querySelector('[data-field=model]').value;
 const token=Symbol();section.discoveryToken=token;
 try{
  const result=await executorOptions(formDocument(),section.dataset.settingsBinding,selected,force);
  if(!section.isConnected||section.discoveryToken!==token||section.querySelector('[data-field=agent]').value!==agent||section.querySelector('[data-field=model]').value!==selected)return;
  for(const field of ['model','reasoning']){
   const input=section.querySelector(tr`[data-field=${field}]`),options=result[field];
   const label=input.closest('label');
   if(field==='reasoning')label.classList.toggle('hidden',!options);
   const control=document.createElement(options?'select':'input');control.dataset.field=field;
   if(options){
    control.innerHTML=tr`<option value="">${esc(tr('По умолчанию'))} (${esc(options.current)})</option>`+options.options.map(o=>tr`<option value="${esc(o.value)}">${esc(o.name)}</option>`).join('');
    control.value=options.options.some(o=>o.value===input.value)?input.value:'';
   }else{control.value=field==='model'?input.value:'';control.placeholder=tr('По умолчанию');}
   input.replaceWith(control);
  }
  setText(status,result.model?tr('Возможности получены от исполнителя.'):tr('Исполнитель не предоставил список моделей. Доступен ручной ввод; значение проверяется при запуске.'));
 }catch(e){if(section.discoveryToken===token)setText(status,e.message);}finally{if(section.discoveryToken===token)button.disabled=false;}
}
document.getElementById('language-select').addEventListener('change',async event=>{
 const staged=$('settings-dialog').open?formDocument():null;
 const yamlText=$('settings-yaml')?.value,useYaml=$('use-yaml')?.checked;
 uiLanguage=event.target.value;try{localStorage.setItem('greatminds-language',uiLanguage)}catch{}
 applyLanguage();applyTheme(document.documentElement.dataset.theme);
 // Drafts already live in state. Changing language never changes agent output.
 lastMarkup='';renderChrome();render(true);
 if(staged){await openSettings(staged);$('settings-yaml').value=yamlText;$('use-yaml').checked=useYaml;}
});

document.addEventListener('change',event=>{
 if(event.target.id==='interface-scale'){applyScale(event.target.value);try{localStorage.setItem('greatminds-scale',interfaceScale)}catch{}}
 if(event.target.name==='dashboard-mode'){dashboardMode=event.target.value;try{localStorage.setItem('greatminds-dashboard',dashboardMode)}catch{}lastMarkup='';render(true);}
});

if(typeof ResizeObserver!=='undefined')new ResizeObserver(entries=>{
 document.documentElement.style.setProperty('--toolbar-height',entries[0].target.getBoundingClientRect().height+'px');
}).observe(document.querySelector('.toolbar'));
