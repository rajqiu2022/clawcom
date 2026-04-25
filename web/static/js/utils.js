/**
 * 全局前端工具函数
 *
 * 注意：本文件通过 base.html <head> 早期引入，确保任何子模板的 inline script
 * 都能拿到这里定义的 window.* 函数。
 */

// ── 本地时区今日日期（YYYY-MM-DD）──
// 不能用 toISOString，那是 UTC，UTC+8 凌晨 00:00–08:00 期间会返回前一天，
// 导致今日待办过滤、日报筛选默认值等场景全部差一天。
window._localDateToday = function() {
    var d = new Date();
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, '0');
    var day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
};
