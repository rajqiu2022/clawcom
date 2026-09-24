/* Team resource shelf: canonical Knowledge/Skill links with on-demand pulls. */
const AgentTeamResources = (() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let team = null, manifest = null, epoch = 0, pickerType = 'knowledge', options = null;

    function reset() {
        ++epoch; team = null; manifest = null; options = null;
        if ($('at-resource-dialog')?.open) $('at-resource-dialog').close();
    }
    function label(type) { return type === 'knowledge' ? '知识' : 'Skill'; }
    function formatTime(value) { return value ? String(value).replace('T', ' ').slice(0, 16) : '—'; }
    function card(item, type) {
        const revision = type === 'knowledge' && item.revision ? `Revision ${item.revision}` : (item.review_status || item.category || '');
        return `<article class="at-resource-card"><header><span class="at-resource-kind">${type === 'knowledge' ? 'KNOWLEDGE' : 'SKILL'} <b class="at-resource-id">#${esc(item.id)}</b></span><span>${esc(revision)}</span></header>
            <h4><a href="${esc(item.web_url)}">${esc(item.title)} ↗</a></h4>
            <p>${esc(type === 'knowledge' ? (item.module_name || item.category || item.entry_type) : `${item.name} · ${item.category || item.scope || ''}`)}</p>
            <footer><span>更新 ${esc(formatTime(item.updated_at))}</span><div><button type="button" class="btn btn-ghost btn-sm" data-copy-resource="${esc(item.pull_url)}">复制拉取地址</button>${manifest.can_manage ? `<button type="button" class="btn btn-ghost btn-sm" data-unlink-resource="${type}" data-resource-id="${item.id}">移出团队</button>` : ''}</div></footer></article>`;
    }
    function render(type) {
        const root = $(`at-${type}-resources`);
        if (!root || !manifest) return;
        const items = type === 'knowledge' ? manifest.knowledge : manifest.skills;
        root.innerHTML = `<section class="at-resource-workspace"><header class="at-resource-head"><div><span class="at-eyebrow">TEAM ${type === 'knowledge' ? 'KNOWLEDGE' : 'SKILLS'}</span><h3>团队共享${label(type)}</h3><p>内容仍由 Hub ${type === 'knowledge' ? '知识库' : 'Skills 市场'}维护；这里保存团队入口，Agent 按需拉取最新版本。</p></div>${manifest.can_manage ? `<button type="button" class="btn btn-primary btn-sm" data-add-resource="${type}">＋ 添加已有${label(type)}</button>` : ''}</header>
            <div class="at-resource-summary"><strong>${items.length}</strong><span>项团队资源</span><code>GET /api/v1/agent-teams/${team.id}/shared-resources</code></div>
            <div class="at-resource-grid">${items.length ? items.map(item => card(item, type)).join('') : `<div class="at-empty">团队还没有共享${label(type)}。<br>${manifest.can_manage ? `从现有${label(type)}库添加，不会复制正文或打断版本历史。` : '由项目成员或团队 Agent 维护。'}</div>`}</div></section>`;
    }
    async function load() {
        if (!team) return;
        const generation = epoch, id = team.id;
        try {
            const data = await API.get(`/agent-teams/${id}/shared-resources`);
            if (generation !== epoch || !team || team.id !== id) return;
            manifest = data; render('knowledge'); render('skills');
        } catch (error) {
            if (generation !== epoch) return;
            ['knowledge','skills'].forEach(type => {
                const root = $(`at-${type}-resources`);
                if (root) root.innerHTML = `<p class="at-empty">共享资源读取失败：${esc(error.message || '请稍后重试')}</p>`;
            });
        }
    }
    async function openPicker(type) {
        if (!team || !manifest?.can_manage) return;
        pickerType = type; options = null;
        $('at-resource-dialog-title').textContent = `添加团队共享${label(type)}`;
        $('at-resource-search').value = '';
        $('at-resource-options').innerHTML = '<p class="at-empty">正在读取现有资源…</p>';
        $('at-resource-dialog').showModal();
        const generation = epoch;
        try {
            options = await API.get(`/agent-teams/${team.id}/shared-resources/options`);
            if (generation !== epoch || !$('at-resource-dialog').open) return;
            renderOptions(); $('at-resource-search').focus();
        } catch (error) {
            $('at-resource-options').innerHTML = `<p class="at-empty">${esc(error.message || '读取失败')}</p>`;
        }
    }
    function renderOptions() {
        if (!options) return;
        const query = $('at-resource-search').value.trim().toLowerCase();
        const rows = (pickerType === 'knowledge' ? options.knowledge : options.skills)
            .filter(item => !query || `${item.id} ${item.title} ${item.name || ''} ${item.module_name || ''}`.toLowerCase().includes(query));
        $('at-resource-options').innerHTML = rows.length ? rows.map(item => `<div class="at-resource-option"><div><strong>#${item.id} ${esc(item.title)}</strong><small>${esc(item.module_name || item.name || item.category || item.entry_type || '')}${item.revision ? ` · Revision ${item.revision}` : ''}</small></div><button type="button" class="btn ${item.linked ? 'btn-secondary' : 'btn-primary'} btn-sm" data-link-resource="${item.id}" ${item.linked ? 'disabled' : ''}>${item.linked ? '已添加' : '添加'}</button></div>`).join('') : '<p class="at-empty">没有匹配的现有资源。</p>';
    }
    async function link(id, button) {
        button.disabled = true;
        try {
            await API.post(`/agent-teams/${team.id}/shared-resources/${pickerType}`, {resource_id: id});
            const row = (pickerType === 'knowledge' ? options.knowledge : options.skills).find(item => item.id === id);
            if (row) row.linked = true;
            renderOptions(); await load();
        } catch (error) { button.disabled = false; window.alert(error.message || '添加失败'); }
    }
    async function unlink(type, id) {
        if (!team || !manifest?.can_manage) return;
        const prompt = `将该${label(type)}移出团队共享区？原知识/Skill 及版本记录不会被删除。`;
        const yes = typeof customConfirm === 'function' ? await customConfirm(prompt) : window.confirm(prompt);
        if (!yes) return;
        await API.del(`/agent-teams/${team.id}/shared-resources/${type}/${id}`);
        await load();
    }
    async function copy(value) {
        const absolute = value.startsWith('http') ? value : `${location.origin}${value}`;
        await navigator.clipboard.writeText(absolute);
    }

    $('at-detail').addEventListener('click', event => {
        const add = event.target.closest('[data-add-resource]');
        if (add) openPicker(add.dataset.addResource);
        const remove = event.target.closest('[data-unlink-resource]');
        if (remove) unlink(remove.dataset.unlinkResource, Number(remove.dataset.resourceId)).catch(error => window.alert(error.message || '移除失败'));
        const copyButton = event.target.closest('[data-copy-resource]');
        if (copyButton) copy(copyButton.dataset.copyResource).then(() => { copyButton.textContent = '已复制'; setTimeout(() => { copyButton.textContent = '复制拉取地址'; }, 1200); }).catch(() => window.alert('复制失败，请手工复制地址'));
    });
    $('at-resource-options').addEventListener('click', event => {
        const button = event.target.closest('[data-link-resource]');
        if (button && !button.disabled) link(Number(button.dataset.linkResource), button);
    });
    $('at-resource-search').addEventListener('input', renderOptions);
    $('at-resource-close').addEventListener('click', () => $('at-resource-dialog').close());
    return {reset, refresh: load, mount: async value => { team = value; ++epoch; await load(); }};
})();
