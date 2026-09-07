'use strict';
let standData=null,standLoading=false,standEditor=null;
function stands(){return heading(tr('Стенды'),tr('Машины, Ansible и состояние стенда проекта.'))+`<div id="stand-summary"></div><section class="panel"><div class="panel-head"><h3>${tr('Машины и доступы')}</h3></div><div class="dialog-body"><p>${tr('Один логический стенд может включать несколько машин. SSH-ключи и параметры доступа задаются в SSH config; секреты остаются на машине.')}</p><form id="stand-connections"><div id="stand-machines"></div><button type="button" class="button" data-stand-add>${tr('Добавить машину')}</button> <button class="button primary">${tr('Сохранить настройки')}</button></form><p class="muted" id="stand-paths"></p></div></section><section class="panel"><div class="panel-head"><h3>Ansible</h3><button class="button" data-stand-op="doctor">${tr('Проверить профили')}</button></div><div class="dialog-body"><div id="stand-profiles"></div><div id="stand-files"></div><form id="stand-editor" class="hidden"><h3 id="stand-editor-name"></h3><textarea id="stand-file-text" class="settings-yaml" aria-label="Ansible"></textarea><button class="button primary">${tr('Сохранить файл')}</button><span id="stand-file-status" role="status"></span></form></div></section><section class="panel"><div class="panel-head"><h3>${tr('Операции')}</h3></div><div class="dialog-body"><p>${tr('Операции выполняет демон. Закрытие веб-интерфейса не останавливает их. Развёртывание использует профиль активной аренды.')}</p><form id="stand-operation"><div class="form-row"><label class="field">${tr('Операция')}<select name="action" id="stand-action"><option value="deploy">${tr('Развернуть активную аренду')}</option><option value="lease">${tr('Запросить аренду')}</option><option value="release">${tr('Освободить аренду')}</option><option value="reclaim">${tr('Вернуть просроченную аренду')}</option><option value="down">${tr('Пометить недоступным')}</option><option value="up">${tr('Восстановить доступность')}</option><option value="deployment-recover">${tr('Остановить процесс незавершённого развёртывания')}</option><option value="deployment-resolve">${tr('Подтвердить разбор последствий')}</option></select></label></div><div id="stand-operation-fields"></div><button class="button primary">${tr('Передать демону')}</button><span id="stand-operation-status" role="status"></span></form></div></section><section class="panel"><div class="panel-head"><h3>${tr('История операций и развёртываний')}</h3></div><div id="stand-history" class="dialog-body"></div></section>`;}
function machineRow(key='',host='',user=''){return `<div class="form-row machine"><label class="field">${tr('Суффикс переменной')}<input data-machine-key value="${esc(key)}" placeholder="GPU"></label><label class="field">${tr('SSH-имя или адрес')}<input data-machine-host value="${esc(host)}" placeholder="localhost"></label><label class="field">${tr('Пользователь')}<input data-machine-user value="${esc(user)}"></label><button type="button" class="button" data-machine-check>${tr('Проверить SSH')}</button><button type="button" class="button" data-machine-remove>${tr('Удалить')}</button></div>`;}
function standFields(){
 const action=$('stand-action').value,lease=standData?.state.active_lease;
 const input=(name,label,value='')=>`<label class="field">${tr(label)}<input name="${name}" value="${esc(value)}" required></label>`;
 let html='';
 if(['deploy','release','reclaim'].includes(action))html+=input('lease_id','ID аренды',lease?.lease_id||'');
 if(action==='lease')html+=input('task_id','ID задачи')+`<label class="field">${tr('Профиль')}<select name="profile">${(standData?.profiles||[]).map(p=>`<option>${esc(p.name)}</option>`).join('')}</select></label>`;
 if(['lease','release'].includes(action))html+=`<label class="field">${tr('Роль владельца')}<select name="role">${['TESTER','EXPLORER','DEVELOPER','ARCHITECT-PLANNER','MAINTAINER','ARCHITECT-REVIEWER','LIVE-DEVELOPER'].map(r=>`<option ${r===lease?.holder_role?'selected':''}>${r}</option>`).join('')}</select></label>`;
 if(action==='release')html+=`<label class="field">${tr('Результат')}<select name="result"><option>pass</option><option>fail</option><option>partial</option></select></label>`;
 if(action.startsWith('deployment-'))html+=input('attempt_id','ID развёртывания');
 if(['up','down','deployment-resolve'].includes(action))html+=input('reason','Причина');
 $('stand-operation-fields').innerHTML=`<div class="form-row">${html}</div>`;
}
async function loadStands(){
 if(standLoading)return;standLoading=true;
 try{
  const data=await api('/api/stands');if(state.view!=='stands'||!$('stand-summary'))return;standData=data;
  const s=data.state;
  setMarkup($('stand-summary'),`<div class="stats"><div class="stat"><div class="stat-top">${tr('Состояние')}</div><div class="stat-value">${esc(tr(({free:'Свободен',preparing:'Подготовка',ready:'Готов',down:'Недоступен'})[s.state]||s.state))}</div></div><div class="stat"><div class="stat-top">${tr('Активная аренда')}</div><strong>${esc(s.active_lease?.task||'—')}</strong><p>${esc(s.active_lease?.holder_role||'')} · ${esc(s.active_lease?.profile||'')}</p></div><div class="stat"><div class="stat-top">${tr('Очередь')}</div><div class="stat-value">${s.queue.length}</div></div></div>${s.down_reason?`<p class="notice">${esc(s.down_reason)}</p>`:''}<details><summary>${tr('Аренда, очередь и планировщик')}</summary><pre>${pretty({lease:s.active_lease,queue:s.queue,scheduler:data.scheduler})}</pre></details>`);
  if(!$('stand-machines').dataset.loaded){
   const rows=Object.entries(data.connections).filter(([k])=>k==='STAND_HOST'||k.startsWith('STAND_HOST_'));
   $('stand-machines').innerHTML=rows.map(([k,v])=>machineRow(k.slice(10).replace(/^_/,''),v,data.connections[k.replace('STAND_HOST','STAND_USER')]||'')).join('')||machineRow();$('stand-machines').dataset.loaded='true';$('stand-connections').dataset.revision=data.connection_revision;
   standFields();
  }
  setText($('stand-paths'),data.environment_path+' · SSH: '+data.ssh_config);
  setMarkup($('stand-profiles'),data.profile_error?`<p class="form-error">${esc(data.profile_error)}</p>`:data.profiles.map(p=>`<div class="evidence"><strong>${esc(p.name)}</strong><p>${esc(p.purpose)}</p><small>${esc(p.file)} · ${esc(p.environment)} · ${esc(p.used_for.join(', '))}</small></div>`).join(''));
  setMarkup($('stand-files'),data.files.map(name=>`<button class="button" data-stand-file="${esc(name)}">${esc(name)}</button>`).join(' '));
  const open=new Set([...$('stand-history').querySelectorAll('details[open]')].map(n=>n.id));
  setMarkup($('stand-history'),Object.values(data.operations).sort((a,b)=>b.at-a.at).map(o=>`<details id="stand-op-${esc(o.id)}"><summary>${esc(o.request.action)} · ${badge(o.status)} · ${when(o.at)}</summary><pre>${pretty(o.request)}</pre><pre>${esc(o.log||'')}</pre></details>`).join('')+Object.values(data.deployments).reverse().map(d=>`<details id="stand-deploy-${esc(d.id)}"><summary>${esc(d.lease.task)} · ${esc(d.lease.profile)} · ${badge(d.status)}</summary><button class="button" data-deploy-log="${esc(d.id)}">${tr('Журнал Ansible')}</button><pre>${pretty(d)}</pre></details>`).join('')+`<details id="stand-transitions"><summary>${tr('Переходы состояния')}</summary><pre>${pretty(s.history)}</pre></details>`);
  for(const id of open)if($(id))$(id).open=true;
 }finally{standLoading=false;}
}
async function submitStand(body){
 const key='greatminds-stand-request:'+state.data.project;let request;
 try{request=JSON.parse(localStorage.getItem(key))}catch{}
 if(!request||JSON.stringify(request.body)!==JSON.stringify(body))request={body,request_id:crypto.randomUUID()};
 localStorage.setItem(key,JSON.stringify(request));
 const result=await api('/api/stands/operations',{...body,request_id:request.request_id});localStorage.removeItem(key);await loadStands();return result;
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('button');if(!button)return;
 try{
  if(button.hasAttribute('data-stand-add'))$('stand-machines').insertAdjacentHTML('beforeend',machineRow());
  if(button.hasAttribute('data-machine-check')){const suffix=button.closest('.machine').querySelector('[data-machine-key]').value.trim();button.disabled=true;await submitStand({action:'check',machine_key:'STAND_HOST'+(suffix?'_'+suffix:'')});toast(tr('Проверяется сохранённый адрес машины.'));}
  if(button.hasAttribute('data-machine-remove'))button.closest('.machine').remove();
  if(button.dataset.standOp){button.disabled=true;await submitStand({action:button.dataset.standOp});}
  if(button.dataset.deployLog){const data=await api('/api/stands/deployment-output',{id:button.dataset.deployLog});$('detail-title').textContent=tr('Журнал Ansible');$('detail-body').innerHTML=Object.entries(data).map(([name,out])=>`<h3>${esc(name)}</h3><pre>${esc(out.text)}</pre>${out.truncated?tr('показан фрагмент'):''}`).join('')||tr('Вывод ещё не записан.');$('detail-dialog').showModal();}
  if(button.dataset.standFile){standEditor=await api('/api/stands/file',{name:button.dataset.standFile});$('stand-editor').classList.remove('hidden');$('stand-editor-name').textContent=standEditor.name;$('stand-file-text').value=standEditor.text;}
 }catch(e){toast(e.message);}finally{button.disabled=false;}
});
document.addEventListener('change',event=>{if(event.target.id==='stand-action')standFields();});
document.addEventListener('submit',async event=>{
 if(!['stand-connections','stand-editor','stand-operation'].includes(event.target.id))return;
 event.preventDefault();const submit=event.target.querySelector('button:not([type=button])');submit.disabled=true;
 try{
  if(event.target.id==='stand-connections'){
   const connections={};for(const row of document.querySelectorAll('.machine')){const suffix=row.querySelector('[data-machine-key]').value.trim();const key='STAND_HOST'+(suffix?'_'+suffix:'');if(key in connections)throw Error(tr('Суффиксы машин должны быть уникальны.'));connections[key]=row.querySelector('[data-machine-host]').value.trim();connections[key.replace('STAND_HOST','STAND_USER')]=row.querySelector('[data-machine-user]').value.trim();}
   const d=await api('/api/stands/connections',{connections,revision:event.target.dataset.revision});event.target.dataset.revision=d.connection_revision;toast(tr('Настройки сохранены.'));
  }else if(event.target.id==='stand-editor'){
   standEditor=await api('/api/stands/file',{...standEditor,text:$('stand-file-text').value});setText($('stand-file-status'),tr('Файл сохранён. Проверьте профили перед развёртыванием.'));
  }else{await submitStand(Object.fromEntries(new FormData(event.target)));setText($('stand-operation-status'),tr('Операция в очереди демона.'));}
 }catch(e){toast(e.message);}finally{submit.disabled=false;}
});
