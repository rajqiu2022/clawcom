"""目录树脑图构建。

与既有 `build_mindmap_from_cases`（按 优先级 → 用例类型 分组）不是一回事：
本模块按 `TestCase.module_path` 还原**真实目录层级**，供评审脑图视图使用。
既有 `/mindmap` 接口与 XMind 导出保持不变。

设计约束：顶层只依赖标准库，入参是鸭子类型的用例对象（需要
``id`` / ``title`` / ``priority`` / ``module_path`` / ``is_placeholder``），
因此可以脱离 Flask 与数据库单测。
"""

# 手动标记：语义固定为"有问题 / 风险 / 重点"，只存在于评审镜像层，不回写用例库
MARK_QUESTION = 'question'
MARK_RISK = 'risk'
MARK_FLAG = 'flag'

MARKS = {
    MARK_QUESTION: {'icon': '❗', 'label': '有问题/待修改'},
    MARK_RISK: {'icon': '⚠️', 'label': '风险或待确认'},
    MARK_FLAG: {'icon': '🚩', 'label': '重点关注'},
}

VALID_MARKS = tuple(MARKS.keys())

PRIORITIES = ('P0', 'P1', 'P2', 'P3')

NODE_MODULE = 'module'
NODE_CASE = 'case'

# 单次返回的**用例叶子**上限。超过后停止挂叶子并置 truncated。
# 只限叶子、不限目录：目录数天然远小于用例数，而且目录树必须完整——
# 否则用户会误以为库里没那个目录（真实事故：2311 条用例的库丢了一个顶层目录）。
DEFAULT_MAX_LEAVES = 2000

# 目录节点的极端防御上限。正常库远达不到，纯粹防脏数据（例如每条用例一个独立目录）。
DEFAULT_MAX_MODULES = 3000

# 兼容旧名字：早期版本这个常量同时限制目录与叶子
DEFAULT_MAX_NODES = DEFAULT_MAX_LEAVES


def normalize_mark(value):
    """把入参归一化为合法标记；空值/非法值返回 None（等价于清除标记）。"""
    value = str(value or '').strip()
    return value if value in VALID_MARKS else None


def module_node_id(module_path):
    return 'mod:' + (module_path or '')


def case_node_id(case_id):
    return 'case:%s' % case_id


def _is_placeholder(case):
    return bool(getattr(case, 'is_placeholder', False))


def _case_path(case):
    return (getattr(case, 'module_path', None) or '').strip().strip('/')


def _in_scope(case_path, root_path):
    """用例是否落在 root_path 子树内。root_path 为空串表示整库。"""
    if not root_path:
        return True
    return case_path == root_path or case_path.startswith(root_path + '/')


def _relative_parts(case_path, root_path):
    """返回相对 root_path 的目录分段列表。"""
    if not root_path:
        remainder = case_path
    else:
        remainder = case_path[len(root_path):].lstrip('/')
    return [p for p in remainder.split('/') if p]


def _new_module_node(path, text):
    return {
        'id': module_node_id(path),
        'node_type': NODE_MODULE,
        'module_path': path,
        'text': text,
        'case_count': 0,
        'priority': None,
        'mark': None,
        'icons': [],
        'children': [],
    }


def build_directory_mindmap(root_text, cases, root_module_path='', marks=None,
                            max_nodes=DEFAULT_MAX_LEAVES,
                            max_modules=DEFAULT_MAX_MODULES):
    """按目录层级构建脑图。

    ``root_text``          根节点显示名（用例库名或目录名）
    ``cases``              用例对象序列（占位用例只用于撑出空目录，不作为叶子）
    ``root_module_path``   起始目录，空串表示整库
    ``marks``              {节点 id: 标记} 映射，来自评审镜像层
    ``max_nodes``          **用例叶子**上限，超过后停止挂叶子并置 ``truncated``
    ``max_modules``        目录节点上限，仅作极端脏数据防御

    返回根节点 dict，附带 ``truncated`` / ``total_case_count`` 元信息。

    **截断语义**（2026-07-30 修正）：截断只影响用例叶子，
    目录树与每个目录的 ``case_count`` 始终完整准确。早期版本让目录和叶子共用
    一个预算，结果深度优先把预算耗在第一个顶层目录的子树里，导致后面的顶层目录
    压根不出现、父目录计数只有真实值的 1/4——数字和结构都是错的。
    """
    marks = marks or {}
    root_module_path = (root_module_path or '').strip().strip('/')

    root = _new_module_node(root_module_path, root_text)
    index = {root_module_path: root}
    module_count = 1
    leaf_count = 0
    truncated = False

    def ensure_module(path_parts):
        """自顶向下补齐目录节点，返回最深一级节点；超限时返回 None。

        目录**不占用例叶子预算**：目录树不完整会直接误导用户，
        代价远大于多返回几十个目录节点。
        """
        nonlocal module_count, truncated
        current = root
        current_path = root_module_path
        for part in path_parts:
            current_path = (current_path + '/' + part) if current_path else part
            existing = index.get(current_path)
            if existing is None:
                if module_count >= max_modules:
                    truncated = True
                    return None
                existing = _new_module_node(current_path, part)
                index[current_path] = existing
                current['children'].append(existing)
                module_count += 1
            current = existing
        return current

    # 先按目录名、再按 (优先级, 标题) 稳定排序，保证脑图每次渲染顺序一致
    in_scope = [c for c in cases if _in_scope(_case_path(c), root_module_path)]

    # 第一趟：只用占位用例撑出空目录结构
    for case in in_scope:
        if _is_placeholder(case):
            ensure_module(_relative_parts(_case_path(case), root_module_path))

    real_cases = [c for c in in_scope if not _is_placeholder(c)]
    real_cases.sort(key=lambda c: (
        _case_path(c),
        PRIORITIES.index(c.priority) if getattr(c, 'priority', None) in PRIORITIES else len(PRIORITIES),
        str(getattr(c, 'title', '') or ''),
    ))

    total_case_count = len(real_cases)

    # 第二趟：挂真实用例叶子，并沿路累加计数
    for case in real_cases:
        case_path = _case_path(case)
        parts = _relative_parts(case_path, root_module_path)
        parent = ensure_module(parts)

        # 计数**无条件**累加，即使叶子因预算未展示：目录上的数字必须是真实总数，
        # 否则用户看到的 case_count 会随 max_nodes 变化，等于是个假数。
        current_path = root_module_path
        index[current_path]['case_count'] += 1
        for part in parts:
            current_path = (current_path + '/' + part) if current_path else part
            node = index.get(current_path)
            if node is not None:
                node['case_count'] += 1

        if parent is None:
            continue

        if leaf_count >= max_nodes:
            truncated = True
            continue

        node_id = case_node_id(getattr(case, 'id', None))
        priority = getattr(case, 'priority', None)
        priority = priority if priority in PRIORITIES else None
        mark = normalize_mark(marks.get(node_id))
        icons = []
        if priority:
            icons.append(priority)
        if mark:
            icons.append(mark)

        parent['children'].append({
            'id': node_id,
            'node_type': NODE_CASE,
            'case_id': getattr(case, 'id', None),
            'case_key': getattr(case, 'case_id', None),
            'module_path': case_path,
            'text': getattr(case, 'title', '') or '',
            'case_count': 0,
            'priority': priority,
            'mark': mark,
            'icons': icons,
            'children': [],
        })
        leaf_count += 1

    _apply_module_marks(root, marks)
    _sort_module_children(root)

    root['truncated'] = truncated
    root['node_count'] = module_count + leaf_count
    root['module_count'] = module_count
    root['shown_case_count'] = leaf_count
    root['total_case_count'] = total_case_count
    root['mark_legend'] = {k: dict(v) for k, v in MARKS.items()}
    return root


def _apply_module_marks(node, marks):
    if node['node_type'] == NODE_MODULE:
        mark = normalize_mark(marks.get(node['id']))
        node['mark'] = mark
        node['icons'] = [mark] if mark else []
    for child in node['children']:
        _apply_module_marks(child, marks)


def _sort_module_children(node):
    """目录排在用例叶子之前。

    目录之间按 Unicode 码点排序，只保证**稳定确定**；中文拼音序交给前端
    `localeCompare` 处理，与侧边栏目录树的排序保持一致，后端不引入排序依赖。
    """
    modules = [c for c in node['children'] if c['node_type'] == NODE_MODULE]
    leaves = [c for c in node['children'] if c['node_type'] == NODE_CASE]
    modules.sort(key=lambda n: n['text'])
    node['children'] = modules + leaves
    for child in modules:
        _sort_module_children(child)


def collect_node_keys(node, acc=None):
    """收集脑图内所有节点 id，用于校验标记目标是否在范围内。"""
    if acc is None:
        acc = set()
    acc.add(node['id'])
    for child in node.get('children') or []:
        collect_node_keys(child, acc)
    return acc
