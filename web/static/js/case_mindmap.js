/**
 * 用例目录脑图渲染器（用例库页与评审课题页共用）。
 *
 * 不依赖任何第三方脑图库：内网环境不宜再引 CDN，沿用仓库既有的
 * "绝对定位 DOM 节点 + SVG 贝塞尔连线" 思路，补上目录树必需的折叠/展开。
 *
 * 用法：
 *   const mm = CaseMindmap.render(container, data, {
 *       canMark: true,
 *       onMarkChange: (nodeId, mark) => {...},   // mark 为 null 表示清除
 *       // 展开时按需补子节点：用例展开前提/步骤/预期，目录补被截断的用例
 *       onLoadChildren: (node, done) => { ...; done(children, errMsg); },
 *   });
 *   mm.setData(newData);   // 局部刷新，保留折叠状态
 */
(function (global) {
    'use strict';

    var NODE_H = 32;
    var V_GAP = 8;
    var H_GAP = 48;
    var PAD = 24;
    var MIN_W = 120;
    var MAX_W = 320;
    var KEEP_VISIBLE = 120;   // 平移到极限时至少还留这么宽的内容在视野内
    var PAN_SLACK_Y = 360;    // 垂直方向的可平移余量

    var NODE_MODULE = 'module';
    var NODE_CASE = 'case';
    // detail：用例就地展开出的前提/步骤/预期，只作展示，不参与评审标记
    var NODE_DETAIL = 'detail';

    var MARK_ICONS = { question: '❗', risk: '⚠️', flag: '🚩' };
    var MARK_LABELS = {
        question: '有问题/待修改',
        risk: '风险或待确认',
        flag: '重点关注',
    };

    var STYLE_ID = 'case-mindmap-style';
    var CSS = [
        '.cmm-wrap{position:relative;overflow:auto;width:100%;height:100%;cursor:grab}',
        '.cmm-wrap.panning{cursor:grabbing;user-select:none}',
        // 右/下留出近一屏的空白，才能把左上角的根节点拖出视野去看右边的深层目录。
        // 用百分比 padding 让浏览器自己算：JS 读 clientWidth 在容器尚未布局时会拿到 0，
        // 余量就没了。节点是 absolute 定位、参照 padding box，加右下 padding 不影响坐标。
        '.cmm-canvas{position:relative;transform-origin:0 0;',
        'padding-right:calc(100% - ' + KEEP_VISIBLE + 'px);padding-bottom:' + PAN_SLACK_Y + 'px}',
        '.cmm-canvas svg{position:absolute;top:0;left:0;pointer-events:none;overflow:visible}',
        '.cmm-canvas svg path{fill:none;stroke-width:1.8;stroke-linecap:round}',
        '.cmm-node{position:absolute;box-sizing:border-box;display:flex;align-items:center;gap:6px;',
        'padding:5px 10px;border-radius:8px;font-size:0.82rem;line-height:1.25;',
        'background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-primary);',
        'white-space:nowrap;overflow:hidden;transition:box-shadow .15s,border-color .15s}',
        '.cmm-node:hover{box-shadow:0 3px 10px rgba(0,0,0,.18)}',
        '.cmm-node.cmm-root{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}',
        '.cmm-node.cmm-module{background:var(--bg-card);border-width:2px;font-weight:600;cursor:pointer}',
        '.cmm-node.cmm-case{background:var(--bg-secondary);color:var(--text-secondary)}',
        '.cmm-node.cmm-mark-question{border-color:#ef4444;background:rgba(239,68,68,.10)}',
        '.cmm-node.cmm-mark-risk{border-color:#f59e0b;background:rgba(245,158,11,.10)}',
        '.cmm-node.cmm-mark-flag{border-color:#10b981;background:rgba(16,185,129,.10)}',
        '.cmm-arrow{flex-shrink:0;width:12px;text-align:center;color:var(--text-muted);',
        'transition:transform .15s;display:inline-block}',
        '.cmm-arrow.open{transform:rotate(90deg)}',
        // min-width:0 必须保留：flex 子项默认 min-width:auto 不肯收缩到内容宽度以下，
        // 会把后面的计数/标记图标挤出节点并被 overflow:hidden 裁掉，导致标记点不到
        '.cmm-text{overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 auto}',
        '.cmm-prio{flex-shrink:0;padding:1px 6px;border-radius:5px;font-size:0.66rem;',
        'font-weight:700;color:#fff;font-family:"JetBrains Mono",monospace}',
        '.cmm-prio.p0{background:#ef4444}.cmm-prio.p1{background:#f59e0b;color:#1a1a1a}',
        '.cmm-prio.p2{background:#3b82f6}.cmm-prio.p3{background:#6b7280}',
        '.cmm-cnt{flex-shrink:0;font-size:0.7rem;color:var(--text-muted)}',
        '.cmm-mark{flex-shrink:0;cursor:pointer;font-size:0.8rem;opacity:.45;padding:0 2px;border-radius:4px}',
        '.cmm-mark.on{opacity:1}',
        '.cmm-mark:hover{opacity:1;background:rgba(127,127,127,.18)}',
        '.cmm-check{flex:0 0 auto;width:15px;height:15px;margin:0;cursor:pointer;accent-color:var(--accent)}',
        '.cmm-node.cmm-sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(88,166,255,.35)}',
        '.cmm-node.cmm-detail{background:transparent;border-style:dashed;',
        'color:var(--text-muted);font-size:0.78rem}',
        '.cmm-err{flex-shrink:0;font-size:0.7rem;color:#ef4444}',
        '.cmm-menu{position:absolute;z-index:30;background:var(--bg-card);border:1px solid var(--border);',
        'border-radius:8px;padding:4px;box-shadow:0 8px 24px rgba(0,0,0,.28);font-size:0.82rem;min-width:150px}',
        '.cmm-menu-item{padding:6px 10px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:8px}',
        '.cmm-menu-item:hover{background:var(--bg-secondary)}',
        '.cmm-legend{display:flex;gap:14px;flex-wrap:wrap;align-items:center;',
        'padding:8px 12px;font-size:0.78rem;color:var(--text-muted)}',
        '.cmm-empty{padding:40px;text-align:center;color:var(--text-muted);font-size:0.85rem}',
        '.cmm-warn{margin:8px 12px;padding:8px 12px;border-radius:6px;font-size:0.8rem;',
        'background:rgba(245,158,11,.12);color:#f59e0b}',
    ].join('');

    function injectStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var el = document.createElement('style');
        el.id = STYLE_ID;
        el.textContent = CSS;
        document.head.appendChild(el);
    }

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    /** 估算节点宽度：漏算任一图标都会让它被 overflow:hidden 裁掉而无法点击 */
    function estimateWidth(node, options) {
        var text = String(node.text || '');
        var isModule = node.node_type === NODE_MODULE;
        var isDetail = node.node_type === NODE_DETAIL;
        // 中文按 1 个字符约 13px 估算，再给左右内边距留余量
        var extra = 34;
        if (node.priority) extra += 30;
        if ((node.children || []).length || node._loading) extra += 18;   // 折叠箭头
        if (isModule && node.case_count) extra += 14 + String(node.case_count).length * 7;
        if (node._loading || node._loadError) extra += 56;
        if ((options.canMark && !isDetail) || node.mark) extra += 22;     // 标记图标
        if (options.selectable && !isDetail) extra += 22;                 // 复选框
        return Math.max(MIN_W, Math.min(MAX_W, text.length * 13 + extra));
    }

    /** 目录排在用例前，目录之间用 localeCompare，与侧边栏目录树顺序一致 */
    function sortedChildren(node) {
        var kids = (node.children || []).slice();
        var modules = kids.filter(function (n) { return n.node_type === 'module'; });
        var cases = kids.filter(function (n) { return n.node_type !== 'module'; });
        modules.sort(function (a, b) {
            return String(a.text || '').localeCompare(String(b.text || ''), 'zh-Hans-CN');
        });
        return modules.concat(cases);
    }

    function CaseMindmapView(container, data, options) {
        this.container = container;
        this.options = options || {};
        this.collapsed = Object.create(null);
        this.menu = null;
        this.selectedId = null;
        this._panMoved = false;
        injectStyle();
        this._buildShell();
        this.setData(data);
    }

    CaseMindmapView.prototype._buildShell = function () {
        this.container.innerHTML = '';
        this.legendEl = document.createElement('div');
        this.legendEl.className = 'cmm-legend';
        this.warnEl = document.createElement('div');
        this.warnEl.className = 'cmm-warn';
        this.warnEl.style.display = 'none';
        this.wrapEl = document.createElement('div');
        this.wrapEl.className = 'cmm-wrap';
        this.canvasEl = document.createElement('div');
        this.canvasEl.className = 'cmm-canvas';
        this.wrapEl.appendChild(this.canvasEl);
        this.container.appendChild(this.legendEl);
        this.container.appendChild(this.warnEl);
        this.container.appendChild(this.wrapEl);

        // 点空白处关标记菜单。视图随页签关闭而脱离 DOM，此时不再做任何事
        var self = this;
        document.addEventListener('click', function () {
            if (!self.container.isConnected) return;
            self._closeMenu();
        });
        this._bindPan();
    };

    CaseMindmapView.prototype.setData = function (data) {
        this.data = data || null;
        this.render();
    };

    /** 改渲染选项（如切换 canMark）而不重建实例，折叠状态得以保留 */
    CaseMindmapView.prototype.setOptions = function (patch) {
        Object.keys(patch || {}).forEach(function (k) {
            this.options[k] = patch[k];
        }, this);
        this.render();
    };

    /** 按 node_id 覆盖整棵树的标记，用于切换评审课题后同步标记层 */
    CaseMindmapView.prototype.applyMarks = function (markByNodeId) {
        (function walk(node) {
            if (!node) return;
            var mark = markByNodeId[node.id] || null;
            node.mark = mark;
            node.icons = (node.priority ? [node.priority] : []).concat(mark ? [mark] : []);
            (node.children || []).forEach(walk);
        })(this.data);
        this.render();
    };

    /** 默认折叠深度超过 2 层的目录，避免一上来就铺满屏；用例详情默认不展开 */
    CaseMindmapView.prototype._initCollapse = function (node, depth) {
        if (!(node.id in this.collapsed)) {
            if (node.node_type === NODE_MODULE) {
                this.collapsed[node.id] = depth >= 2 && (node.children || []).length > 0;
            } else if (node.node_type === NODE_CASE) {
                this.collapsed[node.id] = true;
            } else {
                this.collapsed[node.id] = false;
            }
        }
        (node.children || []).forEach(function (c) {
            this._initCollapse(c, depth + 1);
        }, this);
    };

    CaseMindmapView.prototype.render = function () {
        var data = this.data;
        if (!data) {
            this.canvasEl.innerHTML = '<div class="cmm-empty">暂无脑图数据</div>';
            return;
        }
        this._initCollapse(data, 0);
        this._renderLegend();

        var levelW = [];
        (function measure(node, depth) {
            node._w = estimateWidth(node, this.options);
            levelW[depth] = Math.max(levelW[depth] || 0, node._w);
            if (!this.collapsed[node.id]) {
                sortedChildren(node).forEach(function (c) { measure.call(this, c, depth + 1); }, this);
            }
        }).call(this, data, 0);

        var levelX = [PAD];
        for (var i = 1; i < levelW.length; i++) {
            levelX[i] = levelX[i - 1] + levelW[i - 1] + H_GAP;
        }

        var placed = [];
        var edges = [];
        var self = this;
        var maxBottom = 0;

        function layout(node, depth, top) {
            var kids = self.collapsed[node.id] ? [] : sortedChildren(node);
            var height;
            if (!kids.length) {
                height = NODE_H;
            } else {
                var cursor = top;
                height = 0;
                kids.forEach(function (kid) {
                    var h = layout(kid, depth + 1, cursor);
                    cursor += h + V_GAP;
                    height += h + V_GAP;
                });
                height = Math.max(NODE_H, height - V_GAP);
            }
            node._x = levelX[depth];
            node._y = top + height / 2 - NODE_H / 2;
            node._depth = depth;
            placed.push(node);
            maxBottom = Math.max(maxBottom, node._y + NODE_H);
            kids.forEach(function (kid) { edges.push([node, kid]); });
            return height;
        }
        layout(data, 0, PAD);

        var width = levelX[levelX.length - 1] + levelW[levelW.length - 1] + PAD;
        var height = maxBottom + PAD;
        // 可平移余量由 .cmm-canvas 的 padding 负责，这里只写内容自身尺寸
        this.canvasEl.style.width = width + 'px';
        this.canvasEl.style.height = height + 'px';

        var svg = ['<svg width="', width, '" height="', height, '">'];
        edges.forEach(function (pair) {
            var a = pair[0], b = pair[1];
            var x1 = a._x + a._w, y1 = a._y + NODE_H / 2;
            var x2 = b._x, y2 = b._y + NODE_H / 2;
            var mid = x1 + (x2 - x1) / 2;
            svg.push('<path d="M', x1, ',', y1, ' C', mid, ',', y1, ' ', mid, ',', y2,
                ' ', x2, ',', y2, '" stroke="', b.mark ? markColor(b.mark) : 'var(--border)', '"/>');
        });
        svg.push('</svg>');

        var html = [svg.join('')];
        placed.forEach(function (node) {
            html.push(self._nodeHtml(node));
        });
        this.canvasEl.innerHTML = html.join('');
        this._bindNodes();
    };

    function markColor(mark) {
        return { question: '#ef4444', risk: '#f59e0b', flag: '#10b981' }[mark] || 'var(--border)';
    }

    CaseMindmapView.prototype._renderLegend = function () {
        var parts = ['<span>图标：</span>'];
        ['P0', 'P1', 'P2'].forEach(function (p) {
            parts.push('<span class="cmm-prio ' + p.toLowerCase() + '">' + p + '</span>');
        });
        Object.keys(MARK_ICONS).forEach(function (key) {
            parts.push('<span>' + MARK_ICONS[key] + ' ' + esc(MARK_LABELS[key]) + '</span>');
        });
        parts.push('<span>｜</span>');
        parts.push('<span>点节点折叠/展开，按住拖动可平移</span>');
        if (this.options.onLoadChildren) {
            parts.push('<span>点用例可就地展开前提/步骤/预期</span>');
        }
        if (this.options.canMark) {
            parts.push('<span style="color:var(--accent)">点节点上的 ○ 打评审标记</span>');
        } else if (this.options.markHint) {
            parts.push('<span>' + esc(this.options.markHint) + '</span>');
        }
        if (this.options.selectable) {
            parts.push('<span style="color:var(--accent)">勾选目录可选中整棵子树，勾选用例可精确选择</span>');
        }
        this.legendEl.innerHTML = parts.join('');

        if (this.data && this.data.truncated) {
            // 截断只砍用例叶子：目录结构与每个目录的用例数都是完整准确的
            this.warnEl.style.display = '';
            this.warnEl.textContent = '目录结构与各目录用例数均完整，但用例过多，'
                + '本次只展开了 ' + (this.data.shown_case_count || 0)
                + ' / ' + (this.data.total_case_count || 0)
                + ' 条用例。从更深一级目录打开脑图可看到全部用例。';
        } else {
            this.warnEl.style.display = 'none';
        }
    };

    CaseMindmapView.prototype._nodeHtml = function (node) {
        var isRoot = node._depth === 0;
        var isModule = node.node_type === NODE_MODULE;
        var isDetail = node.node_type === NODE_DETAIL;
        var cls = ['cmm-node',
                   isModule ? 'cmm-module' : (isDetail ? 'cmm-detail' : 'cmm-case')];
        if (isRoot) cls.push('cmm-root');
        if (node.mark) cls.push('cmm-mark-' + node.mark);

        var inner = [];
        if (this._expandable(node)) {
            inner.push('<span class="cmm-arrow' + (this.collapsed[node.id] ? '' : ' open')
                + '" data-toggle="1">›</span>');
        }
        if (node.priority) {
            inner.push('<span class="cmm-prio ' + node.priority.toLowerCase() + '">'
                + esc(node.priority) + '</span>');
        }
        if (this.options.selectable && !isDetail) {
            var selected = this.options.isSelected
                ? !!this.options.isSelected(node)
                : !!((this.options.selectedIds || {})[node.id]);
            inner.push('<input type="checkbox" class="cmm-check" data-select="1" '
                + (selected ? 'checked ' : '') + 'aria-label="选择 ' + esc(node.text) + '">');
        }
        inner.push('<span class="cmm-text" title="' + esc(node.text) + '">'
            + esc(node.text) + '</span>');
        if (isModule && node.case_count) {
            inner.push('<span class="cmm-cnt">' + node.case_count + '</span>');
        }
        if (node._loading) {
            inner.push('<span class="cmm-cnt">加载中…</span>');
        } else if (node._loadError) {
            inner.push('<span class="cmm-err" title="' + esc(node._loadError) + '">加载失败</span>');
        }
        // detail 节点不在评审范围里，后端不接受它的标记
        if (this.options.canMark && !isDetail) {
            inner.push('<span class="cmm-mark' + (node.mark ? ' on' : '') + '" data-mark="1" '
                + 'title="设置评审标记">' + (node.mark ? MARK_ICONS[node.mark] : '○') + '</span>');
        } else if (node.mark) {
            inner.push('<span class="cmm-mark on">' + MARK_ICONS[node.mark] + '</span>');
        }
        if (this.selectedId === node.id) cls.push('cmm-sel');

        return '<div class="' + cls.join(' ') + '" data-id="' + esc(node.id) + '" '
            + 'style="left:' + node._x + 'px;top:' + node._y + 'px;'
            + 'width:' + node._w + 'px;height:' + NODE_H + 'px">'
            + inner.join('') + '</div>';
    };

    /** 递归统计已挂在树上的用例叶子数，用于判断目录是否被截断过 */
    function countCaseLeaves(node) {
        if (!node) return 0;
        if (node.node_type === NODE_CASE) return 1;
        var n = 0;
        (node.children || []).forEach(function (c) { n += countCaseLeaves(c); });
        return n;
    }

    /** 该节点是否还有内容没加载：用例没展开过详情，或目录的用例数多于实际挂上的叶子 */
    CaseMindmapView.prototype._needsLoad = function (node) {
        if (!this.options.onLoadChildren) return false;
        if (node._loaded || node._loading) return false;
        if (node.node_type === NODE_CASE) return !(node.children || []).length;
        if (node.node_type === NODE_MODULE) {
            return countCaseLeaves(node) < (node.case_count || 0);
        }
        return false;
    };

    /** 节点是否可展开（有子节点或还能加载出子节点） */
    CaseMindmapView.prototype._expandable = function (node) {
        return (node.children || []).length > 0 || this._needsLoad(node);
    };

    CaseMindmapView.prototype._toggle = function (node) {
        if (this._needsLoad(node)) {
            this._loadChildren(node);
            return;
        }
        if (!(node.children || []).length) return;
        this.collapsed[node.id] = !this.collapsed[node.id];
        this.render();
    };

    CaseMindmapView.prototype._loadChildren = function (node) {
        var self = this;
        node._loading = true;
        this.collapsed[node.id] = false;
        this.render();
        this.options.onLoadChildren(node, function (children, errMsg) {
            node._loading = false;
            node._loaded = true;
            if (errMsg) {
                node._loadError = errMsg;
                self.render();
                return;
            }
            node._loadError = null;
            if (children && children.length) {
                node.children = children;
                self.collapsed[node.id] = false;
            }
            self.render();
        });
    };

    CaseMindmapView.prototype._findNode = function (id, node) {
        node = node || this.data;
        if (!node) return null;
        if (node.id === id) return node;
        var kids = node.children || [];
        for (var i = 0; i < kids.length; i++) {
            var hit = this._findNode(id, kids[i]);
            if (hit) return hit;
        }
        return null;
    };

    CaseMindmapView.prototype._bindNodes = function () {
        var self = this;
        var nodes = this.canvasEl.querySelectorAll('.cmm-node');
        Array.prototype.forEach.call(nodes, function (el) {
            el.addEventListener('click', function (ev) {
                if (self._panMoved) return;      // 刚刚是拖拽平移，不当作点击
                var id = el.getAttribute('data-id');
                var node = self._findNode(id);
                if (!node) return;

                if (ev.target.getAttribute && ev.target.getAttribute('data-select')) {
                    ev.stopPropagation();
                    var checked = !!ev.target.checked;
                    self.options.selectedIds = self.options.selectedIds || {};
                    if (checked) self.options.selectedIds[node.id] = true;
                    else delete self.options.selectedIds[node.id];
                    if (self.options.onSelectionChange) {
                        self.options.onSelectionChange(node, checked, self);
                    }
                    return;
                }

                if (ev.target.getAttribute && ev.target.getAttribute('data-mark')) {
                    ev.stopPropagation();
                    self._openMarkMenu(el, node);
                    return;
                }
                // 目录和用例都在原地展开（用例展开出前提/步骤/预期），不再新开页签
                if (self._expandable(node)) {
                    self._toggle(node);
                    return;
                }
                self.selectedId = (self.selectedId === node.id) ? null : node.id;
                self.render();
            });
        });
    };

    /** 按住空白处（或节点）拖动即平移画布，靠 scrollLeft/Top 实现以便与滚动条共存 */
    CaseMindmapView.prototype._bindPan = function () {
        var self = this;
        var active = false;
        var startX = 0, startY = 0, startL = 0, startT = 0;

        this.wrapEl.addEventListener('mousedown', function (ev) {
            if (ev.button !== 0) return;
            if (ev.target.closest && ev.target.closest('.cmm-menu,.cmm-check')) return;
            active = true;
            self._panMoved = false;
            startX = ev.clientX;
            startY = ev.clientY;
            startL = self.wrapEl.scrollLeft;
            startT = self.wrapEl.scrollTop;
            ev.preventDefault();            // 拖动时不要选中节点文字
        });

        // 监听挂在 document 上，指针移出容器后仍能继续平移
        document.addEventListener('mousemove', function (ev) {
            if (!active) return;
            if (!self.container.isConnected) { active = false; return; }
            var dx = ev.clientX - startX;
            var dy = ev.clientY - startY;
            if (!self._panMoved && (Math.abs(dx) > 3 || Math.abs(dy) > 3)) {
                self._panMoved = true;
                self.wrapEl.classList.add('panning');
            }
            if (!self._panMoved) return;
            self.wrapEl.scrollLeft = startL - dx;
            self.wrapEl.scrollTop = startT - dy;
        });

        document.addEventListener('mouseup', function () {
            if (!active) return;
            active = false;
            self.wrapEl.classList.remove('panning');
        });
    };

    CaseMindmapView.prototype._closeMenu = function () {
        if (this.menu && this.menu.parentNode) {
            this.menu.parentNode.removeChild(this.menu);
        }
        this.menu = null;
    };

    CaseMindmapView.prototype._openMarkMenu = function (anchorEl, node) {
        this._closeMenu();
        var self = this;
        var menu = document.createElement('div');
        menu.className = 'cmm-menu';
        menu.style.left = (node._x + 12) + 'px';
        menu.style.top = (node._y + NODE_H + 4) + 'px';

        var items = Object.keys(MARK_ICONS).map(function (key) {
            return { mark: key, label: MARK_ICONS[key] + ' ' + MARK_LABELS[key] };
        });
        items.push({ mark: null, label: '○ 清除标记' });

        items.forEach(function (item) {
            var row = document.createElement('div');
            row.className = 'cmm-menu-item';
            row.textContent = item.label;
            row.addEventListener('click', function (ev) {
                ev.stopPropagation();
                self._closeMenu();
                if ((node.mark || null) === item.mark) return;
                node.mark = item.mark;
                node.icons = (node.priority ? [node.priority] : [])
                    .concat(item.mark ? [item.mark] : []);
                self.render();
                if (self.options.onMarkChange) {
                    self.options.onMarkChange(node.id, item.mark, node);
                }
            });
            menu.appendChild(row);
        });

        menu.addEventListener('click', function (ev) { ev.stopPropagation(); });
        this.canvasEl.appendChild(menu);
        this.menu = menu;
    };

    /** 用例详情 → 脑图子节点（前提/步骤/预期各一节）。两个页面共用，保证结构一致 */
    function caseDetailChildren(nodeId, caseData) {
        var content = (caseData || {}).content || {};
        var sections = [
            ['pre', '📋 前提条件', content.preconditions
                ? [content.preconditions] : []],
            ['step', '🔧 操作步骤', Array.isArray(content.steps) ? content.steps : []],
            ['exp', '✅ 预期结果', Array.isArray(content.expected_results)
                ? content.expected_results : []],
        ];
        var out = [];
        sections.forEach(function (sec) {
            var key = sec[0], label = sec[1], items = sec[2];
            if (!items.length) return;
            out.push({
                id: nodeId + ':' + key,
                node_type: NODE_DETAIL,
                text: label,
                case_count: 0,
                priority: null,
                mark: null,
                icons: [],
                children: items.map(function (item, i) {
                    var txt = (typeof item === 'string') ? item : JSON.stringify(item);
                    return {
                        id: nodeId + ':' + key + ':' + i,
                        node_type: NODE_DETAIL,
                        text: (items.length > 1 ? (i + 1) + '. ' : '') + txt,
                        case_count: 0,
                        priority: null,
                        mark: null,
                        icons: [],
                        children: [],
                    };
                }),
            });
        });
        return out;
    }

    global.CaseMindmap = {
        render: function (container, data, options) {
            return new CaseMindmapView(container, data, options);
        },
        caseDetailChildren: caseDetailChildren,
        MARK_ICONS: MARK_ICONS,
        MARK_LABELS: MARK_LABELS,
        NODE_MODULE: NODE_MODULE,
        NODE_CASE: NODE_CASE,
        NODE_DETAIL: NODE_DETAIL,
    };
})(window);
