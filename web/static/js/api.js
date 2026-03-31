/**
 * OpenClaw 管理系统 - API 工具库
 */
const API = {
    base: '/api/v1',

    async request(method, path, data = null) {
        const opts = {
            method,
            headers: { 'Content-Type': 'application/json' },
        };
        if (data) opts.body = JSON.stringify(data);

        const resp = await fetch(`${this.base}${path}`, opts);
        const json = await resp.json();
        if (!resp.ok) throw new Error(json.error || `HTTP ${resp.status}`);
        return json;
    },

    get(path) { return this.request('GET', path); },
    post(path, data) { return this.request('POST', path, data); },
    put(path, data) { return this.request('PUT', path, data); },
    del(path) { return this.request('DELETE', path); },

    // Dashboard
    dashboardStats() { return this.get('/dashboard/stats'); },

    // OpenClaw
    listClaws() { return this.get('/openclaws'); },
    getClaw(id) { return this.get(`/openclaws/${id}`); },
    createClaw(data) { return this.post('/openclaws', data); },
    updateClaw(id, data) { return this.put(`/openclaws/${id}`, data); },
    deleteClaw(id) { return this.del(`/openclaws/${id}`); },
    getClawConfig(id) { return this.get(`/openclaws/${id}/config`); },
    regenerateToken(id) { return this.post(`/openclaws/${id}/regenerate-token`); },
    getClawToken(id) { return this.get(`/openclaws/${id}/token`); },
    getClawReports(id, params = '') {
        return this.get(`/openclaws/${id}/reports${params ? '?' + params : ''}`);
    },

    // Skills
    listSkills() { return this.get('/skills'); },
    createSkill(data) { return this.post('/skills', data); },
    installSkill(clawId, skillId) {
        return this.post(`/openclaws/${clawId}/skills`, { skill_id: skillId });
    },
    uninstallSkill(clawId, skillId) {
        return this.del(`/openclaws/${clawId}/skills/${skillId}`);
    },

    // Knowledge
    listKnowledge(params = '') {
        return this.get(`/knowledge${params ? '?' + params : ''}`);
    },
    pendingReviews() { return this.get('/knowledge/pending'); },
    reviewKnowledge(id, data) {
        return this.post(`/knowledge/${id}/review`, data);
    },
    createKnowledge(data) { return this.post('/knowledge', data); },
    updateKnowledge(id, data) { return this.put(`/knowledge/${id}`, data); },
    deleteKnowledge(id) { return this.del(`/knowledge/${id}`); },

    // Projects
    listProjects() { return this.get('/projects'); },
    createProject(data) { return this.post('/projects', data); },
    updateProject(id, data) { return this.put(`/projects/${id}`, data); },
    deleteProject(id) { return this.del(`/projects/${id}`); },
    createModule(projectId, data) {
        return this.post(`/projects/${projectId}/modules`, data);
    },

    // System config
    getSystemConfig() { return this.get('/system/config'); },
    updateSystemConfig(data) { return this.put('/system/config', data); },
    generateSpec(data) { return this.post('/system/llm/generate-spec', data); },

    // Reports
    listReports(params = '') {
        return this.get(`/reports${params ? '?' + params : ''}`);
    },
    reportStats() { return this.get('/reports/stats'); },
    reportTimeline(date) { return this.get(`/reports/timeline?date=${date}`); },

    // TAPD
    getTapdConfig() { return this.get('/tapd/config'); },
    updateTapdConfig(data) { return this.put('/tapd/config', data); },
    tapdStories(params = '') { return this.get(`/tapd/stories?${params}`); },
    tapdBugs(params = '') { return this.get(`/tapd/bugs?${params}`); },
    tapdIterations(params = '') { return this.get(`/tapd/iterations?${params}`); },
    tapdDashboard() { return this.get('/tapd/dashboard'); },
};

/**
 * 工具函数
 */
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function timeAgo(dateStr) {
    if (!dateStr) return '未知';
    // 后端存的是 UTC 时间但没带 Z 后缀，需要补上让浏览器正确解析
    let str = dateStr;
    if (!str.endsWith('Z') && !str.includes('+') && !str.includes('T00:00:00')) {
        str = str.replace(' ', 'T');
        if (!str.endsWith('Z')) str += 'Z';
    }
    const diff = Date.now() - new Date(str).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return '刚刚';
    if (mins < 60) return `${mins}分钟前`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}小时前`;
    const days = Math.floor(hours / 24);
    return `${days}天前`;
}

function statusDot(status) {
    const colors = { online: '#22c55e', offline: '#94a3b8', busy: '#f59e0b' };
    const labels = { online: '在线', offline: '离线', busy: '忙碌' };
    return `<span class="status-dot" style="background:${colors[status] || '#94a3b8'}"
            title="${labels[status] || status}"></span>`;
}

function showToast(msg, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}
