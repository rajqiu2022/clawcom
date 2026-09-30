(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = {tab:'team', teamMode:'chat', taskMode:'plans', knowledgeMode:'entries', project:0, team:0, teams:[], projects:[], claws:[], me:null, room:null, teamIssue:'', chatTimer:null, busy:false};
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const short = (s, n=120) => String(s || '').length > n ? String(s).slice(0,n) + '…' : String(s || '');
  const date = (s) => s ? String(s).replace('T',' ').slice(0,16) : '—';
  const status = (s) => `<span class="pill ${['failed','blocked','offline','error','rejected'].includes(s) ? 'warn' : ['pending','inactive','expired'].includes(s) ? 'neutral' : ''}">${esc(s || '未知')}</span>`;
  const safeHref = (value) => {try {const url=new URL(value,location.origin);return url.origin===location.origin&&['http:','https:'].includes(url.protocol)?url.href:'#';}catch(_){return '#';}};
  const empty = (title, hint='') => `<div class="list-empty"><b>${esc(title)}</b>${esc(hint)}</div>`;
  const title = (eyebrow, heading, sub='') => `<div class="eyebrow">${eyebrow}</div><h1 class="page-title">${esc(heading)}</h1><p class="page-sub">${esc(sub)}</p>`;
  function toast(message) { const el=$('toast'); el.textContent=message; el.classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.classList.remove('show'),3200); }
  function dialog(heading, body) { $('detail-title').textContent=heading; $('detail-body').innerHTML=body; $('detail').showModal(); }
  function loading() { $('content').innerHTML='<div class="list-empty">正在同步 Hub…</div>'; }
  function api(path, opts={}) {
    return fetch('/api/v1'+path, {credentials:'same-origin', ...opts, headers:{...(opts.body ? {'Content-Type':'application/json'} : {}),...(opts.headers||{})}})
      .then(async response => {
        if (response.redirected && response.url.includes('/login')) { location.href='/login'; throw Error('请先登录'); }
        const data=await response.json().catch(()=>({}));
        if (response.status===401) { location.href='/login'; throw Error('登录已过期'); }
        if (!response.ok) { const error=Error(data.error || `请求失败 (${response.status})`);error.status=response.status;throw error; }
        return data;
      });
  }
  const get=(path)=>api(path);
  const post=(path, data, headers={})=>api(path,{method:'POST',body:JSON.stringify(data),headers});
  const put=(path, data)=>api(path,{method:'PUT',body:JSON.stringify(data)});
  function projectName() { return state.projects.find(p=>p.id===state.project)?.name || ''; }
  function teamName() { return state.teams.find(t=>t.id===state.team)?.name || ''; }
  function clawName(id) { return state.claws.find(c=>c.id===id)?.name || `Claw #${id}`; }
  function setTab(tab) {
    if (!['team','tasks','reports','knowledge','claws'].includes(tab)) return;
    state.tab=tab;
    document.querySelectorAll('.bottom-nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
    $('team-picker').hidden=!['team','tasks'].includes(tab);
    clearInterval(state.chatTimer); state.chatTimer=null;
    render();
  }
  function tabs(items, selected, attribute) {
    return `<div class="segmented" role="group">${items.map(([key,label])=>`<button type="button" data-${attribute}="${key}" class="${selected===key?'active':''}" aria-pressed="${selected===key}">${label}</button>`).join('')}</div>`;
  }
  async function loadTeams() {
    state.teams=[]; state.team=0; state.teamIssue='';
    if (state.project) {
      try {
        const result=await get(`/agent-teams?project_id=${state.project}&limit=100`);
        state.teams=result.items || [];
      } catch(error) {
        if (![403,404].includes(error.status)) throw error;
        state.teamIssue=error.message;
      }
      const saved=Number(localStorage.getItem('mobile-team-'+state.project));
      state.team=state.teams.find(t=>t.id===saved)?.id || state.teams[0]?.id || 0;
    }
    $('team').innerHTML=state.teams.map(t=>`<option value="${t.id}">${esc(t.name)}</option>`).join('') || '<option value="0">暂无团队</option>';
    $('team').value=String(state.team);
  }
  async function init() {
    loading();
    try {
      const [me,projects,claws]=await Promise.all([get('/auth/me'),get('/projects'),get('/openclaws')]);
      state.me=me; state.projects=projects; state.claws=claws;
      const saved=Number(localStorage.getItem('mobile-project'));
      const savedProject=projects.find(p=>p.id===saved);
      state.project=savedProject?.id || projects[0]?.id || 0;
      $('project').innerHTML=projects.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('') || '<option value="0">暂无可访问项目</option>';
      await loadTeams();
      if (!savedProject && !state.teams.length) {
        for (const project of projects.slice(1,10)) {
          state.project=project.id;
          await loadTeams();
          if (state.teams.length) break;
        }
        if (!state.teams.length) {state.project=projects[0]?.id||0;await loadTeams();}
        $('project').value=String(state.project);
      } else $('project').value=String(state.project);
      setTab('team');
    } catch(e) { $('content').innerHTML=empty('暂时无法连接 Hub',e.message); }
  }
  async function render() {
    loading();
    try {
      switch(state.tab) {
        case 'team': return await renderTeam();
        case 'tasks': return await renderTasks();
        case 'reports': return await renderReports();
        case 'knowledge': return await renderKnowledge();
        case 'claws': return await renderClaws();
      }
    } catch(e) { $('content').innerHTML=empty('加载失败',e.message); }
  }
  async function renderTeam() {
    const team=state.teams.find(t=>t.id===state.team);
    if (!team) { $('content').innerHTML=title('AGENT TEAMS','团队工作台',projectName())+empty('暂无团队',state.teamIssue || '请在 Hub 桌面版创建团队，或选择其他项目。'); return; }
    const members=(team.members||[]).map(m=>`<span class="person">${esc(clawName(m.claw_id))} · ${esc(m.role_key)}</span>`).join('');
    $('content').innerHTML=`${title('AGENT TEAMS','团队工作台',projectName())}<div class="hero"><div class="eyebrow">${esc(team.status)} / ${esc(projectName())}</div><h2>${esc(team.name)}</h2><p>${esc(team.objective || '团队正在协作中')}</p></div><div class="stats"><div class="stat"><strong>${(team.members||[]).length}</strong><span>成员</span></div><div class="stat"><strong>${state.teams.length}</strong><span>团队</span></div><div class="stat"><strong>${esc(clawName(team.primary_manager_claw_id)).slice(0,6)}</strong><span>主负责人</span></div></div>${tabs([['chat','团队频道'],['activity','成员动态']],state.teamMode,'team-mode')}<div id="team-panel"></div>`;
    document.querySelectorAll('[data-team-mode]').forEach(b=>b.onclick=()=>{state.teamMode=b.dataset.teamMode;clearInterval(state.chatTimer);state.chatTimer=null;renderTeam();});
    if (state.teamMode==='activity') {
      await renderActivity(team);
      if(state.tab==='team'&&state.teamMode==='activity'&&state.team===team.id)
        state.chatTimer=setInterval(()=>{if(state.tab==='team'&&state.teamMode==='activity'&&!document.hidden)renderActivity(team,true).catch(()=>{});},15000);
      return;
    }
    $('team-panel').innerHTML=`<div class="section-head"><h3>团队成员</h3></div><div class="roster">${members || '<span class="page-sub">暂无成员</span>'}</div><div class="section-head"><h3>团队频道</h3><button id="chat-reload" type="button">刷新</button></div><div id="chat-area" class="chat-panel">${empty('正在加载频道')}</div>`;
    $('chat-reload').onclick=()=>loadChat(false);
    await loadChat(false);
    if(state.tab==='team'&&state.teamMode==='chat'&&state.team===team.id)
      state.chatTimer=setInterval(()=>{ if (state.tab==='team' && state.teamMode==='chat' && !document.hidden) loadChat(true); },5000);
  }
  async function renderActivity(team, silent=false) {
    const data=await get(`/agent-teams/${team.id}/members/activity`);
    if (state.tab!=='team' || state.teamMode!=='activity' || team.id!==state.team || !$('team-panel')) return;
    const labels={idle:'空闲',working:'工作中',blocked:'阻塞',stale:'上报已过期',unknown:'尚未上报',completed:'已完成',failed:'失败',cancelled:'已取消'};
    const people=data.items||[];
    const html=`<div class="section-head"><h3>成员动态</h3><span class="page-sub">Hub 执行 / Agent 自报 · 15 秒刷新</span></div><div class="stats"><div class="stat"><strong>${people.filter(m=>m.effective_state==='working').length}</strong><span>工作中</span></div><div class="stat"><strong>${people.filter(m=>m.effective_state==='blocked').length}</strong><span>阻塞</span></div><div class="stat"><strong>${people.filter(m=>['stale','unknown'].includes(m.effective_state)).length}</strong><span>待确认</span></div></div>${people.map(m=>`<button type="button" class="activity-card" data-activity-member="${m.claw_id}"><span class="card-top"><strong>${esc(m.name)}</strong>${status(labels[m.effective_state]||m.effective_state)}</span><span class="activity-work">${esc(short(m.authoritative_execution?`Hub 执行 ${m.authoritative_execution.reference}`:(m.current_task?.title||m.summary||'暂无当前任务'),130))}</span>${m.current_task?.progress_percent!=null?`<span class="activity-progress"><span style="width:${Math.max(0,Math.min(100,Number(m.current_task.progress_percent)||0))}%"></span></span><small>${esc(m.current_task.progress_percent)}% · ${esc(short(m.current_task.progress_message,100))}</small>`:''}<small>${esc(m.source==='hub_authoritative_execution'?'Hub 执行记录':'Agent 自报')} · ${esc(m.connection_label||'')} · 上报 ${esc(date(m.reported_at))}</small></button>`).join('')||empty('暂无成员动态')}`;
    const panel=$('team-panel');
    if (!silent || panel.innerHTML!==html) panel.innerHTML=html;
    panel.querySelectorAll('[data-activity-member]').forEach(b=>b.onclick=()=>openActivityMember(team.id,Number(b.dataset.activityMember)));
  }
  async function openActivityMember(teamId, clawId) {
    dialog(clawName(clawId),empty('正在读取成员任务'));
    try {
      const data=await get(`/agent-teams/${teamId}/members/${clawId}/activity?limit=10`);
      if (!$('detail').open || teamId!==state.team) return;
      const m=data.member||{}, history=data.history||{}, reports=data.recent_reports||[];
      $('detail-title').textContent=m.name||clawName(clawId);
      $('detail-body').innerHTML=`<p>${status(({idle:'空闲',working:'工作中',blocked:'阻塞',stale:'上报已过期',unknown:'尚未上报'})[m.effective_state]||m.effective_state)} · ${esc(m.connection_label||'')}</p><h4>当前任务</h4>${m.current_task?`<div class="card"><b>${esc(m.authoritative_execution?`Hub 执行 ${m.authoritative_execution.reference}`:(m.current_task.title||'当前任务'))}</b><p>${esc(m.current_task.progress_message||m.summary||'')}</p>${m.current_task.progress_percent!=null?`<p>自报进度 ${esc(m.current_task.progress_percent)}%</p>`:''}</div>`:empty('暂无当前任务',m.summary||'')}<h4>最近上报</h4>${reports.slice(0,10).map(r=>`<div class="activity-event"><time>${esc(date(r.reported_at))}</time><p>${esc(short(r.task?.progress_message||r.summary||r.state,240))}</p></div>`).join('')||empty('暂无上报')}<h4>历史任务 · ${esc(history.total||0)}</h4>${(history.items||[]).map(t=>`<div class="activity-event"><b>${esc(t.title)}</b><p>${esc(t.status)} · ${esc(date(t.updated_at||t.finished_at))}</p></div>`).join('')||empty('暂无历史任务')}`;
    } catch(e) {if($('detail').open)$('detail-body').innerHTML=empty('成员动态读取失败',e.message);}
  }
  async function loadChat(silent) {
    if (!state.team || state.tab!=='team' || state.teamMode!=='chat') return;
    const teamId=state.team;
    try {
      const data=await get(`/agent-teams/${teamId}/chat-room?limit=100`);
      if (state.tab!=='team' || state.teamMode!=='chat' || state.team!==teamId || !document.querySelector('#chat-area')) return;
      state.room=data.room;
      const area=$('chat-area');
      const oldList=area.querySelector('.messages');
      const wasBottom=!oldList || oldList.scrollHeight-oldList.scrollTop-oldList.clientHeight<75;
      const messages=(data.messages||[]).map(m=>{
        const own=m.sender_member_id===data.room.my_member?.id;
        const images=(m.images||[]).map(i=>`<a href="${esc(i.url)}" target="_blank" rel="noopener">图片附件</a>`).join(' · ');
        return `<div class="message ${own?'own':''}"><div class="who">${esc(m.sender?.display_name || '成员')}</div><div class="bubble">${esc(m.content)}${images ? '<br>'+images : ''}</div><time>${esc(date(m.created_at))}</time></div>`;
      }).join('');
      if (!oldList) {
        area.innerHTML=`<div class="messages"></div><form class="composer" id="chat-form"><textarea id="chat-input" maxlength="20000" placeholder="给团队发消息…" aria-label="消息内容"></textarea><div class="send-row"><label class="mention"><input id="mention-all" type="checkbox"> @全部 Agent</label><button class="button accent" type="submit">发送消息 ↗</button></div></form>`;
        $('chat-form').onsubmit=sendChat;
      }
      const list=area.querySelector('.messages');
      if (list.innerHTML!==messages) list.innerHTML=messages || empty('暂无消息','发送第一条消息开始协作。');
      if (wasBottom) area.querySelector('.messages').scrollTop=area.querySelector('.messages').scrollHeight;
    } catch(e) { if (!silent) $('chat-area').innerHTML=empty('团队频道不可用',e.message); }
  }
  async function sendChat(event) {
    event.preventDefault(); if (state.busy) return;
    const content=$('chat-input').value.trim(), mention_all=$('mention-all').checked;
    if (!content) return;
    const team=state.team; state.busy=true;
    const button=event.currentTarget.querySelector('button[type=submit]'); button.disabled=true;
    try {
      const key=crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      await post(`/agent-teams/${team}/chat-room/messages`,{content,mention_all,mention_claw_ids:[],timeout_seconds:300},{'Idempotency-Key':key});
      if (team===state.team) { $('chat-input').value=''; await loadChat(false); }
    } catch(e) { toast('发送失败：'+e.message); }
    finally { state.busy=false; if (button.isConnected) button.disabled=false; }
  }
  async function renderTasks() {
    const team=state.teams.find(t=>t.id===state.team);
    if (!team) { $('content').innerHTML=title('MISSION CONTROL','团队任务',projectName())+empty('暂无团队'); return; }
    if (state.taskMode==='plans') {
      const data=await get(`/agent-teams/${team.id}/test-plans?period=all&limit=24`);
      const cards=(data.items||[]).map(p=>`<div class="card"><div class="card-top"><h3>${esc(p.name)}</h3>${status(p.status)}</div><p>${esc(p.start_date)} — ${esc(p.end_date)} · ${p.completed_tasks}/${p.total_tasks} 已完成</p>${(p.tasks||[]).map(t=>`<button class="row-button" data-plan-task="${p.id}:${t.id}"><div class="card" style="box-shadow:none;margin:8px 0;background:#f7f8f4"><div class="card-top"><h3>${esc(t.name)}</h3>${status(t.status)}</div><div class="meta"><span>${esc(t.assignee)}</span><span>${esc(t.end_date||'未排期')}</span></div></div></button>`).join('')||'<p>当前暂无任务</p>'}</div>`).join('');
      $('content').innerHTML=title('MISSION CONTROL','团队任务',`${team.name} · ${data.summary?.total||0} 项`)+`<div class="segmented"><button class="active" data-task-mode="plans" type="button">测试计划</button><button data-task-mode="missions" type="button">工作流</button></div><div class="stats"><div class="stat"><strong>${data.summary?.total||0}</strong><span>全部</span></div><div class="stat"><strong>${data.summary?.in_progress||0}</strong><span>执行中</span></div><div class="stat"><strong>${data.summary?.blocked||0}</strong><span>阻塞</span></div></div>${cards||empty('暂无团队任务','可在 Hub 桌面版创建或关联测试计划。')}`;
      document.querySelectorAll('[data-plan-task]').forEach(b=>b.onclick=()=>{const [pid,tid]=b.dataset.planTask.split(':').map(Number);const p=data.items.find(x=>x.id===pid),t=p.tasks.find(x=>x.id===tid);dialog(t.name,`<p>${status(t.status)} · ${esc(t.assignee)}</p><p>${esc(t.description||'暂无描述')}</p><p>进度：${esc(t.progress)}% · 截止：${esc(t.end_date||'未排期')}</p><p>关联报告：${esc(t.report_count||0)} 份</p><a href="${esc(p.url)}">在 Hub 查看计划详情 ↗</a>`);});
      document.querySelectorAll('[data-task-mode]').forEach(b=>b.onclick=()=>{state.taskMode=b.dataset.taskMode;renderTasks();});
      return;
    }
    const data=await get(`/agent-teams/${team.id}/missions?limit=100`);
    const rows=(data.items||[]).map(m=>`<div class="card"><button class="row-button" data-mission="${m.id}"><div class="card-top"><h3>${esc(short(m.objective,90))}</h3>${status(m.effective_status||m.status)}</div><p>${esc(m.main_claw_name || clawName(m.main_claw_id))}</p><div class="meta"><span>任务 #${m.id}</span><span>${(m.stages||[]).length} 个阶段</span><span>${esc(date(m.updated_at))}</span></div></button></div>`).join('');
    $('content').innerHTML=title('MISSION CONTROL','团队任务',`${team.name} · ${data.total||0} 项`)+`<div class="segmented"><button data-task-mode="plans" type="button">测试计划</button><button class="active" data-task-mode="missions" type="button">工作流</button></div><div class="stats"><div class="stat"><strong>${data.total||0}</strong><span>全部</span></div><div class="stat"><strong>${(data.items||[]).filter(m=>m.effective_status==='active').length}</strong><span>执行中</span></div><div class="stat"><strong>${(data.items||[]).filter(m=>m.effective_status==='completed').length}</strong><span>已完成</span></div></div>${rows||empty('暂无团队任务','可在 Hub 桌面版创建工作流任务。')}`;
    document.querySelectorAll('[data-task-mode]').forEach(b=>b.onclick=()=>{state.taskMode=b.dataset.taskMode;renderTasks();});
    document.querySelectorAll('[data-mission]').forEach(b=>b.onclick=()=>{
      const m=data.items.find(x=>x.id===Number(b.dataset.mission));
      dialog('任务 #'+m.id,`<p>${esc(m.objective)}</p><p>负责人：${esc(m.main_claw_name || clawName(m.main_claw_id))}</p><p>状态：${esc(m.effective_status||m.status)}</p><h4>执行阶段</h4>${(m.stages||[]).map(s=>`<div class="card"><b>${esc(s.stage_key)}</b> ${status(s.state)}<p>${esc(clawName(s.assigned_claw_id))}</p></div>`).join('')||empty('暂无阶段')}<a href="/agent-teams?team_id=${team.id}">在 Hub 查看完整任务</a>`);
    });
  }
  async function renderReports() {
    const query=projectName() ? '?project='+encodeURIComponent(projectName()) : '';
    const rows=await get('/reports'+query);
    $('content').innerHTML=title('DAILY SIGNAL','Hub 报告',`${projectName()} · 最近 7 天`)+((rows||[]).map(r=>`<div class="card"><button class="row-button" data-report="${r.id}"><div class="card-top"><h3>${esc(r.openclaw_name || clawName(r.openclaw_id))}</h3><span class="pill">${esc(r.report_date)}</span></div><p>${esc(short(r.ai_summary || r.tasks_completed || '查看当天工作报告'))}</p><div class="meta"><span>${esc(r.openclaw_role_title||'Agent')}</span><span>${esc(r.report_time||'')}</span></div></button></div>`).join('')||empty('暂无报告','近期没有新的工作报告。'));
    document.querySelectorAll('[data-report]').forEach(b=>b.onclick=()=>{
      const r=rows.find(x=>x.id===Number(b.dataset.report));
      dialog(`${r.openclaw_name || 'Claw'} · ${r.report_date}`,['ai_summary','tasks_completed','knowledge_recorded','experience_shared','knowledge_learned'].map((k,i)=>`<h4>${['AI 总结','完成任务','记录知识','经验分享','学习内容'][i]}</h4><pre>${esc(r[k]||'—')}</pre>`).join(''));
    });
  }
  function bindKnowledgeModes() {
    document.querySelectorAll('[data-knowledge-mode]').forEach(b=>b.onclick=()=>{state.knowledgeMode=b.dataset.knowledgeMode;renderKnowledge();});
  }
  function knowledgeModes() {return state.team?tabs([['entries','项目知识'],['shared','团队共享']],state.knowledgeMode,'knowledge-mode'):'';}
  async function renderKnowledge(search='') {
    if(state.knowledgeMode==='shared'&&state.team)return renderSharedResources();
    const params=new URLSearchParams(); if (state.project) params.set('project',projectName()); if (search) params.set('search',search);
    const rows=await get('/knowledge?'+params.toString());
    $('content').innerHTML=title('KNOWLEDGE BASE','知识库',projectName())+knowledgeModes()+`<form id="knowledge-search" class="toolbar"><input name="query" placeholder="搜索知识标题或内容" value="${esc(search)}"><button class="button" type="submit">搜索</button></form><div class="section-head"><h3>知识条目</h3><button id="knowledge-add" type="button">+ 新建</button></div>${(rows||[]).map(k=>`<div class="card"><button class="row-button" data-knowledge="${k.id}"><div class="card-top"><h3>${esc(k.title)}</h3>${status(k.status)}</div><p>${esc(short(k.content))}</p><div class="meta"><span>${esc(k.category||'通用')}</span><span>${esc(date(k.updated_at||k.created_at))}</span></div></button></div>`).join('')||empty('暂无知识','试试其他搜索词，或创建第一条知识。')}`;
    bindKnowledgeModes();
    $('knowledge-search').onsubmit=e=>{e.preventDefault(); renderKnowledge(e.currentTarget.elements.namedItem('query').value.trim()).catch(err=>toast(err.message));};
    $('knowledge-add').onclick=()=>dialog('新建知识',`<form id="knowledge-form"><label class="field">标题<input name="title" maxlength="255" required></label><label class="field">内容<textarea name="content" required></textarea></label><button class="button accent" type="submit">保存知识</button></form>`);
    document.querySelectorAll('[data-knowledge]').forEach(b=>b.onclick=()=>{
      const k=rows.find(x=>x.id===Number(b.dataset.knowledge));
      dialog(k.title,`<p>${status(k.status)} · ${esc(k.category||'通用')} · ${esc(date(k.updated_at||k.created_at))}</p><pre>${esc(k.content)}</pre><button class="button ghost" id="favorite" type="button">${k.is_favorite?'取消收藏':'收藏'}</button>`);
      $('favorite').onclick=async()=>{try{await api(`/knowledge/${k.id}/favorite`,{method:k.is_favorite?'DELETE':'POST'});$('detail').close();toast('已更新收藏');renderKnowledge(search);}catch(e){toast(e.message);}};
    });
  }
  async function renderSharedResources() {
    const teamId=state.team;
    const data=await get(`/agent-teams/${teamId}/shared-resources`);
    if(state.tab!=='knowledge'||state.team!==teamId)return;
    const group=(kind,heading,items)=>`<div class="section-head"><h3>${heading} · ${items.length}</h3>${data.can_manage?`<button type="button" data-resource-add="${kind}">+ 添加已有</button>`:''}</div>${items.map(item=>`<div class="card resource-card"><div class="card-top"><h3><a href="${esc(safeHref(item.web_url))}">${esc(item.title||item.name)} ↗</a></h3>${status(kind==='knowledge'?item.status:item.review_status)}</div><p>${esc(kind==='knowledge'?(item.module_name||item.category||item.entry_type||'知识'):(item.category||item.scope||'Skill'))}</p><div class="meta"><span>#${item.id}</span><span>更新 ${esc(date(item.updated_at))}</span>${item.revision?`<span>修订 ${esc(item.revision)}</span>`:''}</div>${data.can_manage?`<button type="button" class="text-action" data-resource-unlink="${kind}:${item.id}">移出团队</button>`:''}</div>`).join('')||empty('暂无共享资源','可从现有资源库添加。')}`;
    $('content').innerHTML=title('TEAM RESOURCES','团队共享',teamName())+knowledgeModes()+`<p class="page-sub">团队共享的是现有资源入口，内容由 Hub 知识库和 Skill 市场维护。</p>${group('knowledge','共享知识',data.knowledge||[])}${group('skills','共享 Skill',data.skills||[])}`;
    bindKnowledgeModes();
    document.querySelectorAll('[data-resource-add]').forEach(b=>b.onclick=()=>openResourcePicker(teamId,b.dataset.resourceAdd));
    document.querySelectorAll('[data-resource-unlink]').forEach(b=>b.onclick=async()=>{
      const [kind,id]=b.dataset.resourceUnlink.split(':');
      if(!confirm('将该资源移出团队共享区？原内容不会删除。'))return;
      try {b.disabled=true;await api(`/agent-teams/${teamId}/shared-resources/${kind}/${id}`,{method:'DELETE'});toast('已移出团队');renderSharedResources();}
      catch(e){toast(e.message);b.disabled=false;}
    });
  }
  async function openResourcePicker(teamId,kind) {
    dialog(`添加团队共享${kind==='knowledge'?'知识':'Skill'}`,empty('正在读取现有资源'));
    try {
      const data=await get(`/agent-teams/${teamId}/shared-resources/options`);
      if(!$('detail').open||teamId!==state.team)return;
      const options=kind==='knowledge'?data.knowledge||[]:data.skills||[];
      $('detail-body').innerHTML=`<input id="resource-search" class="dialog-search" placeholder="搜索名称或 ID" aria-label="搜索资源"><div id="resource-options"></div>`;
      const draw=()=>{
        const q=$('resource-search').value.trim().toLowerCase();
        const rows=options.filter(x=>!q||`${x.id} ${x.title} ${x.name||''}`.toLowerCase().includes(q)).slice(0,50);
        $('resource-options').innerHTML=rows.map(x=>`<div class="resource-option"><span><b>#${x.id} ${esc(x.title||x.name)}</b><small>${esc(x.category||x.module_name||'')}</small></span><button type="button" class="button sm ${x.linked?'ghost':''}" data-resource-link="${x.id}" ${x.linked?'disabled':''}>${x.linked?'已添加':'添加'}</button></div>`).join('')||empty('没有匹配的资源');
        $('resource-options').querySelectorAll('[data-resource-link]').forEach(b=>b.onclick=async()=>{
          try {b.disabled=true;await post(`/agent-teams/${teamId}/shared-resources/${kind}`,{resource_id:Number(b.dataset.resourceLink)});$('detail').close();toast('已添加到团队');renderSharedResources();}
          catch(e){toast(e.message);b.disabled=false;}
        });
      };
      $('resource-search').oninput=draw;draw();
    } catch(e){if($('detail').open)$('detail-body').innerHTML=empty('资源读取失败',e.message);}
  }
  async function renderClaws() {
    const rows=state.claws.filter(c=>!state.project || c.project_id===state.project || (!c.project_id && c.project_name===projectName()));
    $('content').innerHTML=title('CLAW DIRECTORY','Claw 管理',`${projectName()} · ${rows.length} 个实例`)+`${rows.map(c=>`<div class="card"><button class="row-button" data-claw="${c.id}"><div class="card-top"><h3>${esc(c.name)}</h3>${status(c.status)}</div><p>${esc(c.role_title||c.responsibilities||'OpenClaw 实例')}</p><div class="meta"><span>#${c.id}</span><span>${esc(c.agent_type||'Agent')}</span><span>${esc(c.connection_mode||'')}</span></div></button></div>`).join('')||empty('暂无 Claw','此项目还没有可查看的 Claw。')}`;
    document.querySelectorAll('[data-claw]').forEach(b=>b.onclick=()=>{
      const c=rows.find(x=>x.id===Number(b.dataset.claw));
      dialog(c.name,`<p>${status(c.status)} · ${esc(c.agent_type||'Agent')} · ID #${c.id}</p><p>${esc(c.responsibilities||'暂无职责描述')}</p><form id="claw-form" data-id="${c.id}"><label class="field">名称<input name="name" maxlength="100" value="${esc(c.name)}" required></label><label class="field">职位<input name="role_title" maxlength="100" value="${esc(c.role_title||'')}"></label><button class="button accent" type="submit">保存信息</button></form><p><a href="/openclaws/${c.id}">在 Hub 查看完整设置 ↗</a></p>`);
    });
  }
  document.addEventListener('submit',async e=>{
    if (e.target.id==='knowledge-form') {e.preventDefault();const f=e.target;const data={title:f.elements.namedItem('title').value.trim(),content:f.elements.namedItem('content').value.trim(),scope:state.project?'project':'global',project_id:state.project||null};if(!data.title||!data.content)return;try{f.querySelector('button').disabled=true;await post('/knowledge',data);$('detail').close();toast('知识已保存');renderKnowledge();}catch(err){toast(err.message);f.querySelector('button').disabled=false;}}
    if (e.target.id==='claw-form') {e.preventDefault();const f=e.target;const id=Number(f.dataset.id);try{f.querySelector('button').disabled=true;const updated=await put(`/openclaws/${id}`,{name:f.elements.namedItem('name').value.trim(),role_title:f.elements.namedItem('role_title').value.trim()});const ix=state.claws.findIndex(c=>c.id===id);if(ix>=0)state.claws[ix]={...state.claws[ix],...updated};$('detail').close();toast('Claw 信息已更新');renderClaws();}catch(err){toast(err.message);f.querySelector('button').disabled=false;}}
  });
  $('project').onchange=async e=>{state.project=Number(e.target.value);localStorage.setItem('mobile-project',state.project);try{await loadTeams();render();}catch(err){toast(err.message);}};
  $('team').onchange=e=>{state.team=Number(e.target.value);localStorage.setItem('mobile-team-'+state.project,state.team);render();};
  document.querySelectorAll('.bottom-nav button').forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
  $('refresh').onclick=async()=>{try{state.claws=await get('/openclaws');await loadTeams();render();toast('已刷新');}catch(e){toast(e.message);}};
  $('detail-close').onclick=()=>$('detail').close();
  init();
})();
