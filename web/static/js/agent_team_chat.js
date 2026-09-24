/* Durable Team room: discussion is visible; execution still belongs to Stage/Run. */
const AgentTeamChat = (() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let team = null, roomId = null, epoch = 0, requestId = 0, timer = null, selected = new Set(), sending = false;
    let pendingImages = [];
    let rendered = '', firstLoad = true;
    const roundLabels = {open:'等待回复',completed:'已收齐',partial:'部分回复',failed:'回复失败',expired:'已超时'};
    const deliveryLabels = {unread:'待送达',delivered:'已送达',processing:'处理中',replied:'已回复',done:'已完成',failed:'失败',missing:'成员已变化'};
    function visible() { return team && !$('at-chat-panel')?.hidden && !document.hidden; }
    function reset() {
        closeImagePreview();
        ++epoch; ++requestId; team = null; roomId = null; selected.clear(); clearPendingImages(); rendered = ''; firstLoad = true;
        if (timer) clearTimeout(timer); timer = null;
    }
    function frame() {
        $('at-chat-root').innerHTML = `<section class="at-chat-workspace">
            <header class="at-chat-head"><div><span class="at-eyebrow">TEAM CHANNEL</span><h3>固定团队聊天室</h3><p>同一团队始终使用这个房间。@ 才会唤醒 Agent；普通消息只沉淀上下文。</p></div><div class="at-chat-signal"><i></i><span id="at-chat-room-state">正在连接</span></div></header>
            <div class="at-chat-layout"><main class="at-chat-main"><div id="at-chat-stream" class="at-chat-stream" tabindex="0" aria-label="团队聊天室消息"><p class="at-empty">正在读取团队聊天室…</p></div>
            <form id="at-chat-form" class="at-chat-compose"><div id="at-chat-image-preview" class="at-chat-image-preview" hidden></div><textarea id="at-chat-input" rows="3" maxlength="20000" placeholder="输入讨论内容；可直接 Ctrl+V 粘贴图片，需要 Agent 回复时点击右侧成员的 @ 按钮"></textarea><footer><span id="at-chat-compose-note">未 @ Agent，本条仅记录 · Enter 发送 · Ctrl+V 粘贴图片</span><div class="at-chat-actions"><input id="at-chat-image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden><button class="btn btn-secondary btn-sm" id="at-chat-image-button" type="button">图片</button><button class="btn btn-primary btn-sm" id="at-chat-send" type="submit">发送</button></div></footer></form></main>
            <aside id="at-chat-side" class="at-chat-side"><header><strong>在场成员</strong><div><button type="button" class="at-chat-mention-all" data-chat-mention="all" aria-pressed="false">@ 全部</button><span id="at-chat-member-count">—</span></div></header><div id="at-chat-members"></div><div class="at-chat-rule"><b>频道边界</b><span>讨论、问答和结论确认留在这里。</span><span>真正开工、重试、取消仍必须走 Stage / Run。</span></div></aside></div>
            <p id="at-chat-note" class="at-help" role="status"></p></section>
            <dialog id="at-chat-lightbox" class="at-chat-lightbox" aria-labelledby="at-chat-lightbox-caption">
                <button type="button" class="at-chat-lightbox-close" data-chat-lightbox-close aria-label="关闭图片预览">×</button>
                <figure><img id="at-chat-lightbox-image" alt=""><figcaption id="at-chat-lightbox-caption"></figcaption></figure>
            </dialog>`;
        $('at-chat-form').addEventListener('submit', send);
        $('at-chat-input').addEventListener('keydown', event => {
            if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('at-chat-form').requestSubmit(); }
        });
        $('at-chat-input').addEventListener('paste', pasteImages);
        $('at-chat-image-button').addEventListener('click', () => $('at-chat-image-input').click());
        $('at-chat-image-input').addEventListener('change', chooseImages);
        $('at-chat-image-preview').addEventListener('click', event => {
            const button = event.target.closest('[data-remove-image]'); if (!button) return;
            const index = Number(button.dataset.removeImage), item = pendingImages[index];
            if (item?.preview) URL.revokeObjectURL(item.preview);
            pendingImages.splice(index, 1); renderPendingImages();
        });
        $('at-chat-stream').addEventListener('click', event => {
            const button = event.target.closest('[data-chat-image]');
            if (button) openImagePreview(button.dataset.chatImage, button.dataset.chatImageAlt);
        });
        $('at-chat-lightbox').addEventListener('click', event => {
            if (event.target === event.currentTarget || event.target.closest('[data-chat-lightbox-close]')) closeImagePreview();
        });
        $('at-chat-lightbox').addEventListener('close', clearImagePreview);
        $('at-chat-side').addEventListener('click', event => {
            const button = event.target.closest('[data-chat-mention]'); if (!button) return;
            const value = button.dataset.chatMention;
            if (value === 'all') { selected.clear(); selected.add('all'); }
            else { selected.delete('all'); selected.has(value) ? selected.delete(value) : selected.add(value); }
            renderMentionSelection();
            $('at-chat-input').focus({preventScroll:true});
        });
    }
    function openImagePreview(url, label) {
        const dialog = $('at-chat-lightbox'), image = $('at-chat-lightbox-image'), caption = $('at-chat-lightbox-caption');
        if (!dialog || !image || !url) return;
        const text = label || '聊天室图片';
        image.src = url; image.alt = text;
        if (caption) caption.textContent = text;
        if (!dialog.open) dialog.showModal();
    }
    function clearImagePreview() {
        const image = $('at-chat-lightbox-image'), caption = $('at-chat-lightbox-caption');
        if (image) { image.removeAttribute('src'); image.alt = ''; }
        if (caption) caption.textContent = '';
    }
    function closeImagePreview() {
        const dialog = $('at-chat-lightbox');
        if (dialog?.open) dialog.close();
        else clearImagePreview();
    }
    function clearPendingImages() {
        pendingImages.forEach(item => { if (item.preview) URL.revokeObjectURL(item.preview); });
        pendingImages = [];
    }
    function queueImages(files, source = 'picker') {
        const allowed = new Set(['image/png','image/jpeg','image/gif','image/webp']);
        let added = 0;
        for (const file of files) {
            if (pendingImages.length >= 4) { $('at-chat-note').textContent = '每条消息最多发送 4 张图片。'; break; }
            if (!allowed.has(file.type) || file.size > 8 * 1024 * 1024) { $('at-chat-note').textContent = '仅支持 PNG、JPEG、GIF、WebP，单张不超过 8 MB。'; continue; }
            pendingImages.push({file, preview:URL.createObjectURL(file), id:null, uploadKey:(crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`)});
            added += 1;
        }
        renderPendingImages();
        if (source === 'clipboard' && added) $('at-chat-note').textContent = `已从剪贴板添加 ${added} 张图片，可继续输入文字后一起发送。`;
        return added;
    }
    function chooseImages(event) {
        queueImages([...(event.target.files || [])]);
        event.target.value = '';
    }
    function pasteImages(event) {
        const clipboard = event.clipboardData;
        if (!clipboard) return;
        const files = [...(clipboard.items || [])]
            .filter(item => item.kind === 'file' && String(item.type || '').startsWith('image/'))
            .map(item => item.getAsFile()).filter(Boolean);
        if (!files.length) return;
        // 纯截图剪贴板不应在 textarea 中留下浏览器生成的无意义占位；若同时
        // 带有文字则保留浏览器默认的文字粘贴，并把图片作为同一条消息附件。
        if (!clipboard.getData('text/plain')) event.preventDefault();
        queueImages(files, 'clipboard');
    }
    function renderPendingImages() {
        const box = $('at-chat-image-preview'); if (!box) return;
        box.hidden = !pendingImages.length;
        box.innerHTML = pendingImages.map((item,index) => `<figure><img src="${item.preview}" alt="${esc(item.file.name)}"><button type="button" data-remove-image="${index}" aria-label="移除 ${esc(item.file.name)}">×</button><figcaption>${esc(item.file.name)}</figcaption></figure>`).join('');
    }
    async function uploadPendingImages(roomId) {
        for (const item of pendingImages) {
            if (item.id) continue;
            const body = new FormData(); body.append('file', item.file, item.file.name);
            const response = await fetch(`${API.base}/chat-rooms/${roomId}/images`, {method:'POST', headers:{'Idempotency-Key':item.uploadKey}, body});
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || `图片上传失败（HTTP ${response.status}）`);
            item.id = data.id;
        }
        return pendingImages.map(item => item.id);
    }
    function renderMentionSelection() {
        $('at-chat-side')?.querySelectorAll('[data-chat-mention]').forEach(button => {
            const active = selected.has(button.dataset.chatMention);
            button.classList.toggle('selected', active);
            button.setAttribute('aria-pressed', String(active));
        });
        const count = selected.has('all') ? '将唤醒全部 Agent' : selected.size ? `将唤醒 ${selected.size} 个 Agent` : '未 @ Agent，本条仅记录';
        if ($('at-chat-compose-note')) $('at-chat-compose-note').textContent = `${count} · Enter 发送 · Ctrl+V 粘贴图片`;
    }
    function time(value) {
        if (!value) return '';
        const normalized = String(value).replace(' ', 'T') + (String(value).includes('+') ? '' : '+08:00');
        const date = new Date(normalized);
        return Number.isNaN(date.getTime()) ? String(value).slice(5,16) : new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
    }
    function render(data) {
        const room = data.room, members = data.members || [], rounds = new Map((data.rounds || []).map(item => [item.question_message_id, item]));
        roomId = room.id;
        $('at-chat-room-state').textContent = `Room #${room.id} · ${rounds.size} 轮问答`;
        $('at-chat-member-count').textContent = `${members.length} 人`;
        const me = room.my_member?.id;
        const agentIds = new Set(members.filter(member => member.member_type === 'agent' && member.id !== me).map(member => String(member.claw_id)));
        if (!selected.has('all')) selected = new Set([...selected].filter(value => agentIds.has(value)));
        $('at-chat-members').innerHTML = members.map(member => {
            const canMention = member.member_type === 'agent' && member.id !== me;
            const mention = canMention ? `<button type="button" class="at-chat-member-mention" data-chat-mention="${member.claw_id}" aria-pressed="false" aria-label="@ ${esc(member.display_name)}" title="@ ${esc(member.display_name)}">@</button>` : '';
            return `<div class="at-chat-member"><span class="at-chat-avatar ${member.member_type}">${member.member_type === 'agent' ? 'AI' : esc((member.display_name || '?').slice(0,1))}</span><div><strong>${esc(member.display_name)}</strong><small>${member.member_type === 'agent' ? `Claw #${member.claw_id}` : member.role === 'owner' ? '房主' : '项目成员'}</small></div>${mention}<i class="${member.status === 'active' ? 'online' : ''}"></i></div>`;
        }).join('');
        const mentionAll = $('at-chat-side').querySelector('[data-chat-mention="all"]');
        mentionAll.disabled = !agentIds.size;
        renderMentionSelection();
        const messages = data.messages || [], signature = JSON.stringify(messages.map(message => [message.id,message.content,message.created_at,(message.images||[]).map(image=>[image.id,image.sha256])])) + JSON.stringify(data.rounds || []);
        if (signature === rendered) return;
        rendered = signature;
        const stream = $('at-chat-stream'), nearBottom = stream.scrollHeight - stream.scrollTop <= stream.clientHeight + 90;
        stream.innerHTML = messages.length ? messages.map(message => {
            const mine = message.sender_member_id === me, sender = message.sender || {}, round = rounds.get(message.id);
            const mentionText = (message.mentions || []).map(item => item.type === 'all' ? '@所有人' : `@${members.find(member => member.id === item.member_id)?.display_name || '成员'}`).join(' ');
            const roundHtml = round ? `<div class="at-chat-round ${esc(round.status)}"><header><strong>${esc(roundLabels[round.status] || round.status)}</strong><span>${round.replied_count}/${round.expected_count} 已回复</span><time>截止 ${esc(time(round.deadline_at))}</time></header><div>${round.members.map(item => `<span class="${esc(item.status)}"><i></i>${esc(item.name)} · ${esc(deliveryLabels[item.status] || item.status)}</span>`).join('')}</div></div>` : '';
            const images = (message.images || []).map(image => `<button type="button" class="at-chat-image" data-chat-image="${esc(image.url)}" data-chat-image-alt="${esc(image.name || '聊天室图片')}" aria-label="放大查看 ${esc(image.name || '聊天室图片')}"><img src="${esc(image.url)}" alt="${esc(image.name || '聊天室图片')}" loading="lazy"></button>`).join('');
            return `<article class="at-chat-message ${mine ? 'mine' : ''}" data-message-id="${message.id}"><span class="at-chat-avatar ${esc(sender.member_type || '')}">${sender.member_type === 'agent' ? 'AI' : esc((sender.display_name || '?').slice(0,1))}</span><div class="at-chat-bubble"><header><strong>${esc(sender.display_name || '未知成员')}</strong><time>${esc(time(message.created_at))}</time></header>${mentionText ? `<div class="at-chat-mentioned">${esc(mentionText)}</div>` : ''}${message.content ? `<p>${esc(message.content)}</p>` : ''}${images ? `<div class="at-chat-images">${images}</div>` : ''}${roundHtml}</div></article>`;
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
        const input = $('at-chat-input'), content = input.value.trim(); if (!content && !pendingImages.length) return;
        const generation = epoch, id = team.id, all = selected.has('all');
        const ids = all ? [] : [...selected].map(Number);
        sending = true; $('at-chat-send').disabled = true;
        try {
            $('at-chat-note').textContent = pendingImages.length ? '正在安全上传图片…' : '正在发送…';
            if (!roomId) throw new Error('聊天室尚未完成同步');
            const imageIds = await uploadPendingImages(roomId);
            const key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
            await API.request('POST', `/agent-teams/${id}/chat-room/messages`,
                {content, image_ids:imageIds, mention_all:all, mention_claw_ids:ids, timeout_seconds:300},
                {'Idempotency-Key':key});
            if (generation !== epoch) return;
            input.value = ''; selected.clear(); clearPendingImages(); renderPendingImages(); await refresh();
        } catch (error) { if (generation === epoch) $('at-chat-note').textContent = `发送失败：${error.message || '请稍后重试'}`; }
        finally { sending = false; if ($('at-chat-send')) $('at-chat-send').disabled = false; }
    }
    document.addEventListener('visibilitychange', () => { if (visible()) refresh(true); });
    return {reset, refresh, mount: async value => { team = value; selected.clear(); clearPendingImages(); rendered=''; firstLoad=true; frame(); if (!$('at-chat-panel').hidden) await refresh(); }};
})();
