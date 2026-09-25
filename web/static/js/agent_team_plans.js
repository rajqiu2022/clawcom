/* Read-only schedule projection; explicit authoring never starts a Run. */
const AgentTeamPlans = (() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const labels = {draft:'草稿',active:'进行中',completed:'已完成',archived:'归档',assigned:'已指派',pending:'待开始',in_progress:'执行中',scheduled:'等待到点',ready:'待派发',dispatched:'待领取',running:'执行中',failed:'失败',cancelled:'已取消',blocked:'阻塞',skipped:'已跳过'};
    let team = null, epoch = 0, requestId = 0, reportRequestId = 0, reportContext = null, period = 'week', day = '', offset = 0, mode = 'new', saving = false;
    function today() { return new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date()); }
    function reset() { ++epoch; ++requestId; ++reportRequestId; team = null; $('at-plan-dialog').close(); $('at-plan-reports-dialog').close(); }
    function frame() {
        $('at-plans-root').innerHTML = `<section class="at-plan-workspace"><header class="at-plan-toolbar"><div><span class="at-eyebrow">TEAM SCHEDULE</span><h3>测试计划与任务</h3></div><div><button id="at-plan-refresh" type="button" class="btn btn-secondary btn-sm"><span class="at-refresh-icon" aria-hidden="true">↻</span> 刷新状态</button> <button type="button" class="btn btn-secondary btn-sm" id="at-plan-link" hidden>关联已有计划</button> <button type="button" class="btn btn-primary btn-sm" id="at-plan-create" hidden>＋ 新建计划</button></div></header>
        <div class="at-plan-filters"><div role="group" aria-label="排期范围">${[['day','每日'],['week','每周'],['all','全部']].map(([key,label])=>`<button type="button" data-period="${key}" aria-pressed="${key===period}">${label}</button>`).join('')}</div><label>基准日期 <input id="at-plan-date" type="date" class="form-input" value="${day}" ${period==='all'?'disabled':''}></label><button id="at-plan-today" type="button" class="btn btn-ghost btn-sm">回到今天</button></div>
        <p id="at-plan-range" class="at-help"></p><div id="at-plan-summary" class="at-plan-summary"></div><div id="at-plan-recovery" class="at-plan-recovery" hidden></div><p id="at-plan-note" class="at-help" role="status"></p><div id="at-plan-cards" class="at-plan-grid"></div><div id="at-plan-pages" class="at-pager"></div>
        <p class="at-help">按实际测试任务排期交集展示（北京时间，周一至周日），完成比例不含任务链。跨日任务会在对应日期出现；未排期任务请在“全部”查看。日期筛选不代表重复执行或自动调度。</p></section>`;
    }
    function badge(status) { return `<span class="at-plan-state ${Object.hasOwn(labels,status)?status:''}">${esc(labels[status] || status)}</span>`; }
    function supervision(p) {
        if (p.status !== 'active') return '';
        const s = p.supervision;
        if (!s) return '<p class="at-help at-plan-alert">监管未启动 · 尚无 Supervisor/Mission</p>';
        if (!s.manager_authority_active) return `<p class="at-help at-plan-alert">监管 ${esc(s.status)} · 测试经理已被移出或团队已暂停 · Mission ${s.mission_id ? '#'+esc(s.mission_id) : '未创建'}</p>`;
        const resume = s.can_resume ? ` <button type="button" class="btn btn-secondary btn-sm at-plan-resume" data-plan-resume="${esc(p.id)}" data-resume-api="${esc(s.resume_api)}" title="恢复 Supervisor，并原子推进和补派所有已到点的独立任务；局部阻断任务保持原状">恢复监督</button>` : '';
        return `<p class="at-help">监管 ${esc(s.status)} · 经理 #${esc(s.orchestrator_claw_id)} · Mission #${esc(s.mission_id)}${s.next_check_at?' · 下次 '+esc(s.next_check_at):''}${resume}</p>`;
    }
    function renderRecovery(items) {
        const root = $('at-plan-recovery');
        const resumable = items.filter(plan => plan.supervision?.can_resume);
        root.hidden = !resumable.length;
        root.innerHTML = resumable.map(plan => `<section class="at-plan-recovery-banner" role="alert"><div><strong>计划 #${esc(plan.id)} 的持续监督已暂停</strong><span>${esc(plan.name)} · 当前 ${esc(plan.supervision.status)}；恢复后会立即对账并补派已到点任务，局部阻断保持不变。</span></div><button type="button" class="btn btn-primary btn-sm at-plan-resume" data-plan-resume="${esc(plan.id)}" data-resume-api="${esc(plan.supervision.resume_api)}">恢复监督</button></section>`).join('');
    }
    async function resumeSupervision(button) {
        const planId = Number(button.dataset.planResume);
        if (!planId || !button.dataset.resumeApi) return;
        const confirmed = await customConfirm({
            type: 'warning',
            title: `恢复计划 #${planId} 的持续监督？`,
            msg: 'Hub 将立即重新对账并补派已到点任务。\n局部阻断任务保持原状，不会被错误解除。',
            okText: '恢复监督',
            cancelText: '暂不恢复',
        });
        if (!confirmed) return;
        button.disabled = true;
        const original = button.textContent;
        button.textContent = '恢复中…';
        try {
            const commandKey = `team-ui-resume:${planId}:${Date.now()}:${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`;
            const result = await API.post(button.dataset.resumeApi, {
                command_key: commandKey,
                reason: '管理员从 Agent 团队计划页面恢复监督并补派已到点任务',
            });
            const count = (result.dispatched || []).length;
            $('at-plan-note').textContent = `计划 #${planId} 已恢复监督，已确认 ${count} 个到点派发。`;
            showToast(`计划 #${planId} 已恢复持续监督`, 'success');
            await load(offset);
        } catch (error) {
            $('at-plan-note').textContent = `恢复失败：${error.message || '请稍后重试'}`;
            showToast(`恢复失败：${error.message || '请稍后重试'}`, 'error');
            button.disabled = false;
            button.textContent = original;
        }
    }
    function task(t, planId, collapsed) {
        const occurrence = t.occurrence || null, execution = occurrence && occurrence.execution;
        const refs = t.references || {};
        const referenceItems = [
            ...(refs.skills || []).map(item => ({...item, icon:'⚙'})),
            ...(refs.knowledge || []).map(item => ({...item, icon:'📚'})),
            ...(refs.reports || []).map(item => ({...item, icon:'📄'})),
        ];
        const referenceHtml = referenceItems.length
            ? `<div class="at-task-references">${referenceItems.slice(0,4).map(item=>`<a href="${esc(item.web_url || '#')}" target="_blank" rel="noopener" title="${esc(item.title)}">${item.icon} #${esc(item.id)} ${esc(item.title)}</a>`).join('')}${referenceItems.length>4?`<span>+${referenceItems.length-4}</span>`:''}</div>`
            : '';
        const detailId = `at-task-detail-${esc(planId)}-${esc(t.id)}`;
        const detailButton = t.description
            ? `<button type="button" class="at-task-detail-toggle" data-task-detail-toggle aria-expanded="false" aria-controls="${detailId}">查看详情</button>`
            : '';
        const description = t.description
            ? `<div id="${detailId}" class="at-task-details" hidden><p class="at-task-description">${esc(t.description)}</p></div>`
            : '';
        const executionMeta = occurrence
            ? `<p class="at-help">执行实例 #${esc(occurrence.id)} · ${esc(occurrence.occurrence_date)}${execution?` · AgentTask #${esc(execution.agent_task_row_id)} / ${esc(execution.agent_task_id)} · 尝试 ${esc(execution.attempt_no || 1)}${execution.last_heartbeat_at?` · 心跳 ${esc(execution.last_heartbeat_at)}`:''}`:''}${occurrence.next_action?` · 下一步 ${esc(occurrence.next_action)}`:''}${occurrence.next_check_at?` · 检查 ${esc(occurrence.next_check_at)}`:''}${execution && execution.lease_expired?' · <span class="at-plan-alert">租约已超时，等待 Hub 回队</span>':''}</p>`
            : (t.schedule_enabled ? '<p class="at-help">当前日期尚未生成执行实例</p>' : '');
        return `<li ${collapsed?'hidden data-plan-extra-task':''}><div class="at-plan-task-title"><span><span class="at-plan-task-id">任务 #${esc(t.id)}</span>${esc(t.name)}</span><span class="at-plan-task-actions">${detailButton}<button type="button" class="at-task-report" data-task-reports="${esc(t.id)}" data-plan-id="${esc(planId)}" data-task-name="${esc(t.name)}" aria-label="查看任务 #${esc(t.id)} 的关联报告">📄 报告 <b data-report-count ${t.report_count?'':'hidden'}>${esc(t.report_count || 0)}</b></button>${badge(t.status || 'scheduled')}</span></div><p class="at-help">${esc(t.priority)} · ${esc(t.assignee)} · ${esc(t.start_date || t.end_date || '未排期')}${t.end_date && t.end_date!==t.start_date?' → '+esc(t.end_date):''}${t.schedule_enabled?` · ${esc(t.recurrence_type==='daily'?'每日':t.recurrence_type==='weekly'?'每周':'单次')}`:''}${t.overdue?' · <span class="at-plan-alert">已逾期</span>':''}</p>${description}${referenceHtml}${executionMeta}<div class="at-progress"><progress max="100" value="${t.progress}" aria-label="任务上报进度"></progress><span>${t.progress}%</span></div></li>`;
    }
    function reportCard(r) {
        const global = r.kind === 'global';
        const meta = global
            ? `${esc(r.report_type_label || '测试报告')} · ${esc(r.status_label || r.status || '未标记')} · ${esc(r.risk_level_label || '风险待定')}`
            : `${esc(r.scopeLabel || '计划报告')} · ${esc(r.format || 'markdown').toUpperCase()}`;
        const author = r.submitter_name || r.created_by_name || r.created_by || '未知作者';
        const summary = r.remark || r.summary || '';
        return `<article class="at-plan-report-card"><header><span class="at-plan-report-kind ${global?'global':'legacy'}">${global?'GLOBAL REPORT':esc(r.kindLabel || 'PLAN REPORT')}</span><time>${esc((r.created_at || '').slice(0,16).replace('T',' '))}</time></header><h4>${esc(r.title || '未命名报告')}</h4><p class="at-plan-report-meta">${meta} · ${esc(author)}</p>${summary?`<p class="at-plan-report-summary">${esc(summary)}</p>`:''}<footer>${global && (r.attachments || []).length?`<span>${r.attachments.length} 个附件</span>`:'<span></span>'}<button type="button" class="btn btn-secondary btn-sm" data-report-kind="${global?'global':'legacy'}" data-report-id="${esc(r.id)}" data-report-format="${esc(r.format || 'markdown')}" data-report-title="${esc(r.title || '测试报告')}">查看报告</button></footer></article>`;
    }
    function renderReportContent(data) {
        const content = data.content || '';
        if (!content) return '<div class="at-empty">该报告暂无正文。</div>';
        if (data.format === 'html') return DOMPurify.sanitize(content, {FORBID_TAGS:['script','style','iframe','object','embed'], FORBID_ATTR:['style','onerror','onload','onclick']});
        return DOMPurify.sanitize(marked.parse(content));
    }
    function reportContextKey(value) {
        return value ? `${value.scope}:${value.planId}:${value.taskId || 0}` : '';
    }
    function setReportsFullscreen(fullscreen) {
        const dialog = $('at-plan-reports-dialog'), button = $('at-plan-reports-fullscreen');
        dialog.classList.toggle('is-fullscreen', fullscreen);
        button.setAttribute('aria-pressed', String(fullscreen));
        button.textContent = fullscreen ? '↙ 退出全屏' : '⛶ 全屏';
    }
    async function openReports(target) {
        const context = {
            scope: target.scope === 'task' ? 'task' : 'plan',
            planId: Number(target.planId),
            taskId: target.taskId ? Number(target.taskId) : null,
            name: target.name,
        };
        const key = reportContextKey(context), turn = ++reportRequestId;
        reportContext = context;
        setReportsFullscreen(false);
        $('at-plan-reports-eyebrow').textContent = context.scope === 'task' ? 'TASK REPORTS / 任务报告' : 'PLAN REPORTS / 计划报告';
        $('at-plan-reports-title').textContent = `${context.name} · 报告`;
        $('at-plan-reports-count').textContent = '正在读取关联报告…';
        if ($('at-task-conclusion')) $('at-task-conclusion').hidden = true;
        $('at-plan-reports-list').innerHTML = '<div class="at-empty">加载中…</div>';
        $('at-plan-report-preview').hidden = true;
        $('at-plan-reports-list').hidden = false;
        $('at-plan-reports-dialog').showModal();
        try {
            const legacyPath = context.scope === 'task'
                ? `/test-plans/${context.planId}/tasks/${context.taskId}/reports`
                : `/test-plans/${context.planId}/reports`;
            const sourceType = context.scope === 'task' ? 'test_task' : 'test_plan';
            const sourceId = context.scope === 'task' ? context.taskId : context.planId;
            const requests = [
                API.get(legacyPath),
                API.get(`/test-reports?source_ref_type=${sourceType}&source_ref_id=${sourceId}&page_size=100`),
            ];
            if (context.scope === 'task') requests.push(API.get(`/test-plans/${context.planId}/tasks/${context.taskId}/conclusion`));
            const [legacy, global, conclusion] = await Promise.all(requests);
            if (turn !== reportRequestId || reportContextKey(reportContext) !== key) return;
            if (conclusion && $('at-task-conclusion')) {
                $('at-task-conclusion').hidden = false;
                $('at-task-conclusion-content').value = conclusion.content || '';
                $('at-task-conclusion-content').readOnly = !conclusion.can_edit;
                $('at-task-conclusion-save').hidden = !conclusion.can_edit;
                $('at-task-conclusion-meta').textContent = conclusion.updated_at
                    ? `最近更新：${conclusion.updated_at}`
                    : '尚未填写任务结论';
            }
            const globalItems = global.items || [], globalIds = new Set(globalItems.map(item=>Number(item.id)));
            const legacyItems = (legacy.items || []).filter(item=>!item.linked_test_report_id || !globalIds.has(Number(item.linked_test_report_id)));
            const scopeLabel = context.scope === 'task' ? '任务报告' : '计划报告';
            const items = [
                ...globalItems.map(item=>({...item,kind:'global'})),
                ...legacyItems.map(item=>({...item,kind:'legacy',scopeLabel,kindLabel:context.scope === 'task' ? 'TASK REPORT' : 'PLAN REPORT'})),
            ];
            $('at-plan-reports-count').textContent = `共 ${items.length} 份 · 全局报告 ${globalItems.length} · 旧版${scopeLabel} ${legacyItems.length}`;
            $('at-plan-reports-list').innerHTML = items.length ? items.map(reportCard).join('') : `<div class="at-empty">当前${context.scope === 'task' ? '任务' : '计划'}尚未关联报告。</div>`;
            const selector = context.scope === 'task' ? `[data-task-reports="${context.taskId}"]` : `[data-plan-reports="${context.planId}"]`;
            document.querySelectorAll(`${selector} [data-report-count]`).forEach(node=>{node.textContent=items.length;node.hidden=!items.length;});
        } catch (error) {
            if (turn !== reportRequestId) return;
            $('at-plan-reports-count').textContent = '关联报告读取失败';
            $('at-plan-reports-list').innerHTML = `<div class="at-empty at-plan-alert">${esc(error.message || '请稍后重试')}</div>`;
        }
    }
    async function previewReport(kind, id, title, format) {
        const turn = ++reportRequestId;
        $('at-plan-report-preview-title').textContent = title;
        $('at-plan-report-preview-body').innerHTML = '<div class="at-empty">正在读取报告正文…</div>';
        $('at-plan-reports-list').hidden = true;
        $('at-plan-report-preview').hidden = false;
        if (kind === 'global' && format === 'html') {
            $('at-plan-report-preview-body').innerHTML = `<iframe class="at-plan-report-frame" sandbox="allow-scripts" src="/api/v1/test-reports/${encodeURIComponent(id)}/html-preview" title="${esc(title)}"></iframe>`;
            return;
        }
        try {
            if (!reportContext) throw new Error('报告上下文已失效');
            const legacyPath = reportContext.scope === 'task'
                ? `/test-plans/${reportContext.planId}/tasks/${reportContext.taskId}/reports/${id}`
                : `/test-plans/${reportContext.planId}/reports/${id}`;
            const data = await API.get(kind === 'global' ? `/test-reports/${id}` : legacyPath);
            if (turn !== reportRequestId) return;
            $('at-plan-report-preview-body').innerHTML = renderReportContent(data);
        } catch (error) {
            if (turn === reportRequestId) $('at-plan-report-preview-body').innerHTML = `<div class="at-empty at-plan-alert">${esc(error.message || '报告读取失败')}</div>`;
        }
    }
    async function saveTaskConclusion() {
        if (!reportContext || reportContext.scope !== 'task') return;
        const button = $('at-task-conclusion-save');
        button.disabled = true;
        try {
            const data = await API.put(
                `/test-plans/${reportContext.planId}/tasks/${reportContext.taskId}/conclusion`,
                {content:$('at-task-conclusion-content').value});
            $('at-task-conclusion-meta').textContent = data.updated_at
                ? `已保存 · ${data.updated_at}` : '已保存';
        } catch (error) {
            $('at-task-conclusion-meta').textContent = `保存失败：${error.message || '请稍后重试'}`;
        } finally { button.disabled = false; }
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
            renderRecovery(data.items);
            $('at-plan-cards').innerHTML = data.items.length ? data.items.map(p=>`<article class="at-plan-card" data-plan-card="${esc(p.id)}"><header><span class="at-eyebrow">PLAN #${p.id}</span>${badge(p.status)}</header><h4><a href="${esc(p.url)}">${esc(p.name)} ↗</a></h4><p class="at-help">${esc(p.start_date)} → ${esc(p.end_date)}</p>${supervision(p)}<div class="at-progress"><progress max="100" value="${p.progress}" aria-label="计划任务完成比例"></progress><span>${p.completed_tasks}/${p.total_tasks} 已完成</span></div><div class="at-plan-section">${period==='all'?'全部任务':'当前范围任务'} <strong>${p.period_tasks}</strong>${p.unscheduled_tasks?`<span>另有 ${p.unscheduled_tasks} 项未排期</span>`:''}</div><ul data-plan-tasks="${esc(p.id)}">${p.tasks.map((t,index)=>task(t,p.id,index>=5)).join('') || '<li class="at-help">当前范围暂无已排期任务</li>'}</ul><footer><span class="at-help">${p.tasks.length>5?`<button type="button" class="btn btn-secondary btn-sm at-plan-task-toggle" data-plan-task-toggle="${esc(p.id)}" aria-expanded="false">查看全部 ${p.tasks.length} 项</button>`:'进度来自测试计划记录'}</span><div class="at-plan-actions"><button type="button" class="at-plan-report" data-plan-reports="${esc(p.id)}" data-plan-name="${esc(p.name)}" aria-label="查看计划 #${esc(p.id)} 的关联报告">📄 报告 <b data-report-count ${p.report_count?'':'hidden'}>${esc(p.report_count)}</b></button><a href="${esc(p.url)}">查看计划详情 →</a></div></footer></article>`).join('') : '<div class="at-empty">当前范围暂无团队计划。<br>可切换日期、“全部”，或由测试经理新建 / 关联已有计划。</div>';
            $('at-plan-pages').innerHTML = `<span class="at-muted">${data.total?page+1:0}–${page+data.items.length} / ${data.total} 个计划</span><button type="button" class="btn btn-secondary btn-sm" data-plan-page="${Math.max(0,page-6)}" ${page===0?'disabled':''}>上一页</button><button type="button" class="btn btn-secondary btn-sm" data-plan-page="${page+6}" ${page+data.items.length>=data.total?'disabled':''}>下一页</button>`;
            $('at-plan-note').textContent = data.can_manage?'可创建草稿计划，或将同项目已有计划关联到本团队。':'计划由团队测试经理或项目管理员维护。';
            return true;
        } catch (error) {
            if (generation!==epoch || turn!==requestId) return;
            $('at-plan-create').hidden = $('at-plan-link').hidden = true;
            $('at-plan-cards').replaceChildren(); $('at-plan-summary').replaceChildren(); $('at-plan-recovery').replaceChildren(); $('at-plan-recovery').hidden = true; $('at-plan-pages').replaceChildren();
            $('at-plan-note').textContent = `读取失败：${error.message || '请稍后重试'}，请点击刷新。`;
            return false;
        } finally { if(generation===epoch && turn===requestId) $('at-plan-cards').removeAttribute('aria-busy'); }
    }
    async function refreshStatus(button) {
        button.disabled = true;
        button.classList.add('is-loading');
        button.setAttribute('aria-busy', 'true');
        const ok = await load(offset);
        button.disabled = false;
        button.classList.remove('is-loading');
        button.removeAttribute('aria-busy');
        if (!ok) return;
        const refreshedAt = new Intl.DateTimeFormat('zh-CN', {
            timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit',
            second: '2-digit', hour12: false,
        }).format(new Date());
        $('at-plan-note').textContent = `任务、执行实例和监督状态已刷新 · ${refreshedAt}`;
        showToast('团队任务状态已刷新', 'success');
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
    $('at-plan-reports-close').addEventListener('click',()=>$('at-plan-reports-dialog').close());
    $('at-plan-reports-fullscreen').addEventListener('click',()=>setReportsFullscreen(!$('at-plan-reports-dialog').classList.contains('is-fullscreen')));
    $('at-plan-report-back').addEventListener('click',()=>{$('at-plan-report-preview').hidden=true;$('at-plan-reports-list').hidden=false;});
    $('at-task-conclusion-save')?.addEventListener('click',saveTaskConclusion);
    $('at-plan-reports-dialog').addEventListener('close',()=>{++reportRequestId;reportContext=null;setReportsFullscreen(false);});
    $('at-plan-reports-dialog').addEventListener('click',event=>{const button=event.target.closest('button[data-report-kind]');if(button)previewReport(button.dataset.reportKind,button.dataset.reportId,button.dataset.reportTitle,button.dataset.reportFormat);});
    function tab(value) {
        ['members','chat','plans','knowledge','skills'].forEach(key=>{const selected=key===value; $(`at-tab-${key}`).setAttribute('aria-selected',String(selected)); $(`at-tab-${key}`).tabIndex=selected?0:-1; $(`at-${key}-panel`).hidden=!selected;});
        if(value==='members') AgentTeamActivity.refresh(true);
        if(value==='chat') AgentTeamChat.refresh();
        if(value==='knowledge' || value==='skills') AgentTeamResources.refresh();
    }
    $('at-detail').addEventListener('keydown',event=>{if(event.target.matches('[data-team-tab]') && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const keys=['members','chat','plans','knowledge','skills'], current=keys.indexOf(event.target.dataset.teamTab);const next=event.key==='Home'?keys[0]:event.key==='End'?keys[keys.length-1]:keys[(current+(event.key==='ArrowRight'?1:-1)+keys.length)%keys.length];tab(next);$(`at-tab-${next}`).focus();}});
    $('at-detail').addEventListener('change',event=>{if(event.target.id==='at-plan-date' && event.target.value){day=event.target.value;load();}});
    $('at-detail').addEventListener('click',event=>{
        const button=event.target.closest('button'); if(!button || button.disabled) return;
        if(button.dataset.teamTab) tab(button.dataset.teamTab);
        if(button.dataset.period){period=button.dataset.period;frame();load();}
        if(button.dataset.planPage) load(Number(button.dataset.planPage));
        if(button.id==='at-plan-refresh') refreshStatus(button);
        if(button.id==='at-plan-today'){day=today();frame();load();}
        if(button.id==='at-plan-create') open('new');
        if(button.id==='at-plan-link') open('link');
        if(button.dataset.planResume) resumeSupervision(button);
        if(button.dataset.planTaskToggle){
            const card=button.closest('[data-plan-card]');
            const extras=card?card.querySelectorAll('[data-plan-extra-task]'):[];
            const expand=button.getAttribute('aria-expanded')!=='true';
            extras.forEach(item=>{item.hidden=!expand;});
            button.setAttribute('aria-expanded',String(expand));
            button.textContent=expand?'收起任务':`查看全部 ${(card?.querySelectorAll('[data-plan-tasks] > li').length || 0)} 项`;
        }
        if(button.hasAttribute('data-task-detail-toggle')){
            const panelId=button.getAttribute('aria-controls');
            const panel=panelId?document.getElementById(panelId):null;
            if(panel){
                const expand=button.getAttribute('aria-expanded')!=='true';
                panel.hidden=!expand;
                button.setAttribute('aria-expanded',String(expand));
                button.textContent=expand?'收起详情':'查看详情';
            }
        }
        if(button.dataset.planReports) openReports({scope:'plan',planId:button.dataset.planReports,name:button.dataset.planName});
        if(button.dataset.taskReports) openReports({scope:'task',planId:button.dataset.planId,taskId:button.dataset.taskReports,name:button.dataset.taskName});
    });
    return {reset, mount:async value=>{team=value;period='week';day=today();frame();await load();}};
})();
