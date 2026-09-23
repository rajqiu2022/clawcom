/* Durable Team room: discussion is visible; execution still belongs to Stage/Run. */
const AgentTeamChat = (() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let team = null, epoch = 0, requestId = 0, timer = null, selected = new Set(), sending = false;
    let rendered = '', firstLoad = true;
    const roundLabels = {open:'等待回复',completed:'已收齐',partial:'部分回复',failed:'回复失败',expired:'已超时'};
    const deliveryLabels = {unread:'待送达',delivered:'已送达',processing:'处理中',replied:'已回复',done:'已完成',failed:'失败',missing:'成员已变化'};
    function visible() { return team && !$('at-chat-panel')?.hidden && !document.hidden; }
    function reset() {
        ++epoch; ++requestId; team = null; selected.clear(); rendered = ''; firstLoad = true;
        if (timer) clearTimeout(timer); timer = null;
    }
    function frame() {
        $('at-chat-root').innerHTML = `<section class="at-chat-workspace">
            <header class="at-chat-head"><div><span class="at-eyebrow">TEAM CHANNEL</span><h3>固定团队聊天室</h3><p>同一团队始终使用这个房间。@ 才会唤醒 Agent；普通消息只沉淀上下文。</p></div><div class="at-chat-signal"><i></i><span id="at-chat-room-state">正在连接</span></div></header>
            <div class="at-chat-layout"><main class="at-chat-main"><div id="at-chat-stream" class="at-chat-stream" tabindex="0" aria-label="团队聊天室消息"><p class="at-empty">正在读取团队聊天室…</p></div>
            <form id="at-chat-form" class="at-chat-compose"><div id="at-chat-mentions" class="at-chat-mentions"></div><textarea id="at-chat-input" rows="3" maxlength="20000" placeholder="输入讨论内容；需要 Agent 回复时先选择 @成员"></textarea><footer><span id="at-chat-compose-note">Enter 发送 · Shift+Enter 换行</span><button class="btn btn-primary btn-sm" id="at-chat-send" type="submit">发送</button></footer></form></main>
            <aside class="at-chat-side"><header><strong>在场成员</strong><span id="at-chat-member-count">—</span></header><div id="at-chat-members"></div><div class="at-chat-rule"><b>频道边界</b><span>讨论、问答和结论确认留在这里。</span><span>真正开工、重试、取消仍必须走 Stage / Run。</span></div></aside></div>
            <p id="at-chat-note" class="at-help" role="status"></p></section>`;
        $('at-chat-form').addEventListener('submit', send);
        $('at-chat-input').addEventListener('keydown', event => {
            if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('at-chat-form').requestSubmit(); }
        });
        $('at-chat-mentions').addEventListener('click', event => {
            const button = event.target.closest('[data-chat-mention]'); if (!button) return;
            const value = button.dataset.chatMention;
            if (value === 'all') { selected.clear(); selected.add('all'); }
            else { selected.delete('all'); selected.has(value) ? selected.delete(value) : selected.add(value); }
            renderMentionSelection();
        });
    }
    function renderMentionSelection() {
        $('at-chat-mentions')?.querySelectorAll('[data-chat-mention]').forEach(button => {
            button.classList.toggle('selected', selected.has(button.dataset.chatMention));
        });
        const count = selected.has('all') ? '将唤醒全部 Agent' : selected.size ? `将唤醒 ${selected.size} 个 Agent` : '未 @ Agent，本条仅记录';
        if ($('at-chat-compose-note')) $('at-chat-compose-note').textContent = `${count} · Enter 发送`;
    }
    function time(value) {
        if (!value) return '';
        const normalized = String(value).replace(' ', 'T') + (String(value).includes('+') ? '' : '+08:00');
        const date = new Date(normalized);
        return Number.isNaN(date.getTime()) ? String(value).slice(5,16) : new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
    }
    function render(data) {
        const room = data.room, members = data.members || [], rounds = new Map((data.rounds || []).map(item => [item.question_message_id, item]));
        $('at-chat-room-state').textContent = `Room #${room.id} · ${rounds.size} 轮问答`;
        $('at-chat-member-count').textContent = `${members.length} 人`;
        $('at-chat-members').innerHTML = members.map(member => `<div class="at-chat-member"><span class="at-chat-avatar ${member.member_type}">${member.member_type === 'agent' ? 'AI' : esc((member.display_name || '?').slice(0,1))}</span><div><strong>${esc(member.display_name)}</strong><small>${member.member_type === 'agent' ? `Claw #${member.claw_id}` : member.role === 'owner' ? '房主' : '项目成员'}</small></div><i class="${member.status === 'active' ? 'online' : ''}"></i></div>`).join('');
        const me = room.my_member?.id;
        $('at-chat-mentions').innerHTML = `<button type="button" class="at-chat-mention" data-chat-mention="all">@ 所有人</button>` + members.filter(member => member.member_type === 'agent' && member.id !== me).map(member => `<button type="button" class="at-chat-mention" data-chat-mention="${member.claw_id}">@ ${esc(member.display_name)}</button>`).join('');
        renderMentionSelection();
        const messages = data.messages || [], signature = JSON.stringify(messages.map(message => [message.id,message.content,message.created_at])) + JSON.stringify(data.rounds || []);
        if (signature === rendered) return;
        rendered = signature;
        const stream = $('at-chat-stream'), nearBottom = stream.scrollHeight - stream.scrollTop <= stream.clientHeight + 90;
        stream.innerHTML = messages.length ? messages.map(message => {
            const mine = message.sender_member_id === me, sender = message.sender || {}, round = rounds.get(message.id);
            const mentionText = (message.mentions || []).map(item => item.type === 'all' ? '@所有人' : `@${members.find(member => member.id === item.member_id)?.display_name || '成员'}`).join(' ');
            const roundHtml = round ? `<div class="at-chat-round ${esc(round.status)}"><header><strong>${esc(roundLabels[round.status] || round.status)}</strong><span>${round.replied_count}/${round.expected_count} 已回复</span><time>截止 ${esc(time(round.deadline_at))}</time></header><div>${round.members.map(item => `<span class="${esc(item.status)}"><i></i>${esc(item.name)} · ${esc(deliveryLabels[item.status] || item.status)}</span>`).join('')}</div></div>` : '';
            return `<article class="at-chat-message ${mine ? 'mine' : ''}" data-message-id="${message.id}"><span class="at-chat-avatar ${esc(sender.member_type || '')}">${sender.member_type === 'agent' ? 'AI' : esc((sender.display_name || '?').slice(0,1))}</span><div class="at-chat-bubble"><header><strong>${esc(sender.display_name || '未知成员')}</strong><time>${esc(time(message.created_at))}</time></header>${mentionText ? `<div class="at-chat-mentioned">${esc(mentionText)}</div>` : ''}<p>${esc(message.content)}</p>${roundHtml}</div></article>`;
        }).join('') : '<div class="at-empty">频道已经建立。发送第一条消息开始团队协作。</div>';
        if (firstLoad || nearBottom) stream.scrollTop = stream.scrollHeight;
        firstLoad = false;
    }
    async function refresh(silent = false) {
        if (!team || !visible()) return;
        const generation = epoch, turn = ++requestId, id = team.id;
        if (!silent) $('at-chat-note').textContent = '正在同步团队频道…';
        try {
            const data = await API.get(`/agent-teams/${id}/chat-room?limit=120`);
            if (generation !== epoch || turn !== requestId || team?.id !== id) return;
            render(data); $('at-chat-note').textContent = '频道内容与问答回执已同步。';
        } catch (error) {
            if (generation === epoch && turn === requestId) $('at-chat-note').textContent = `聊天室读取失败：${error.message || '请稍后重试'}`;
        } finally {
            if (generation === epoch && team?.id === id) { if (timer) clearTimeout(timer); timer = setTimeout(() => refresh(true), 5000); }
        }
    }
    async function send(event) {
        event.preventDefault(); if (sending || !team) return;
        const input = $('at-chat-input'), content = input.value.trim(); if (!content) return;
        const generation = epoch, id = team.id, all = selected.has('all');
        const ids = all ? [] : [...selected].map(Number);
        sending = true; $('at-chat-send').disabled = true;
        try {
            const key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
            await API.request('POST', `/agent-teams/${id}/chat-room/messages`,
                {content, mention_all:all, mention_claw_ids:ids, timeout_seconds:300},
                {'Idempotency-Key':key});
            if (generation !== epoch) return;
            input.value = ''; selected.clear(); await refresh();
        } catch (error) { if (generation === epoch) $('at-chat-note').textContent = `发送失败：${error.message || '请稍后重试'}`; }
        finally { sending = false; if ($('at-chat-send')) $('at-chat-send').disabled = false; }
    }
    document.addEventListener('visibilitychange', () => { if (visible()) refresh(true); });
    return {reset, refresh, mount: async value => { team = value; selected.clear(); rendered=''; firstLoad=true; frame(); if (!$('at-chat-panel').hidden) await refresh(); }};
})();
