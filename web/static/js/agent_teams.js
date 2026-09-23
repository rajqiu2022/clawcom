/* Project-scoped team console. Activity refresh is read-only; no Run starts. */
(() => {
    'use strict';
    // Narrow-screen navigation is local to this page; do not overwrite the user's saved preference.
    if (window.matchMedia('(max-width:720px)').matches) {
        document.getElementById('app-sidebar')?.classList.add('collapsed');
        document.querySelector('.app-layout')?.classList.add('sidebar-collapsed');
    }
    const $ = id => document.getElementById(id);
    const state = { project: '', options: null, teams: [], total: 0, selected: null,
        loadEpoch: 0, missionEpoch: 0, missionOffset: 0, editing: null, saving: false };
    const roleLabels = {test_manager: '测试经理', project_assistant: '项目助理', code_analyst: '代码分析员', test_executor: '测试执行员'};
    const statusLabels = {active: '启用', paused: '暂停', archived: '归档', ready: '待派发',
        running: '执行中', completed: '已完成', cancelled: '已取消', expired: '已过期', failed: '失败',
        blocked: '阻塞', submitted: '待复核', accepted: '已验收', rejected: '已驳回', superseded: '已替代',
        waiting_input: '等待输入', waiting_permission: '等待授权', waiting_human_review: '等待人工复核'};
    function esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }
    function agentName(id) { return state.options?.agents.find(a => a.id === id)?.name || `Claw #${id}（当前不可用）`; }
    function agentLabel(id) { return `${agentName(id)} · #${id}`; }
    function badge(status) {
        const kind = ['active', 'completed', 'paused', 'failed', 'blocked'].includes(status) ? status : 'waiting';
        return `<span class="at-badge ${kind}">${esc(statusLabels[status] || status)}</span>`;
    }
    function notice(message, error = false) {
        $('at-notice').textContent = message;
        $('at-notice').classList.toggle('at-error', error);
        $('at-notice').hidden = !message;
    }
    function messageFor(error) {
        if (error.code === 'AGENT_TEAMS_DISABLED') return '当前项目尚未开启 Agent 团队。请由 Hub 部署管理员完成迁移并开启该项目的团队开关；这里不会绕过开关。';
        if (error.status === 403) return '你没有当前项目的相应权限。可联系项目管理员配置访问权限。';
        if (error.status === 401) return '登录已失效，请重新登录 Hub 后刷新。';
        return error.message || '读取失败，请稍后重试。';
    }
    function configOf(team) {
        return {project_id: team.project_id, name: team.name, objective: team.objective, status: team.status,
            primary_manager_claw_id: team.primary_manager_claw_id, backup_manager_claw_id: team.backup_manager_claw_id,
            members: team.members.map(m => ({claw_id: m.claw_id, role_key: m.role_key, specialties: [...m.specialties]})),
            policy: {allowed_definition_ids: [...team.policy.allowed_definition_ids], max_child_runs: team.policy.max_child_runs},
            expected_version: team.version};
    }
    function renderList() {
        $('at-total').textContent = state.total;
        $('at-list-count').textContent = `${state.teams.length} / ${state.total}`;
        $('at-more').hidden = state.teams.length >= state.total;
        $('at-list').innerHTML = state.teams.length ? state.teams.map(team => {
            const count = role => team.members.filter(m => m.role_key === role).length;
            return `<button class="at-team" type="button" data-team="${team.id}" aria-pressed="${team.id === state.selected}">
                <span class="at-team-head"><span class="at-team-name">${esc(team.name)}</span>${badge(team.status)}</span>
                <div class="at-team-meta">经理 · ${esc(agentName(team.primary_manager_claw_id))}</div>
                <div class="at-team-counts"><span>助理 ${count('project_assistant')}</span><span>分析员 ${count('code_analyst')}</span><span>执行员 ${count('test_executor')}</span><span>v${team.version}</span></div></button>`;
        }).join('') : '<p class="at-empty">这个项目还没有团队。<br>可为不同目标创建多支团队。</p>';
    }
    async function reloadProject(preferredTeam = null) {
        AgentTeamActivity.reset();
        AgentTeamPlans.reset();
        AgentTeamResources.reset();
        AgentTeamChat.reset();
        const epoch = ++state.loadEpoch;
        ++state.missionEpoch;
        state.project = $('at-project').value;
        state.options = null; state.selected = null; state.teams = []; state.total = 0;
        $('at-create').hidden = true; $('at-more').hidden = true;
        $('at-total').textContent = '—'; $('at-list-count').textContent = '—';
        $('at-detail').innerHTML = '<div class="at-empty at-start">正在读取团队配置…</div>';
        $('at-list').innerHTML = '<p class="at-empty">加载中…</p>';
        notice('');
        if (!state.project) {
            $('at-list').innerHTML = '<p class="at-empty">暂无可访问的项目。</p>';
            $('at-detail').innerHTML = '<div class="at-empty at-start">请选择项目。</div>';
            return;
        }
        $('at-loop-link').href = `/automation-closed-loop?project_id=${encodeURIComponent(state.project)}`;
        history.replaceState(null, '', `?project_id=${encodeURIComponent(state.project)}`);
        try { localStorage.setItem('agentTeams.project', state.project); } catch (_) { /* storage may be disabled */ }
        try {
            const query = `?project_id=${encodeURIComponent(state.project)}`;
            const [options, teams] = await Promise.all([API.get(`/agent-teams/options${query}`), API.get(`/agent-teams${query}`)]);
            if (epoch !== state.loadEpoch) return;
            state.options = options; state.teams = teams.items; state.total = teams.total;
            $('at-create').hidden = !options.can_manage;
            renderList();
            if (!options.can_manage) notice('只读模式：你可以查看团队编制和任务；团队配置由项目管理员维护。');
            const selected = state.teams.find(t => t.id === preferredTeam) || state.teams[0];
            if (selected) await selectTeam(selected.id);
            else $('at-detail').innerHTML = '<div class="at-empty at-start">先创建一支团队，再配置它的经理和成员。<br>新建团队不会自动启动任务。</div>';
        } catch (error) {
            if (epoch !== state.loadEpoch) return;
            notice(messageFor(error), error.code !== 'AGENT_TEAMS_DISABLED');
            $('at-list').innerHTML = '<p class="at-empty">团队暂不可用</p>';
            $('at-detail').innerHTML = '<div class="at-empty at-start">当前没有可显示的团队配置。</div>';
        }
    }
    async function moreTeams() {
        const epoch = state.loadEpoch;
        $('at-more').disabled = true;
        try {
            const result = await API.get(`/agent-teams?project_id=${encodeURIComponent(state.project)}&offset=${state.teams.length}`);
            if (epoch !== state.loadEpoch) return;
            state.teams = [...new Map([...state.teams, ...result.items].map(t => [t.id, t])).values()];
            state.total = result.total; renderList();
        } catch (error) { if (epoch === state.loadEpoch) notice(messageFor(error), true); }
        finally { $('at-more').disabled = false; }
    }
    async function selectTeam(id) {
        const team = state.teams.find(t => t.id === id);
        if (!team) return;
        AgentTeamActivity.reset();
        AgentTeamPlans.reset();
        AgentTeamResources.reset();
        AgentTeamChat.reset();
        state.selected = id; state.missionOffset = 0; renderList();
        const manage = state.options.can_manage;
        const actions = manage ? `<button class="btn btn-secondary btn-sm" data-action="edit" type="button">编辑配置</button>
            ${team.status !== 'archived' ? `<button class="btn btn-secondary btn-sm" data-action="${team.status === 'paused' ? 'active' : 'paused'}" type="button">${team.status === 'paused' ? '恢复' : '暂停'}</button>` : ''}
            <button class="btn btn-ghost btn-sm" data-action="${team.status === 'archived' ? 'active' : 'archived'}" type="button">${team.status === 'archived' ? '恢复团队' : '归档'}</button>` : '';
        const authority = team.manager_authority_active ? `主测试经理：${agentLabel(team.primary_manager_claw_id)}` : '当前未启用经理调度权限';
        $('at-detail').innerHTML = `<header class="at-detail-head"><div class="at-detail-title"><div><span class="at-eyebrow">TEAM #${team.id} / CONFIG v${team.version}</span><h2>${esc(team.name)}</h2></div><div class="at-detail-actions">${actions}</div></div>
            <p class="at-detail-goal">${esc(team.objective)}</p><div class="at-lease">${badge(team.status)}<strong>${esc(authority)}</strong><span>持续授权</span></div>
            <p class="at-help">测试经理的调度权限随团队角色持续有效；仅在管理员将其移出、替换或暂停团队时撤回。Supervisor 短锁只防止并发回合，不影响经理权限。</p></header>
            <div class="at-detail-tabs" role="tablist" aria-label="团队工作视图"><button id="at-tab-members" role="tab" aria-selected="true" aria-controls="at-members-panel" data-team-tab="members" type="button">团队成员</button><button id="at-tab-chat" role="tab" aria-selected="false" aria-controls="at-chat-panel" data-team-tab="chat" tabindex="-1" type="button">团队聊天室</button><button id="at-tab-plans" role="tab" aria-selected="false" aria-controls="at-plans-panel" data-team-tab="plans" tabindex="-1" type="button">任务计划</button><button id="at-tab-knowledge" role="tab" aria-selected="false" aria-controls="at-knowledge-panel" data-team-tab="knowledge" tabindex="-1" type="button">共享知识库</button><button id="at-tab-skills" role="tab" aria-selected="false" aria-controls="at-skills-panel" data-team-tab="skills" tabindex="-1" type="button">共享 Skills</button></div>
            <div id="at-members-panel" role="tabpanel" aria-labelledby="at-tab-members"><section class="at-activity"><header class="at-activity-head"><div><h3>团队成员 <span class="at-muted">Agent 自报状态</span></h3><p class="at-help">状态与进度由成员自行上报；超过 3 分钟未更新标为过期，不推断为空闲。</p></div><button type="button" class="btn btn-secondary btn-sm" id="at-activity-refresh">刷新状态</button></header><p id="at-activity-note" class="at-help" role="status"></p><div id="at-activity-cards" class="at-activity-grid"><p class="at-empty">正在读取成员状态…</p></div></section>
            <div class="at-policy">允许调度：${team.policy.allowed_definition_ids.map(flowId => `<a href="/workflows?definition_id=${flowId}">Flow #${flowId}</a>`).join('')}<br>每个 Mission 最多 ${team.policy.max_child_runs} 个 Run；经理自动获得调度权限，成员自动获得执行权限，不授予 Flow 编辑权限。</div>
            </div><div id="at-chat-panel" role="tabpanel" aria-labelledby="at-tab-chat" hidden><div id="at-chat-root"></div></div><div id="at-plans-panel" role="tabpanel" aria-labelledby="at-tab-plans" hidden><div id="at-plans-root"></div><div class="at-missions-head"><h3>执行链 / Mission</h3><span class="at-muted" id="at-mission-count">读取中…</span></div><div id="at-missions"><p class="at-empty">正在读取任务…</p></div><div id="at-pager" class="at-pager"></div></div>
            <div id="at-knowledge-panel" role="tabpanel" aria-labelledby="at-tab-knowledge" hidden><div id="at-knowledge-resources"><p class="at-empty">正在读取团队知识库…</p></div></div>
            <div id="at-skills-panel" role="tabpanel" aria-labelledby="at-tab-skills" hidden><div id="at-skills-resources"><p class="at-empty">正在读取团队 Skills…</p></div></div>`;
        await Promise.all([loadMissions(0), AgentTeamActivity.mount(id), AgentTeamPlans.mount(team), AgentTeamResources.mount(team), AgentTeamChat.mount(team)]);
    }
    async function loadMissions(offset) {
        const teamId = state.selected, epoch = ++state.missionEpoch;
        const projectEpoch = state.loadEpoch;
        $('at-missions').innerHTML = '<p class="at-empty">读取中…</p>';
        $('at-pager').innerHTML = '';
        try {
            const result = await API.get(`/agent-teams/${teamId}/missions?limit=20&offset=${offset}`);
            if (epoch !== state.missionEpoch || projectEpoch !== state.loadEpoch || teamId !== state.selected) return;
            state.missionOffset = offset;
            $('at-mission-count').textContent = `共 ${result.total} 项`;
            $('at-missions').innerHTML = result.items.length ? result.items.map(m => `<article class="at-mission"><div class="at-mission-title"><strong>#${m.id} ${esc(m.objective)}</strong>${badge(m.effective_status)}</div>
                <div class="at-mission-meta">Run 预算 ${m.child_run_count} / ${m.max_child_runs} · 创建于 ${esc(m.created_at)}</div><div class="at-stages">${m.stages.length ? m.stages.map(s => `<div class="at-stage"><strong>${esc(s.stage_key)}</strong> · ${esc(statusLabels[s.state] || s.state)}<small>${esc(roleLabels[s.role_key] || s.role_key)} · ${esc(agentName(s.assigned_claw_id))}</small>${s.last_reason_code ? `<small>${esc(s.last_reason_code)}</small>` : ''}${s.workflow_run_id ? `<a class="at-run-link" href="/workflows?run_id=${s.workflow_run_id}">Run #${s.workflow_run_id} ↗</a>` : '<small>尚未关联 Run</small>'}</div>`).join('') : '<span class="at-muted">经理尚未提交阶段计划。</span>'}</div></article>`).join('') : '<p class="at-empty">暂无任务。配置团队本身不会创建 Mission 或 Run。</p>';
            $('at-pager').innerHTML = `<span class="at-muted">${result.total ? offset + 1 : 0}–${offset + result.items.length} / ${result.total}</span><button type="button" class="btn btn-secondary btn-sm" data-page="${Math.max(0, offset - 20)}" ${offset === 0 ? 'disabled' : ''}>上一页</button><button type="button" class="btn btn-secondary btn-sm" data-page="${offset + 20}" ${offset + result.items.length >= result.total ? 'disabled' : ''}>下一页</button>`;
        } catch (error) {
            if (epoch !== state.missionEpoch || projectEpoch !== state.loadEpoch) return;
            $('at-mission-count').textContent = '读取失败';
            $('at-missions').innerHTML = `<p class="at-empty">${esc(messageFor(error))}</p>`;
        }
    }
    function agentOptions(selected = null, optional = false) {
        const agents = state.options.agents;
        let html = `<option value="">${optional ? '不配置' : '选择 Agent'}</option>`;
        html += agents.map(a => `<option value="${a.id}" ${a.id === selected ? 'selected' : ''}>${esc(a.name)} · #${a.id}${!a.has_worker_runtime ? '（未注册可信 Runtime）' : ''}</option>`).join('');
        if (selected && !agents.some(a => a.id === selected)) html += `<option value="${selected}" selected disabled>Claw #${selected}（不可用，请重新选择）</option>`;
        return html;
    }
    function addMember(role, member = null) {
        const div = document.createElement('div');
        div.className = 'at-member'; div.dataset.role = role;
        div.innerHTML = `<div class="at-member-top"><label>${esc(roleLabels[role])}<select class="form-select at-member-claw" required aria-label="${esc(roleLabels[role])} Agent">${agentOptions(member?.claw_id)}</select></label><button class="btn btn-ghost btn-sm at-remove-member" type="button" aria-label="移除此团队成员" title="仅移除团队分工，不删除 Claw">移除</button></div>${role === 'test_executor' ? `<div class="at-specialties">${Object.entries(state.options.executor_specialties).map(([key, label]) => `<label><input type="checkbox" value="${esc(key)}" ${member?.specialties.includes(key) ? 'checked' : ''}>${esc(label)}</label>`).join('')}</div>` : ''}`;
        $('at-members').appendChild(div);
    }
    function openEditor(team = null) {
        if (!state.options?.can_manage || state.saving) return;
        state.editing = team ? structuredClone(team) : null;
        $('at-form').reset(); $('at-form-error').hidden = true;
        $('at-edit-warning').hidden = !team;
        $('at-editor-title').textContent = team ? '编辑团队配置' : '创建团队';
        $('at-editor-project').textContent = `${$('at-project').selectedOptions[0].textContent}${team ? ' · v' + team.version : ' · 新团队'}`;
        $('at-name').value = team?.name || ''; $('at-objective').value = team?.objective || '';
        $('at-primary').innerHTML = agentOptions(team?.primary_manager_claw_id);
        $('at-backup').innerHTML = agentOptions(team?.backup_manager_claw_id, true);
        $('at-budget').value = team?.policy.max_child_runs || 20;
        $('at-members').replaceChildren();
        (team?.members || []).forEach(m => addMember(m.role_key, m));
        const selected = new Set(team?.policy.allowed_definition_ids || []);
        const flows = [...state.options.flows];
        selected.forEach(id => { if (!flows.some(f => f.id === id)) flows.push({id, name: '当前不可用，请取消选择', unavailable: true}); });
        $('at-flows').innerHTML = flows.length ? flows.map(f => `<label class="at-flow-option" data-search="${esc((f.id + ' ' + f.name).toLowerCase())}"><input type="checkbox" value="${f.id}" ${selected.has(f.id) ? 'checked' : ''} ${f.unavailable ? 'data-unavailable="true"' : ''}><span>#${f.id} ${esc(f.name)}</span></label>`).join('') : '<p class="at-empty">该项目没有 active Flow，创建团队前请先配置 Flow。</p>';
        $('at-editor').showModal(); $('at-name').focus();
    }
    function formError(message) { $('at-form-error').textContent = message; $('at-form-error').hidden = false; $('at-form-error').scrollIntoView({block: 'nearest'}); }
    function collectConfig() {
        const primary = Number($('at-primary').value), backup = Number($('at-backup').value) || null;
        if (primary === backup) throw new Error('主经理和备用经理不能是同一个 Agent。');
        const validIds = new Set(state.options.agents.map(a => a.id));
        if (!validIds.has(primary) || (backup && !validIds.has(backup))) throw new Error('经理已不属于当前项目，请重新选择。');
        const seen = new Set();
        const members = [...$('at-members').children].map(row => {
            const clawId = Number(row.querySelector('select').value), role = row.dataset.role;
            const specialties = [...row.querySelectorAll('input:checked')].map(el => el.value);
            if (!validIds.has(clawId)) throw new Error('请选择当前项目内有效的成员。');
            if (seen.has(`${role}:${clawId}`)) throw new Error('同一 Agent 不能在同一团队角色下重复添加。');
            if (role === 'test_executor' && !specialties.length) throw new Error('每名执行员至少选择一项二级角色。');
            seen.add(`${role}:${clawId}`);
            return {claw_id: clawId, role_key: role, specialties};
        });
        const flows = [...$('at-flows').querySelectorAll('input:checked')];
        if (!flows.length) throw new Error('请至少选择一个允许调度的 Flow。');
        if (flows.some(el => el.dataset.unavailable)) throw new Error('部分已选 Flow 已失效，请取消选择后再保存。');
        const name = $('at-name').value.trim(), objective = $('at-objective').value.trim();
        if (!name || !objective) throw new Error('请填写团队名称和目标。');
        return {project_id: Number(state.project), name, objective, status: state.editing?.status || 'active',
            primary_manager_claw_id: primary, backup_manager_claw_id: backup, members,
            policy: {allowed_definition_ids: flows.map(el => Number(el.value)), max_child_runs: Number($('at-budget').value)},
            ...(state.editing ? {expected_version: state.editing.version} : {})};
    }
    async function save(event) {
        event.preventDefault(); if (state.saving) return;
        let config;
        try { config = collectConfig(); } catch (error) { formError(error.message); return; }
        state.saving = true; $('at-save').disabled = true; $('at-save').textContent = '保存中…';
        try {
            const result = state.editing ? await API.put(`/agent-teams/${state.editing.id}`, config) : await API.post('/agent-teams', config);
            $('at-editor').close(); await reloadProject(result.id);
            if (state.options) notice('团队配置已保存。未启动 Workflow；测试经理的调度权限已按团队角色生效。');
        } catch (error) {
            formError(error.code === 'TEAM_VERSION_CONFLICT' ? '配置已被其他人修改，本次没有覆盖。请保留你的修改内容，关闭弹窗并刷新后重新编辑。' : messageFor(error));
        } finally { state.saving = false; $('at-save').disabled = false; $('at-save').textContent = '保存团队'; }
    }
    async function changeStatus(status) {
        const team = state.teams.find(t => t.id === state.selected);
        if (!team || !state.options.can_manage || state.saving) return;
        const epoch = state.loadEpoch;
        const prompt = `${statusLabels[status]}团队「${team.name}」？${status === 'paused' || status === 'archived' ? '这会暂停团队经理的新调度权限，' : ''}不会取消已经运行的任务。`;
        const confirmed = typeof customConfirm === 'function' ? await customConfirm(prompt) : window.confirm(prompt);
        if (!confirmed || epoch !== state.loadEpoch || state.saving) return;
        state.saving = true;
        try {
            await API.put(`/agent-teams/${team.id}`, {...configOf(team), status});
            if (epoch === state.loadEpoch) await reloadProject(team.id);
        } catch (error) {
            if (epoch === state.loadEpoch) notice(error.code === 'TEAM_VERSION_CONFLICT' ? '配置版本已变化，本次未覆盖，请刷新后重试。' : messageFor(error), true);
        } finally { state.saving = false; }
    }
    $('at-project').addEventListener('change', () => reloadProject());
    $('at-refresh').addEventListener('click', () => reloadProject(state.selected));
    $('at-create').addEventListener('click', () => openEditor());
    $('at-more').addEventListener('click', moreTeams);
    $('at-list').addEventListener('click', event => { const button = event.target.closest('[data-team]'); if (button) selectTeam(Number(button.dataset.team)); });
    $('at-detail').addEventListener('click', event => {
        const action = event.target.closest('[data-action]');
        if (action?.dataset.action === 'edit') openEditor(state.teams.find(t => t.id === state.selected));
        else if (action) changeStatus(action.dataset.action);
        const page = event.target.closest('[data-page]'); if (page && !page.disabled) loadMissions(Number(page.dataset.page));
    });
    $('at-add-assistant').addEventListener('click', () => addMember('project_assistant'));
    $('at-add-analyst').addEventListener('click', () => addMember('code_analyst'));
    $('at-add-executor').addEventListener('click', () => addMember('test_executor'));
    $('at-members').addEventListener('click', event => { const button = event.target.closest('.at-remove-member'); if (button) button.closest('.at-member').remove(); });
    $('at-flow-search').addEventListener('input', () => { const query = $('at-flow-search').value.toLowerCase(); $('at-flows').querySelectorAll('[data-search]').forEach(el => { el.hidden = !el.dataset.search.includes(query); }); });
    ['at-close', 'at-cancel'].forEach(id => $(id).addEventListener('click', () => { if (!state.saving) $('at-editor').close(); }));
    $('at-editor').addEventListener('cancel', event => { if (state.saving) event.preventDefault(); });
    $('at-form').addEventListener('submit', save);
    async function init() {
        try {
            const projects = await API.listProjects();
            $('at-project').innerHTML = '<option value="">选择项目</option>' + projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
            let saved = ''; try { saved = localStorage.getItem('agentTeams.project'); } catch (_) { /* optional */ }
            const wanted = new URLSearchParams(location.search).get('project_id') || saved;
            $('at-project').value = projects.some(p => String(p.id) === wanted) ? wanted : String(projects[0]?.id || '');
            await reloadProject();
        } catch (error) { notice(messageFor(error), true); $('at-project').innerHTML = '<option value="">项目加载失败，请刷新页面</option>'; }
    }
    init();
})();
