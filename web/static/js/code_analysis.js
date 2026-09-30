/* Project code analysis. No untrusted HTML or executable report content here. */
const CA = {project: '', tab: 'runs', options: null, page: 1, job: null, loadEpoch:0, detailEpoch:0};
const caEl = id => document.getElementById(id);
const caEsc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const caKey = () => 'ca-' + crypto.randomUUID();
const caWrite = (method, path, data) => API.request(method, path, data, {'Idempotency-Key': caKey()});
function caError(error) { const message=error?.message || String(error);caEl('ca-error').textContent=message;if(caEl('ca-dialog').open&&caEl('ca-modal-error'))caEl('ca-modal-error').textContent=message; }
function caDialog(title, content) {
 caEl('ca-dialog-title').textContent = title;
 caEl('ca-dialog-content').innerHTML = '<div id="ca-modal-error" class="ca-error" role="alert"></div>'+content;
 if (!caEl('ca-dialog').open) caEl('ca-dialog').showModal();
}
// Nested native dialogs stay above the existing modal's top layer.
function caAsk(message, value=null) {
 return new Promise(resolve=>{const d=document.createElement('dialog');d.className='ca-dialog';d.style.maxWidth='540px';d.innerHTML=`<main><form method="dialog"><p>${caEsc(message)}</p>${value===null?'':`<label class="ca-field">内容<textarea name="value" required>${caEsc(value)}</textarea></label>`}<div class="ca-actions" style="margin-top:16px"><button class="btn btn-ghost" type="button" data-cancel>取消</button><button class="btn btn-primary" value="yes">确认</button></div></form></main>`;document.body.append(d);d.querySelector('[data-cancel]').onclick=()=>d.close('cancel');d.addEventListener('close',()=>{const answer=d.returnValue==='yes'?(value===null?true:d.querySelector('textarea').value):false;d.remove();resolve(answer);},{once:true});d.showModal();});
}
async function caPublishGeneral(decision) {
 try{const text=await caAsk('审核并移除项目专属信息后，发布为通用经验。不会自动复制源码或业务配置。',decision.general_proposal);if(!text)return;const page=CA.options.knowledge.find(k=>k.project_id===null);if(!page)throw new Error('通用知识库尚未初始化');const current=await API.get(`/knowledge/journal-pages/${page.id}`);await caWrite('POST',`/code-analysis/decisions/${decision.id}/publish-general`,{confirmed:true,content:text,expected_revision:current.current_revision});await caLoad();await caJob(CA.job.id);}catch(e){caError(e);}
}
function caStatus(status) {return ({pending:'待执行',running:'执行中',succeeded:'成功',failed:'失败',blocked:'阻断',cancelled:'已取消',completed:'已完成',completed_with_findings:'已完成 · 有候选',dispatched:'已派发',learned:'已学习',superseded:'已被新标注替代',unknown:'待核验',submitted:'已提单'})[status] || status || '—';}
function caOptions(items, value, label='title') {return items.map(i=>`<option value="${Number(i.id)}" ${String(i.id)===String(value)?'selected':''}>#${Number(i.id)} ${caEsc(i[label])}</option>`).join('');}
async function caLoad() {
 const epoch=++CA.loadEpoch;
 if (!CA.project) {CA.options=null;caEl('ca-content').textContent='请选择项目';return;}
 caEl('ca-content').textContent='正在加载…';
 caEl('ca-error').textContent='';
 try {
  const options = await API.get(`/code-analysis/options?project_id=${encodeURIComponent(CA.project)}`);
  if(epoch!==CA.loadEpoch)return;
  CA.options=options;
  await caRender();
 } catch(e) { if(epoch!==CA.loadEpoch)return;caError(e); caEl('ca-content').innerHTML='<div class="ca-empty">暂时无法加载专项，请检查功能启用状态与项目权限。</div>'; }
}
async function caRender() {
 if(CA.tab==='settings') return caSettings();
 if(CA.tab==='knowledge') return caResources();
 const epoch=CA.loadEpoch;
 const params = new URLSearchParams({project_id:CA.project,page:CA.page});
 const plan = new URLSearchParams(location.search).get('plan_id');
 if(plan) params.set('plan_id',plan);
 const data = await API.get('/code-analysis/runs?'+params);
 if(epoch!==CA.loadEpoch||CA.tab!=='runs')return;
 caEl('ca-content').innerHTML=`<div class="ca-panel"><div class="ca-toolbar"><div><h3 style="margin:0">分析记录</h3><span class="ca-note">共 ${Number(data.total)} 次 ${plan?'· 计划 #'+Number(plan):''}</span></div><div><button class="btn btn-ghost" id="ca-refresh">刷新</button> <button class="btn btn-primary" id="ca-new">创建分析</button></div></div>
 <div class="ca-scroll"><table class="ca-table"><thead><tr><th>分析 / 计划</th><th>代码基线</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${data.items.map(j=>`<tr><td>#${j.id} · Plan #${j.plan_id}<br>${caEsc(j.snapshot.plan.name)}</td><td>${caEsc(j.snapshot.source_ref)}<br><span class="ca-note">${caEsc(j.baseline.base_sha?.slice(0,12)||j.snapshot.baseline_ref)} → ${caEsc(j.baseline.target_sha?.slice(0,12)||'执行时解析')}</span></td><td>${caEsc(caStatus(j.status))}<br><span class="ca-note">Workflow ${caEsc(caStatus(j.workflow_status))}</span></td><td>${caEsc(j.created_at)}</td><td><button class="btn btn-ghost" data-job="${j.id}">详情</button> ${j.workflow_run_id?`<a href="/workflows?run_id=${j.workflow_run_id}" class="btn btn-ghost">Run #${j.workflow_run_id}</a>`:`<button class="btn btn-ghost" data-start="${j.id}">启动</button>`} ${j.report_id?`<a class="btn btn-ghost" href="/test-reports/${j.report_id}">报告 #${j.report_id}</a>`:''}</td></tr>`).join('')}</tbody></table></div>${data.items.length?'':'<div class="ca-empty">尚无分析记录。先配置初始 Skill，再选择测试计划创建分析。</div>'}
 <div class="ca-pager"><span>${data.page} / ${Math.max(1,Math.ceil(data.total/data.page_size))} 页</span><button class="btn btn-ghost" id="ca-prev" ${data.page<=1?'disabled':''}>上一页</button><button class="btn btn-ghost" id="ca-next" ${data.page*data.page_size>=data.total?'disabled':''}>下一页</button></div></div>`;
 caEl('ca-new').onclick=caCreateDialog; caEl('ca-refresh').onclick=()=>caLoad();
 caEl('ca-prev').onclick=()=>{CA.page--;caLoad();}; caEl('ca-next').onclick=()=>{CA.page++;caLoad();};
 document.querySelectorAll('[data-job]').forEach(b=>b.onclick=()=>caJob(b.dataset.job));
 document.querySelectorAll('[data-start]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await caWrite('POST',`/code-analysis/runs/${b.dataset.start}/start`,{});await caLoad();}catch(e){caError(e);b.disabled=false;}});
}
function caSettings() {
 const o=CA.options,c=o.config;
 if(!c){caEl('ca-content').innerHTML='<div class="ca-panel"><h3>初始化专项经验库</h3><p class="ca-note">复用现有知识库，创建一篇空白通用库与一篇本项目经验库；不会覆盖已有文档。</p><button class="btn btn-primary" id="ca-init">初始化</button></div>';caEl('ca-init').onclick=async()=>{try{await caWrite('POST',`/code-analysis/projects/${CA.project}/initialize`,{});await caLoad();}catch(e){caError(e);}};return;}
 caEl('ca-content').innerHTML=`<form class="ca-panel" id="ca-config-form"><h3>分析配置</h3><p class="ca-note">初始 Skill 定义分析方法。凭据使用 Worker 配置；代码默认只读，不自动合并或推送。</p><div class="ca-grid">
 <label class="ca-field">执行 Agent<select id="ca-agent" required><option value="">请选择</option>${caOptions(o.agents,c.executor_claw_id,'name')}</select></label>
 <label class="ca-field">初始 Skill<select id="ca-skill" required><option value="">请选择</option>${caOptions(o.skills,c.skill_id)}</select></label>
 <label class="ca-field ca-wide">代码仓库 URL<input id="ca-repo" value="${caEsc(c.repository_url)}" placeholder="https://git.example.com/project/repo.git 或 ssh://git@host/repo.git" required></label>
 <label class="ca-field">源分支<input id="ca-ref" value="${caEsc(c.source_ref||'main')}" required></label>
 <label class="ca-field">项目经验库<select id="ca-project-knowledge" required>${caOptions(o.knowledge.filter(k=>Number(k.project_id)===Number(CA.project)),c.knowledge_id)}</select></label>
 <label class="ca-field ca-wide">补充知识（可多选跨项目共享知识）<select id="ca-extra-knowledge" multiple size="5">${o.knowledge.filter(k=>k.project_id!==null&&k.id!==c.knowledge_id).map(k=>`<option value="${k.id}" ${(c.knowledge_ids||[]).includes(k.id)?'selected':''}>#${k.id} ${caEsc(k.title)}</option>`).join('')}</select><span class="ca-note">按 Ctrl / Command 多选；通用库与主项目经验库默认关联。</span></label>
 </div><div class="ca-actions" style="margin-top:20px"><span class="ca-note">配置修改只影响新分析；历史快照不会被覆盖。</span><button class="btn btn-primary" type="submit">保存配置</button></div></form>`;
 caEl('ca-config-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{await caWrite('PUT',`/code-analysis/projects/${CA.project}`,{expected_revision:c.revision,executor_claw_id:Number(caEl('ca-agent').value),skill_id:Number(caEl('ca-skill').value),repository_url:caEl('ca-repo').value,source_ref:caEl('ca-ref').value,knowledge_id:Number(caEl('ca-project-knowledge').value),knowledge_ids:[...caEl('ca-extra-knowledge').selectedOptions].map(o=>Number(o.value))});await caLoad();showToast('分析配置已保存','success');}catch(err){caError(err);b.disabled=false;}};
}
function caCreateDialog() {
 const o=CA.options;
 if(!o.config?.skill_id){CA.tab='settings';caActivateTab();caRender();return;}
 caDialog('创建代码分析',`<form id="ca-create-form"><div class="ca-grid"><label class="ca-field ca-wide">测试计划<select id="ca-plan" required><option value="">请选择测试计划</option>${caOptions(o.plans,new URLSearchParams(location.search).get('plan_id'),'name')}</select></label><label class="ca-field ca-wide">起始基线<input id="ca-baseline" placeholder="固定 commit SHA / tag / 分支引用" required></label></div><p class="ca-note">运行时解析为固定 commit，并结合该计划的任务范围、知识版本和历史人工反馈进行分析。已完成计划也可用于历史复盘。</p><button class="btn btn-primary" type="submit">创建并启动</button></form>`);
 caEl('ca-create-form').onsubmit=async e=>{e.preventDefault();e.submitter.disabled=true;try{const job=await caWrite('POST','/code-analysis/runs',{plan_id:Number(caEl('ca-plan').value),baseline_ref:caEl('ca-baseline').value});caEl('ca-dialog').close();await caLoad();await caWrite('POST',`/code-analysis/runs/${job.id}/start`,{});await caLoad();}catch(err){caError(err);if(e.submitter)e.submitter.disabled=false;}};
}
async function caJob(id) {
 const epoch=++CA.detailEpoch;
 try{const j=await API.get(`/code-analysis/runs/${Number(id)}`);if(epoch!==CA.detailEpoch)return;CA.job=j;
 caDialog(`分析 #${j.id} · Plan #${j.plan_id}`,`<p class="ca-note">${caEsc(j.snapshot.plan.name)} · ${caEsc(caStatus(j.status))} · ${caEsc(j.created_at)}</p><div class="ca-actions"><div>${j.report_id?`<a class="btn btn-ghost" href="/test-reports/${j.report_id}">完整报告 #${j.report_id}</a>`:''}</div><button class="btn btn-primary" id="ca-batch">处置选中问题</button></div><div class="ca-scroll"><table class="ca-table"><thead><tr><th><input type="checkbox" id="ca-select-all" aria-label="选择全部问题"></th><th>潜在 Bug / 证据</th><th>最近人工处置</th><th>学习 / 提单</th><th>操作</th></tr></thead><tbody>${j.findings.map(f=>{const d=f.decisions[0];return `<tr><td><input type="checkbox" data-find="${f.id}" aria-label="选择问题 #${f.id}"></td><td><strong>#${f.id} ${caEsc(f.observation.title||f.title)}</strong><br><span class="ca-note">${caEsc(f.severity)} · ${caEsc(f.module)}</span><details><summary>分析依据</summary><pre>${caEsc(f.observation.description||f.description)}</pre><pre>${caEsc(JSON.stringify(f.observation.code_locations||f.code_locations,null,2))}</pre></details></td><td>${d?`${caEsc({submit:'直接提单',confirm:'待人工确认',ignore:'忽略'}[d.action])}<br>${caEsc(d.reason)}<br><span class="ca-note">${caEsc(d.actor_name)} · ${caEsc(d.created_at)}</span>`:'尚未标注'}</td><td>${d?`${caEsc(caStatus(d.learning_status))}${d.learned_revision?' · r'+d.learned_revision:''}<br>${caEsc(caStatus(d.submission_status))}${d.bug_id?' · #'+caEsc(d.bug_id):''}`:'—'}</td><td><button class="btn btn-ghost" data-triage="${f.id}">标注</button>${d?`<button class="btn btn-ghost" data-history="${f.id}">反馈记录</button>`:''}</td></tr>`;}).join('')}</tbody></table></div>${j.findings.length?'':'<div class="ca-empty">尚无候选结果；分析完成后在这里进行人工确认。</div>'}`);
 caEl('ca-select-all').onchange=e=>document.querySelectorAll('[data-find]').forEach(b=>b.checked=e.target.checked);
 caEl('ca-batch').onclick=()=>caTriage([...document.querySelectorAll('[data-find]:checked')].map(b=>Number(b.dataset.find)));
 document.querySelectorAll('[data-triage]').forEach(b=>b.onclick=()=>caTriage([Number(b.dataset.triage)]));
 document.querySelectorAll('[data-history]').forEach(b=>b.onclick=()=>caHistory(Number(b.dataset.history)));
 }catch(e){caError(e);}
}
function caTriage(ids) {
 if(!ids.length){showToast('请先选择问题','error');return;}
 const job=CA.job,findings=job.findings.filter(f=>ids.includes(f.id));
 caDialog(`人工处置 · ${ids.length} 个候选`, `<form id="ca-triage-form"><p>${findings.map(f=>`#${f.id} ${caEsc(f.title)}`).join('<br>')}</p><div class="ca-grid"><label class="ca-field">处置动作<select id="ca-action"><option value="confirm">待人工确认</option><option value="submit">直接提单</option><option value="ignore">忽略</option></select></label><label class="ca-field">事实标注<select id="ca-truth"><option value="unknown">尚未确认</option><option value="true_positive">真实 Bug</option><option value="false_positive">误报</option><option value="duplicate_bug">重复 Bug</option><option value="risk_accepted">风险接受（不算误报）</option></select></label><label class="ca-field ca-wide">判断依据<textarea id="ca-reason" required placeholder="说明确认依据、误报原因或接受风险的原因"></textarea></label></div><p class="ca-note">每次操作保留历史，并派发反馈学习。直接提单会在保存人工标注后向 TAPD 创建 Bug。</p><button class="btn btn-primary" type="submit">保存处置</button><div id="ca-triage-receipts" class="ca-note"></div></form>`);
 caEl('ca-action').onchange=e=>{if(e.target.value==='submit')caEl('ca-truth').value='true_positive';};
 caEl('ca-triage-form').onsubmit=async e=>{e.preventDefault();const action=caEl('ca-action').value,truth=caEl('ca-truth').value,reason=caEl('ca-reason').value;if(action==='submit'&&truth!=='true_positive'){showToast('直接提单需要确认是真实 Bug','error');return;}e.submitter.disabled=true;const messages=[];for(const f of findings){try{const d=await caWrite('POST',`/code-analysis/runs/${job.id}/findings/${f.id}/decision`,{expected_revision:f.revision,action,truth_label:truth,reason});messages.push(`#${f.id} 标注已保存；学习${d.learning_dispatch_error?'待重派':'已派发'}`);if(action==='submit'){try{const result=await caWrite('POST',`/code-analysis/decisions/${d.id}/submit-bug`,{confirmed:true});messages.push(`TAPD #${result.bug_id}`);}catch(err){messages.push(`提单未闭环：${err.message}`);}}}catch(err){messages.push(`#${f.id} ${err.message}`);}caEl('ca-triage-receipts').textContent=messages.join('；');}e.submitter.remove();const back=document.createElement('button');back.type='button';back.className='btn btn-ghost';back.textContent='返回问题列表';back.onclick=()=>caJob(job.id);caEl('ca-triage-form').append(back);};
}
function caHistory(fid) {
 const job=CA.job,f=job.findings.find(f=>f.id===fid);
 caDialog(`问题 #${fid} · 反馈与学习记录`,f.decisions.map(d=>`<section class="ca-panel"><div class="ca-tag">反馈 #${d.id} · ${caEsc(d.created_at)}</div><p>${caEsc(d.action)} / ${caEsc(d.truth_label)} · ${caEsc(d.actor_name)}</p><pre>${caEsc(d.reason)}</pre><p>学习：${caEsc(caStatus(d.learning_status))} ${d.learned_revision?'· 知识 r'+d.learned_revision:''}</p><pre>${caEsc(d.learning_summary)}</pre>${d.general_proposal?`<details><summary>通用经验建议（未自动发布）</summary><pre>${caEsc(d.general_proposal)}</pre>${d.general_published_revision?`<p>已发布通用知识 r${d.general_published_revision}</p>`:`<button class="btn btn-ghost" data-general="${d.id}">审核并发布通用经验</button>`}</details>`:''}<div class="ca-actions">${d.learning_run_id?`<a href="/workflows?run_id=${d.learning_run_id}" class="btn btn-ghost">学习 Run #${d.learning_run_id}</a>`:`<button class="btn btn-ghost" data-learn="${d.id}">重新派发学习</button>`}${d.action==='submit'&&d.submission_status!=='submitted'?`<button class="btn btn-ghost" data-submit="${d.id}">提单</button><button class="btn btn-ghost" data-reconcile="${d.id}">关联已有 Bug</button>`:''}</div></section>`).join('')+`<button class="btn btn-ghost" id="ca-back">返回问题列表</button>`);
 caEl('ca-back').onclick=()=>caJob(job.id);
 document.querySelectorAll('[data-general]').forEach(b=>b.onclick=()=>caPublishGeneral(f.decisions.find(d=>d.id===Number(b.dataset.general))));
 document.querySelectorAll('[data-learn]').forEach(b=>b.onclick=async()=>{try{await caWrite('POST',`/code-analysis/decisions/${b.dataset.learn}/learn`,{});await caJob(job.id);}catch(e){caError(e);}});
 document.querySelectorAll('[data-submit]').forEach(b=>b.onclick=async()=>{if(!await caAsk('确认向 TAPD 创建 Bug？'))return;try{await caWrite('POST',`/code-analysis/decisions/${b.dataset.submit}/submit-bug`,{confirmed:true});await caJob(job.id);}catch(e){caError(e);}});
 document.querySelectorAll('[data-reconcile]').forEach(b=>b.onclick=async()=>{const id=await caAsk('输入本项目已存在的 TAPD Bug ID','');if(!id)return;try{await caWrite('POST',`/code-analysis/decisions/${b.dataset.reconcile}/reconcile-bug`,{bug_id:id});await caJob(job.id);}catch(e){caError(e);}});
}
function caResources() {
 const o=CA.options;
 caEl('ca-content').innerHTML=`<div class="ca-panel"><h3>通用 ＋ 项目知识</h3><p class="ca-note">引用现有知识库，不复制文档。源项目保留维护权；通用经验可复用，但不能替代当前项目的代码证据。</p><div class="ca-grid">${o.knowledge.map(k=>`<div class="ca-panel"><div class="ca-tag">${k.project_id===null?'通用':Number(k.project_id)===Number(CA.project)?'本项目':'跨项目共享'} · #${k.id} · r${k.revision}</div><h4>${caEsc(k.title)}</h4><button class="btn btn-ghost" data-knowledge="${k.id}">查看 / 版本</button> <button class="btn btn-ghost" data-share="knowledge:${k.id}">共享范围</button></div>`).join('')}</div></div><div class="ca-panel"><h3>可用 Skill</h3><div class="ca-scroll"><table class="ca-table"><tbody>${o.skills.map(s=>`<tr><td>#${s.id} ${caEsc(s.title)}</td><td>${caEsc(s.scope)}</td><td><button class="btn btn-ghost" data-skill="${s.id}">详情与附件</button> <button class="btn btn-ghost" data-share="skill:${s.id}">共享范围</button></td></tr>`).join('')}</tbody></table></div></div>`;
 document.querySelectorAll('[data-knowledge]').forEach(b=>b.onclick=()=>caKnowledge(Number(b.dataset.knowledge)));
 document.querySelectorAll('[data-skill]').forEach(b=>b.onclick=async()=>{try{const s=await API.get(`/skills/${Number(b.dataset.skill)}`);caDialog(s.display_name,`<pre>${caEsc(s.template_content)}</pre><h4>附件（${Number(s.attachment_count||0)}）</h4>${(s.attachments||[]).map(a=>`<p><a href="/api/v1/skills/${Number(s.id)}/attachments/${Number(a.id)}/download">${caEsc(a.filename)}</a> · ${Number(a.size_bytes)} 字节</p>`).join('')||'<p class="ca-note">暂无附件</p>'}${s.attachment_count?`<a class="btn btn-ghost" href="/api/v1/skills/${Number(s.id)}/pack">下载完整包</a>`:''}`);}catch(e){caError(e);}});
 document.querySelectorAll('[data-share]').forEach(b=>b.onclick=()=>caSharing(b.dataset.share));
}
async function caKnowledge(id) {
 try{const meta=CA.options.knowledge.find(k=>k.id===id);const p=await API.get(meta.sharing?`/knowledge/journal-pages/${id}`:`/knowledge/${id}`);caDialog(p.title,`<p class="ca-note">当前 r${p.current_revision||0}</p><pre>${caEsc(p.content||'（空白经验库）')}</pre>${p.can_edit?'<button class="btn btn-primary" id="ca-edit-knowledge">编辑并保存新版本</button>':''} ${meta.sharing?'<button class="btn btn-ghost" id="ca-versions">版本记录 / 对比 / 回退</button>':''}`);
 if(p.can_edit)caEl('ca-edit-knowledge').onclick=()=>{caDialog(p.title,`<form id="ca-knowledge-form"><label class="ca-field">知识正文<textarea id="ca-knowledge-content" style="min-height:380px">${caEsc(p.content)}</textarea></label><label class="ca-field">修改说明<input id="ca-change-summary" required></label><button class="btn btn-primary" type="submit">保存新版本</button></form>`);caEl('ca-knowledge-form').onsubmit=async e=>{e.preventDefault();e.submitter.disabled=true;try{await caWrite('POST',`/knowledge/${id}/revisions`,{title:p.title,content:caEl('ca-knowledge-content').value,change_summary:caEl('ca-change-summary').value,expected_revision:p.current_revision});await caLoad();await caKnowledge(id);}catch(err){caError(err);e.submitter.disabled=false;}};};
 if(meta.sharing)caEl('ca-versions').onclick=async()=>{try{const v=await API.get(`/knowledge/${id}/revisions`);caDialog(p.title+' · 版本',v.items.map(r=>`<div class="ca-panel"><strong>r${r.revision_no} ${caEsc(r.change_summary)}</strong><p class="ca-note">${caEsc(r.editor_name)} · ${caEsc(r.created_at)}</p><button class="btn btn-ghost" data-compare="${r.revision_no}">与当前版本对比</button> ${p.can_edit&&r.revision_no!==p.current_revision?`<button class="btn btn-ghost" data-rollback="${r.revision_no}">恢复此版本</button>`:''}</div>`).join(''));document.querySelectorAll('[data-compare]').forEach(b=>b.onclick=async()=>{try{const d=await API.get(`/knowledge/${id}/compare?from=${b.dataset.compare}&to=${p.current_revision}`);caDialog(p.title+' · 对比',`<pre>${caEsc(Array.isArray(d.diff)?d.diff.join('\n'):d.diff||d.unified_diff||JSON.stringify(d,null,2))}</pre>`);}catch(e){caError(e);}});document.querySelectorAll('[data-rollback]').forEach(b=>b.onclick=async()=>{if(!await caAsk('恢复会追加一个新版本，不删除历史，继续？'))return;try{await caWrite('POST',`/knowledge/${id}/rollback`,{expected_revision:p.current_revision,target_revision:Number(b.dataset.rollback)});await caLoad();await caKnowledge(id);}catch(e){caError(e);}});}catch(e){caError(e);}};
 }catch(e){caError(e);}
}
async function caSharing(value) {
 const [kind,id]=value.split(':');
 try{const p=await API.get(`/resource-sharing/${kind}/${id}`);const projects=await API.listProjects();const list=Array.isArray(projects)?projects:projects.items||projects.projects||[];
 caDialog('资源共享范围',`<form id="ca-sharing-form"><p class="ca-note">共享只授予读取／使用；不会转移归属或开放原始代码报告。仅来源维护者可保存。</p><label class="ca-field">使用范围<select id="ca-sharing-scope"><option value="project">仅来源项目</option><option value="selected">指定项目</option><option value="all">通用 · 全项目</option></select></label><div class="ca-grid">${list.map(x=>`<label><input type="checkbox" value="${x.id}" data-share-project ${(p.project_ids||[]).includes(x.id)?'checked':''}> ${caEsc(x.name)}</label>`).join('')}</div>${p.can_manage?'<button class="btn btn-primary" type="submit">保存共享范围</button>':'<p class="ca-note">你有读取权限，维护权保留在来源项目。</p>'}</form>`);caEl('ca-sharing-scope').value=p.scope;caEl('ca-sharing-form').onsubmit=async e=>{e.preventDefault();e.submitter.disabled=true;try{await caWrite('PUT',`/resource-sharing/${kind}/${id}`,{scope:caEl('ca-sharing-scope').value,project_ids:[...document.querySelectorAll('[data-share-project]:checked')].map(b=>Number(b.value)),expected_revision:p.revision});caEl('ca-dialog').close();await caLoad();}catch(err){caError(err);e.submitter.disabled=false;}};
 }catch(e){caError(e);}
}
function caActivateTab(){document.querySelectorAll('[data-ca-tab]').forEach(b=>{b.classList.toggle('active',b.dataset.caTab===CA.tab);b.setAttribute('aria-selected',b.dataset.caTab===CA.tab?'true':'false');});}
document.addEventListener('DOMContentLoaded',async()=>{
 caEl('ca-close').onclick=()=>caEl('ca-dialog').close();caEl('ca-full').onclick=()=>{caEl('ca-dialog').classList.toggle('full');caEl('ca-full').textContent=caEl('ca-dialog').classList.contains('full')?'退出全屏':'全屏';};
 caEl('ca-dialog').addEventListener('close',()=>{CA.detailEpoch++;caEl('ca-dialog').classList.remove('full');caEl('ca-full').textContent='全屏';});
 document.querySelectorAll('[data-ca-tab]').forEach(b=>b.onclick=()=>{CA.tab=b.dataset.caTab;caActivateTab();if(CA.options)caRender().catch(caError);});
 try{const data=await API.listProjects(),projects=Array.isArray(data)?data:data.items||data.projects||[];caEl('ca-project').innerHTML='<option value="">请选择项目</option>'+caOptions(projects,'','name');const requested=new URLSearchParams(location.search).get('project_id');if(requested&&projects.some(p=>String(p.id)===requested))caEl('ca-project').value=requested;else {const saved=localStorage.getItem('openclaw_last_project');caEl('ca-project').value=projects.some(p=>String(p.id)===saved)?saved:String(projects[0]?.id||'');}caEl('ca-project').onchange=()=>{CA.project=caEl('ca-project').value;CA.page=1;localStorage.setItem('openclaw_last_project',CA.project);const url=new URL(location.href);url.searchParams.set('project_id',CA.project);url.searchParams.delete('plan_id');history.replaceState(null,'',url);caLoad();};CA.project=caEl('ca-project').value;await caLoad();}catch(e){caError(e);}
});
