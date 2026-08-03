"""Helpers for iteration-level dynamic tabs."""

VALID_CELL_TYPES = {'text', 'link', 'select', 'checkbox', 'number', 'status'}
VALID_STATUS_COLORS = {'red', 'orange', 'blue', 'green', 'gray'}
VALID_VIEW_MODES = {'table', 'chart', 'table_chart', 'flow_progress'}
VALID_CHART_TYPES = {'line', 'bar', 'stacked_bar', 'pie', 'summary_cards'}


def _slug(value):
    text = ''.join(ch.lower() if ch.isalnum() else '_' for ch in str(value or '').strip())
    text = '_'.join(part for part in text.split('_') if part)
    return text[:80] or 'tab'


def _normalize_column(col):
    if not isinstance(col, dict):
        title = str(col or '').strip()
        return {
            'key': _slug(title),
            'title': title or '列',
            'type': 'text',
        }

    key = _slug(col.get('key') or col.get('title'))
    ctype = col.get('type') or 'text'
    if ctype not in VALID_CELL_TYPES:
        ctype = 'text'
    out = {
        'key': key,
        'title': str(col.get('title') or key),
        'type': ctype,
    }
    if ctype in {'select', 'status'}:
        options = []
        for opt in (col.get('options') or []):
            if isinstance(opt, dict):
                color = opt.get('color') if opt.get('color') in VALID_STATUS_COLORS else 'gray'
                options.append({'value': str(opt.get('value') or ''), 'label': str(opt.get('label') or opt.get('value') or ''), 'color': color})
            else:
                options.append({'value': str(opt), 'label': str(opt), 'color': 'gray'})
        out['options'] = options
    if col.get('width'):
        out['width'] = str(col.get('width'))
    for color_key in ('text_color', 'color'):
        if col.get(color_key):
            out[color_key] = str(col.get(color_key))
    return out


def _normalize_row(row, idx):
    row = row or {}
    row_id = str(row.get('id') or row.get('row_id') or f'row_{idx + 1}')
    if isinstance(row.get('cells'), dict):
        cells = dict(row.get('cells') or {})
    else:
        cells = {
            key: value for key, value in row.items()
            if key not in {'id', 'row_id', 'cells'}
        }
    return {'id': row_id, 'cells': dict(cells)}


def _normalize_chart(chart, idx):
    chart_id = _slug(chart.get('id') or chart.get('title') or f'chart_{idx + 1}')
    ctype = chart.get('type') or 'bar'
    if ctype not in VALID_CHART_TYPES:
        ctype = 'bar'
    out = {
        'id': chart_id,
        'title': str(chart.get('title') or chart_id),
        'type': ctype,
    }
    for key in ('x', 'y', 'series', 'stack', 'value', 'label'):
        if key in chart:
            out[key] = chart[key]
    if ctype == 'summary_cards' and isinstance(chart.get('cards'), list):
        out['cards'] = [
            dict(card) for card in chart.get('cards')
            if isinstance(card, dict)
        ]
    return out


def normalize_tab_payload(data):
    """Normalize API payload for a dynamic iteration tab."""
    data = data or {}
    tab_key = _slug(data.get('tab_key') or data.get('key') or data.get('title'))
    raw_columns = data.get('columns') or data.get('headers') or data.get('fields') or []
    raw_rows = data.get('rows') or data.get('data') or data.get('items') or []
    columns = [_normalize_column(c) for c in raw_columns]
    rows = [_normalize_row(r, i) for i, r in enumerate(raw_rows) if isinstance(r, dict)]
    if not columns and rows:
        first_cells = rows[0].get('cells') or {}
        columns = [_normalize_column(key) for key in first_cells.keys()]
    charts = [_normalize_chart(c, i) for i, c in enumerate(data.get('charts') or []) if isinstance(c, dict)]
    view_mode = data.get('view_mode')
    if view_mode not in VALID_VIEW_MODES:
        view_mode = 'table_chart' if charts else 'table'
    return {
        'tab_key': tab_key,
        'title': str(data.get('title') or tab_key),
        'tab_type': data.get('tab_type') if data.get('tab_type') in ('requirement', 'bug', 'custom') else 'custom',
        'view_mode': view_mode,
        'columns': columns,
        'rows': rows,
        'charts': charts,
    }


def should_preserve_existing_rows(existing_rows, incoming_rows, data):
    """Protect populated tabs from accidental full-save payloads with empty rows."""
    if not existing_rows or incoming_rows:
        return False
    data = data or {}
    if data.get('clear_rows') is True or data.get('replace_rows') is True or data.get('allow_empty_rows') is True:
        return False
    return True


def update_cell_value(rows, row_id, column_key, value):
    """Return a copy of rows with a single target cell updated."""
    target_row_id = str(row_id)
    target_col = str(column_key)
    updated = []
    found = False
    for row in rows or []:
        copied = {'id': str(row.get('id')), 'cells': dict(row.get('cells') or {})}
        if copied['id'] == target_row_id:
            copied['cells'][target_col] = value
            found = True
        updated.append(copied)
    if not found:
        raise ValueError('row_id 不存在')
    return updated


def append_row(rows, row):
    """Append a normalized row and reject duplicate row ids."""
    existing = [_normalize_row(r, i) for i, r in enumerate(rows or []) if isinstance(r, dict)]
    new_row = _normalize_row(row or {}, len(existing))
    if any(r['id'] == new_row['id'] for r in existing):
        raise ValueError('row_id 已存在')
    return existing + [new_row]


def append_chart(charts, chart):
    """Append a normalized chart and reject duplicate chart ids."""
    existing = [_normalize_chart(c, i) for i, c in enumerate(charts or []) if isinstance(c, dict)]
    new_chart = _normalize_chart(chart or {}, len(existing))
    if any(c['id'] == new_chart['id'] for c in existing):
        raise ValueError('chart_id 已存在')
    return existing + [new_chart]


def can_manage_tab(user, created_by):
    if not user:
        return False
    role = user.get('role') if isinstance(user, dict) else getattr(user, 'role', '')
    if role in ('super_admin', 'admin'):
        return True
    username = user.get('username') if isinstance(user, dict) else getattr(user, 'username', '')
    claw_name = user.get('_claw_name') if isinstance(user, dict) else getattr(user, '_claw_name', None)
    return bool(created_by and created_by in {username, claw_name})


def can_edit_tab_cell(user):
    return bool(user)


# ---------- flow_progress helpers ----------

_FLOW_STATUS_MAP = {
    'passed': 'green', 'pass': 'green', 'done': 'green',
    'green': 'green', '通过': 'green', '完成': 'green',
    'failed': 'red', 'fail': 'red', 'error': 'red',
    'red': 'red', '不通过': 'red', '失败': 'red',
    'running': 'blue', 'in_progress': 'blue',
    'blue': 'blue', '进行中': 'blue',
    'blocked': 'orange', 'warning': 'orange',
    'orange': 'orange', '异常': 'orange', '阻塞': 'orange',
    'pending': 'gray', 'gray': 'gray', 'grey': 'gray',
    '未开始': 'gray', '待开始': 'gray',
}

DEFAULT_FLOW_STEPS = [
    {'id': 'daily_merge', 'name': '每日合线'},
    {'id': 'editor_smoke', 'name': '编辑器冒烟'},
    {'id': 'submit_build', 'name': '提交构建'},
    {'id': 'mobile_smoke', 'name': '手机包冒烟'},
    {'id': 'report', 'name': '报告'},
]


def flow_status_color(value):
    """Map any status string to a canonical color name."""
    key = str(value or '').strip().lower().replace(' ', '_').replace('-', '_')
    return _FLOW_STATUS_MAP.get(key, 'gray')


def _normalize_flow_step(step, idx):
    if not isinstance(step, dict):
        step = {'name': str(step or '')}
    step_id = str(step.get('id') or step.get('step_key') or step.get('key')
                  or _slug(step.get('name') or f'step_{idx + 1}'))
    name = str(step.get('name') or step.get('title') or step_id)
    status = str(step.get('status') or 'pending')
    cells = {
        'name': name,
        'status': status,
        'message': step.get('message', ''),
        'updated_by': str(step.get('updated_by') or step.get('reporter') or ''),
        'updated_at': str(step.get('updated_at') or step.get('time') or ''),
    }
    if step.get('link'):
        cells['link'] = str(step['link'])
    return {'id': step_id, 'cells': cells}


def normalize_flow_steps_payload(data, existing_rows=None, actor=''):
    """Normalize an Agent flow-progress upload.

    ``data`` may contain ``steps`` (list) and optionally ``replace_steps``.
    When ``replace_steps`` is falsy, incoming steps are merged into
    ``existing_rows`` by step id; steps not present in the upload are kept.
    """
    data = data or {}
    raw_steps = data.get('steps') or data.get('rows') or data.get('items') or []
    if not raw_steps:
        raw_steps = list(DEFAULT_FLOW_STEPS)
    steps = [_normalize_flow_step(s, i) for i, s in enumerate(raw_steps)]

    if actor:
        for s in steps:
            if not s['cells'].get('updated_by'):
                s['cells']['updated_by'] = actor

    if data.get('replace_steps') or not existing_rows:
        return steps

    existing_by_id = {str(r.get('id')): dict(r) for r in (existing_rows or [])}
    for s in steps:
        old = existing_by_id.get(s['id'])
        if old:
            merged_cells = dict(old.get('cells') or {})
            for k, v in s['cells'].items():
                if v or v == 0 or v is False:
                    merged_cells[k] = v
            existing_by_id[s['id']] = {'id': s['id'], 'cells': merged_cells}
        else:
            existing_by_id[s['id']] = s

    existing_ids = [str(r.get('id')) for r in (existing_rows or [])]
    new_ids = [s['id'] for s in steps if s['id'] not in set(existing_ids)]
    return [existing_by_id[sid] for sid in existing_ids if sid in existing_by_id] + \
           [existing_by_id[sid] for sid in new_ids]
