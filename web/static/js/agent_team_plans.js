/* Read-only schedule projection; explicit authoring never starts a Run. */
const AgentTeamPlans = (() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const labels = {draft:'草稿',active:'进行中',completed:'已完成',archived:'归档',assigned:'已指派',pending:'待开始',in_progress:'执行中',blocked:'阻塞',skipped:'已跳过'};
    let team = null, epoch = 0, requestId = 0, period = 'week', day = '', offset = 0, mode = 'new', saving = false;
    function today() { return new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date()); }
    function reset() { ++epoch; ++requestId; team = null; $('at-plan-dialog').close(); }
    function frame() {
        $('at-plans-root').innerHTML = `<section class="at-plan-workspace"><header class="at-plan-toolbar"><div><span class="at-eyebrow">TEAM SCHEDULE</span><h3>测试计划与任务</h3></div><div><button type="button" class="btn btn-secondary btn-sm" id="at-plan-link" hidden>关联已有计划</button> <button type="button" class="btn btn-primary btn-sm" id="at-plan-create" hidden>＋ 新建计划</button></div></header>
        <div class="at-plan-filters"><div role="group" aria-label="排期范围">${[['day','每日'],['week','每周'],['all','全部']].map(([key,label])=>`<button type="button" data-period="${key}" aria-pressed="${key===period}">${label}</button>`).join('')}</div><label>基准日期 <input id="at-plan-date" type="date" class="form-input" value="${day}" ${period==='all'?'disabled':''}></label><button id="at-plan-today" type="button" class="btn btn-ghost btn-sm">回到今天</button><button id="at-plan-refresh" type="button" class="btn btn-secondary btn-sm">刷新</button></div>
        <p id="at-plan-range" class="at-help"></p><div id="at-plan-summary" class="at-plan-summary"></div><p id="at-plan-note" class="at-help" role="status"></p><div id="at-plan-cards" class="at-plan-grid"></div><div id="at-plan-pages" class="at-pager"></div>
        <p class="at-help">按实际测试任务排期交集展示（北京时间，周一至周日），完成比例不含任务链。跨日任务会在对应日期出现；未排期任务请在“全部”查看。日期筛选不代表重复执行或自动调度。</p></section>`;
    }
    function badge(status) { return `<span class="at-plan-state ${Object.hasOwn(labels,status)?status:''}">${esc(labels[status] || status)}</span>`; }
    function supervision(p) {
        if (p.status !== 'active') return '';
        const s = p.supervision;
        if (!s) return '<p class="at-help at-plan-alert">监管未启动 · 尚无 Supervisor/Mission</p>';
        if (!s.manager_lease_active && !['waiting','stopped'].includes(s.status)) return `<p class="at-help at-plan-alert">监管 ${esc(s.status)} · 经理任期无效 · Mission ${s.mission_id ? '#'+esc(s.mission_id) : '未创建'}</p>`;
        if (!s.manager_lease_active) return `<p class="at-help">监管等待唤醒 · Mission #${esc(s.mission_id)}${s.next_check_at?' · 下次 '+esc(s.next_check_at):''}</p>`;
        return `<p class="at-help">监管 ${esc(s.status)} · 经理 #${esc(s.orchestrator_claw_id)} · Mission #${esc(s.mission_id)}${s.next_check_at?' · 下次 '+esc(s.next_check_at):''}</p>`;
    }
    function task(t) {
        return `<li><div class="at-plan-task-title"><span>${esc(t.name)}</span>${badge(t.status)}</div><p class="at-help">${esc(t.priority)} · ${esc(t.assignee)} · ${esc(t.start_date || t.end_date || '未排期')}${t.end_date && t.end_date!==t.start_date?' → '+esc(t.end_date):''}${t.overdue?' · <span class="at-plan-alert">已逾期</span>':''}</p><div class="at-progress"><progress max="100" value="${t.progress}" aria-label="任务上报进度"></progress><span>${t.progress}%</span></div></li>`;
    }
    async function load(page = 0) {
        if (!team) return;
        const generation = epoch, turn = ++requestId, id = team.id;
        offset = page;
        $('at-plan-note').textContent = '正在读取测试计划…';
        $('at-plan-cards').setAttribute('aria-busy','true');
        try {
            const data = await API.get(`/agent-teams/${id}/test-plans?period=${period}&date=${encodeURIComponent(day)}&limit=6&offset=${page}`);
            if (generation!==epoch || turn!==requestId) return;
            $('at-plan-create').hidden = !data.can_manage || team.status!=='active';
            $('at-plan-link').hidden = !data.can_manage || team.status!=='active';
            $('at-plan-range').textContent = period==='all'?'全部排期（含归档计划）':`${data.start_date} — ${data.end_date} · ${period==='week'?'周排期':'日排期'}`;
            const s = data.summary;
            $('at-plan-summary').innerHTML = [['范围内任务',s.total],['执行中',s.in_progress],['已完成',s.completed],['阻塞',s.blocked],['已逾期',s.overdue]].map(([label,count])=>`<div><strong>${count}</strong><span>${label}</span></div>`).join('');
            $('at-plan-cards').innerHTML = data.items.length ? data.items.map(p=>`<article class="at-plan-card"><header><span class="at-eyebrow">PLAN #${p.id}</span>${badge(p.status)}</header><h4><a href="${esc(p.url)}">${esc(p.name)} ↗</a></h4><p class="at-help">${esc(p.start_date)} → ${esc(p.end_date)}</p>${supervision(p)}<div class="at-progress"><progress max="100" value="${p.progress}" aria-label="计划任务完成比例"></progress><span>${p.completed_tasks}/${p.total_tasks} 已完成</span></div><div class="at-plan-section">${period==='all'?'全部任务':'当前范围任务'} <strong>${p.period_tasks}</strong>${p.unscheduled_tasks?`<span>另有 ${p.unscheduled_tasks} 项未排期</span>`:''}</div><ul>${p.tasks.map(task).join('') || '<li class="at-help">当前范围暂无已排期任务</li>'}</ul><footer><span class="at-help">${p.period_tasks>p.tasks.length?`展示前 ${p.tasks.length} 项，共 ${p.period_tasks} 项`: '进度来自测试计划记录'}</span><a href="${esc(p.url)}">查看计划详情 →</a></footer></article>`).join('') : '<div class="at-empty">当前范围暂无团队计划。<br>可切换日期、“全部”，或由测试经理新建 / 关联已有计划。</div>';
            $('at-plan-pages').innerHTML = `<span class="at-muted">${data.total?page+1:0}–${page+data.items.length} / ${data.total} 个计划</span><button type="button" class="btn btn-secondary btn-sm" data-plan-page="${Math.max(0,page-6)}" ${page===0?'disabled':''}>上一页</button><button type="button" class="btn btn-secondary btn-sm" data-plan-page="${page+6}" ${page+data.items.length>=data.total?'disabled':''}>下一页</button>`;
            $('at-plan-note').textContent = data.can_manage?'可创建草稿计划，或将同项目已有计划关联到本团队。':'计划由团队测试经理或项目管理员维护。';
        } catch (error) {
            if (generation!==epoch || turn!==requestId) return;
            $('at-plan-create').hidden = $('at-plan-link').hidden = true;
            $('at-plan-cards').replaceChildren(); $('at-plan-summary').replaceChildren(); $('at-plan-pages').replaceChildren();
            $('at-plan-note').textContent = `读取失败：${error.message || '请稍后重试'}，请点击刷新。`;
        } finally { if(generation===epoch && turn===requestId) $('at-plan-cards').removeAttribute('aria-busy'); }
    }
    async function open(kind) {
        if (!team || saving) return;
        mode = kind; const generation = epoch;
        $('at-plan-form').reset(); $('at-plan-error').hidden = true;
        $('at-plan-dialog-title').textContent = kind==='new'?'新建测试计划':'关联已有测试计划';
        $('at-plan-team-label').textContent = `${team.name} · 团队 #${team.id}`;
        $('at-plan-save').textContent = kind==='new'?'保存草稿':'确认关联';
        $('at-plan-new-fields').hidden = $('at-plan-new-fields').disabled = kind!=='new';
        $('at-plan-link-fields').hidden = $('at-plan-link-fields').disabled = kind==='new';
        $('at-plan-start').value = $('at-plan-end').value = today();
        $('at-plan-save').disabled = kind==='link';
        $('at-plan-dialog').showModal();
        if(kind==='new') { $('at-plan-name').focus(); return; }
        $('at-plan-existing').innerHTML = '<option value="">正在读取…</option>';
        try {
            const plans = await API.get(`/test-plans?project_id=${team.project_id}`);
            if(generation!==epoch || !$('at-plan-dialog').open) return;
            const choices = plans.filter(p=>p.project_id===team.project_id && !p.team_id);
            $('at-plan-existing').innerHTML = '<option value="">选择计划</option>'+choices.map(p=>`<option value="${p.id}">#${p.id} ${esc(p.name)} · ${esc(p.start_date)}～${esc(p.end_date)}</option>`).join('');
            $('at-plan-save').disabled = !choices.length;
            if(!choices.length) throw new Error('当前项目没有可关联的计划');
        } catch(error) { if(generation===epoch) { $('at-plan-error').textContent = error.message; $('at-plan-error').hidden=false; } }
    }
    $('at-plan-form').addEventListener('submit', async event=>{
        event.preventDefault(); if(saving || !team) return;
        const generation=epoch, id=team.id;
        saving=true; $('at-plan-save').disabled=true;
        try {
            if(mode==='new') {
                await API.post(`/agent-teams/${id}/test-plans`,{name:$('at-plan-name').value.trim(),description:$('at-plan-desc').value.trim(),start_date:$('at-plan-start').value,end_date:$('at-plan-end').value,status:'draft'});
            } else {
                const planId=Number($('at-plan-existing').value); if(!planId) throw new Error('请选择计划');
                await API.put(`/test-plans/${planId}`,{team_id:id});
            }
            if(generation!==epoch) return;
            $('at-plan-dialog').close(); period='all'; frame(); await load();
        } catch(error) { if(generation===epoch) { $('at-plan-error').textContent=error.message; $('at-plan-error').hidden=false; } }
        finally { saving=false; $('at-plan-save').disabled=false; }
    });
    $('at-plan-close').addEventListener('click',()=>{if(!saving)$('at-plan-dialog').close();});
    $('at-plan-dialog').addEventListener('cancel',event=>{if(saving)event.preventDefault();});
    function tab(value) {
        ['members','plans'].forEach(key=>{const selected=key===value; $(`at-tab-${key}`).setAttribute('aria-selected',String(selected)); $(`at-tab-${key}`).tabIndex=selected?0:-1; $(`at-${key}-panel`).hidden=!selected;});
        if(value==='members') AgentTeamActivity.refresh(true);
    }
    $('at-detail').addEventListener('keydown',event=>{if(event.target.matches('[data-team-tab]') && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?'members':event.key==='End'?'plans':event.target.dataset.teamTab==='members'?'plans':'members';tab(next);$(`at-tab-${next}`).focus();}});
    $('at-detail').addEventListener('change',event=>{if(event.target.id==='at-plan-date' && event.target.value){day=event.target.value;load();}});
    $('at-detail').addEventListener('click',event=>{
        const button=event.target.closest('button'); if(!button || button.disabled) return;
        if(button.dataset.teamTab) tab(button.dataset.teamTab);
        if(button.dataset.period){period=button.dataset.period;frame();load();}
        if(button.dataset.planPage) load(Number(button.dataset.planPage));
        if(button.id==='at-plan-refresh') load(offset);
        if(button.id==='at-plan-today'){day=today();frame();load();}
        if(button.id==='at-plan-create') open('new');
        if(button.id==='at-plan-link') open('link');
    });
    return {reset, mount:async value=>{team=value;period='week';day=today();frame();await load();}};
})();
