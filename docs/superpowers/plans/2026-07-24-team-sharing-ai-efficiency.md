# 小组分享 AI 提效页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可离线演示、左侧导航、图文并茂的小组分享单页。

**Architecture:** 使用单个 HTML 文件承载语义化内容、CSS 视觉系统、内嵌 SVG 插画和少量原生 JavaScript。JavaScript 仅负责导航高亮、阅读进度、进入动画和返回顶部，不引入第三方依赖。

**Tech Stack:** HTML5、CSS3、原生 JavaScript、内嵌 SVG

## Global Constraints

- 输出必须是可直接双击打开的单文件 HTML。
- 考勤制度只使用用户明确给出的四项原则。
- AI 提效必须覆盖需求、代码、Bug、用例和 Workflow。
- 页面必须支持桌面与窄屏布局，并尊重减少动画偏好。

---

### Task 1: 构建分享页面

**Files:**
- Create: `小组分享_AI提效与个人发展.html`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-07-24-team-sharing-ai-efficiency-design.md`
- Produces: 可离线打开的完整分享页面

- [ ] **Step 1: 建立语义结构**

创建 `aside` 导航和 `main` 内容区，加入 `intro`、`attendance`、`ai`、`growth`、`action` 五个带 ID 的章节。

- [ ] **Step 2: 完成视觉系统**

使用 CSS 变量定义纸张色、墨色、橙色和绿色；实现固定导航、卡片、引言、徽章、流程图、响应式断点和动画降级。

- [ ] **Step 3: 加入内嵌图形**

使用 SVG 绘制打卡时钟、晚归场景、Hub 中心辐射图和个人能力成长图，所有图形包含可读文本或 `aria-label`。

- [ ] **Step 4: 加入交互**

使用 `IntersectionObserver` 高亮当前导航和触发进入动画；监听滚动更新顶部阅读进度并控制返回顶部按钮。

- [ ] **Step 5: 静态校验**

运行：

```powershell
python -c "from pathlib import Path; from html.parser import HTMLParser; p=Path('小组分享_AI提效与个人发展.html'); HTMLParser().feed(p.read_text(encoding='utf-8')); print(p.stat().st_size)"
```

预期：退出码为 0，输出文件大小大于 20 KB。

- [ ] **Step 6: 内容验收**

运行脚本检查五个章节 ID、四项考勤原则、五个 AI 场景和无外部 HTTP 资源。预期所有检查均为 `True`。

- [ ] **Step 7: 浏览器验收**

在桌面宽度和手机宽度打开页面，确认导航跳转、当前章节高亮、阅读进度、返回顶部以及无横向溢出。
