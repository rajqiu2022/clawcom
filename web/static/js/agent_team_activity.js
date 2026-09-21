/* Display-only polling. Reports are accepted from the member's own Agent token, never this UI. */
window.AgentTeamActivity = (() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const labels = {idle:'空闲', working:'工作中', blocked:'阻塞', unknown:'尚未上报', stale:'上报已过期',
        completed:'已完成（自报）', failed:'失败（自报）', cancelled:'已取消（自报）'};
    const roles = {primary_manager:'测试经理 · 主', backup_manager:'测试经理 · 备用', code_analyst:'代码分析员', test_executor:'测试执行员'};
    const specialties = {editor:'编辑器', mobile_package:'手机包', client_performance:'客户端性能'};
    const kinds = {flow:'Flow 执行', bug_regression:'Bug 回归', code_analysis:'代码分析', other:'其他任务'};
    let teamId = null, epoch = 0, timer = null, loading = false, members = [], selected = null, offset = 0, detailEpoch = 0;
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const label = state => labels[state] || '未知';
    const time = value => value ? esc(String(value).split('.')[0]) : '—';
    function badge(state) { return `<span class="at-work-state ${Object.hasOwn(labels, state) ? state : 'unknown'}">${label(state)}</span>`; }
    function progress(task) {
        const value = task?.progress_percent;
        return Number.isInteger(value) && value >= 0 && value <= 100 ? `<div class="at-progress"><progress max="100" value="${value}" aria-label="Agent 自报进度 ${value}%"></progress><span>${value}%</span></div>` : '<p class="at-help">未上报百分比</p>';
    }
    function renderCards() {
        const container = $('at-activity-cards');
        if (!container) return;
        container.innerHTML = members.length ? members.map(m => `<button type="button" class="at-agent-card" data-member="${m.claw_id}" aria-label="查看 ${esc(m.name)} 当前任务和历史">
            <span class="at-agent-top"><span class="at-agent-avatar" aria-hidden="true">${esc([...m.name][0] || 'A')}</span><span class="at-agent-name">${esc(m.name)}<small>Claw #${m.claw_id}</small></span>${badge(m.effective_state)}</span>
            <span class="at-agent-roles">${m.roles.map(r=>esc(roles[r] || r)).join(' · ')}</span>
            <span class="at-agent-specialties">${m.specialties.map(s=>`<span class="at-tag">${esc(specialties[s] || s)}</span>`).join('')}</span>
            <span class="at-agent-work">${esc(m.current_task?.title || m.summary || (m.effective_state === 'unknown' ? '等待 Agent 首次上报' : '无当前任务'))}</span>
            ${m.current_task ? progress(m.current_task) : ''}
            <span class="at-agent-message">${esc(m.current_task ? (m.current_task.progress_message || m.summary) : '')}</span>
            <span class="at-agent-connection"><i class="${m.connection === 'recently_seen' ? 'seen' : ''}"></i>${esc(m.connection_label)}</span>
            <span class="at-agent-footer"><span>上报：${time(m.reported_at)}</span><span>任务详情 ↗</span></span></button>`).join('') : '<p class="at-empty">暂无有效成员，请检查团队配置。</p>';
    }
    function schedule() {
        clearTimeout(timer);
        if (teamId && !document.hidden && !$('at-members-panel')?.hidden) timer = setTimeout(refresh, 15000);
    }
    async function refresh(force = false) {
        if (!teamId || loading || (document.hidden && !force)) return;
        const generation = epoch, id = teamId;
        loading = true;
        try {
            const result = await API.get(`/agent-teams/${id}/members/activity`);
            if (generation !== epoch) return;
            const focused = $('at-activity-cards')?.contains(document.activeElement) ? document.activeElement.dataset.member : null;
            members = result.items; renderCards();
            if (focused) $('at-activity-cards')?.querySelector(`[data-member="${Number(focused)}"]`)?.focus({preventScroll:true});
            $('at-activity-note').textContent = '每 15 秒刷新展示 · 仅反映本团队自报情况，不代表 Agent 全局执行容量';
            if ($('at-activity-dialog').open && selected) await loadDetail(selected, offset, true);
        } catch (error) {
            if (generation !== epoch) return;
            members = members.map(m=>({...m,effective_state:m.effective_state === 'unknown' ? 'unknown' : 'stale',connection:'unknown',connection_label:'连接状态未刷新'}));
            renderCards();
            $('at-activity-note').textContent = '状态刷新失败，以下为旧快照；不能据此判断是否空闲。' + (error.message || '');
        } finally {
            if (generation === epoch) { loading = false; schedule(); }
        }
    }
    function taskBlock(task) {
        return `<article class="at-task-log"><div class="at-task-heading"><strong>${esc(task.title)}</strong>${badge(task.status)}</div><p class="at-help">${esc(kinds[task.task_type] || task.task_type)} · ${esc(task.reference || task.task_key)}</p>${progress(task)}<p class="at-task-message">${esc(task.progress_message)}</p><p class="at-help">开始 ${time(task.started_at)} · 更新 ${time(task.updated_at)}${task.finished_at ? ' · 结束 ' + time(task.finished_at) : ''}</p></article>`;
    }
    async function loadDetail(clawId, page = 0, background = false) {
        const generation = epoch, requestId = ++detailEpoch, id = teamId;
        selected = clawId; offset = page;
        const dialog = $('at-activity-dialog'), body = $('at-activity-body');
        if (!background) {
            $('at-activity-title').textContent = members.find(m=>m.claw_id===clawId)?.name || `Claw #${clawId}`;
            body.innerHTML = '<p class="at-empty">正在读取任务记录…</p>';
            if (!dialog.open) dialog.showModal();
        }
        try {
            const data = await API.get(`/agent-teams/${id}/members/${clawId}/activity?limit=10&offset=${page}`);
            if (generation !== epoch || requestId !== detailEpoch || !dialog.open) return;
            const m = data.member, h = data.history, scroll = body.scrollTop;
            body.innerHTML = `<p class="at-warning">以下内容由 Agent 自行上报，不作为 Flow 完成、Bug 修复或业务验收的权威结论。</p>
                <div class="at-activity-summary">${badge(m.effective_state)}<span>${esc(m.connection_label)}</span><span>最后上报 ${time(m.reported_at)}</span></div>
                ${m.effective_state === 'stale' ? '<p class="at-warning">状态已过期，下面保留最后任务进展，不能据此认定任务已停止。</p>' : ''}
                <h3>当前任务</h3>${m.current_task ? taskBlock(m.current_task) : `<p class="at-empty">${esc(m.summary || (m.effective_state === 'unknown' ? '尚未接入状态上报' : '暂无当前任务'))}</p>`}
                <h3>最近上报 <small>最多 20 条${m.current_task ? ' · 当前任务' : ''}</small></h3><ol class="at-report-timeline">${data.recent_reports.map(r=>`<li><time>${time(r.reported_at)}</time><span>${esc(r.task?.progress_message || r.summary || label(r.state))}</span><small>${esc(r.task?.title || '空闲上报')}${r.task?.progress_percent != null ? ' · ' + esc(r.task.progress_percent) + '%' : ''}</small></li>`).join('') || '<li>暂无记录</li>'}</ol>
                <h3>历史任务 <small>${h.total} 项</small></h3>${h.items.map(taskBlock).join('') || '<p class="at-empty">暂无已结束的自报任务</p>'}
                <div class="at-pager"><button type="button" class="btn btn-secondary btn-sm" data-history="${Math.max(0,page-10)}" ${page===0?'disabled':''}>上一页</button><span class="at-muted">${h.total?page+1:0}–${page+h.items.length} / ${h.total}</span><button type="button" class="btn btn-secondary btn-sm" data-history="${page+10}" ${page+h.items.length>=h.total?'disabled':''}>下一页</button></div>`;
            if (background) body.scrollTop = scroll;
        } catch (error) {
            if (generation !== epoch || requestId !== detailEpoch || !dialog.open) return;
            body.innerHTML = `<p class="at-empty">详情读取失败：${esc(error.message)}。请关闭后重试。</p>`;
        }
    }
    function reset() {
        ++epoch; ++detailEpoch; clearTimeout(timer); teamId = null; members = []; selected = null; loading = false;
        $('at-activity-dialog').close();
    }
    document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) refresh(); });
    window.addEventListener('pagehide', reset);
    $('at-activity-close').addEventListener('click', ()=>$('at-activity-dialog').close());
    $('at-activity-dialog').addEventListener('close', ()=>{selected=null;++detailEpoch;});
    $('at-activity-body').addEventListener('click', event=>{const button=event.target.closest('[data-history]');if(button && !button.disabled && selected)loadDetail(selected, Number(button.dataset.history));});
    $('at-detail').addEventListener('click', event=>{
        const card=event.target.closest('[data-member]');if(card)loadDetail(Number(card.dataset.member));
        if(event.target.closest('#at-activity-refresh'))refresh(true);
    });
    return {reset, refresh, mount:async id=>{teamId=id;await refresh(true);}};
})();
