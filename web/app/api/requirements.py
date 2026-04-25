"""需求分析中心 API（Requirement Analysis Center）

设计文档：c:/Users/rajqiu/.cursor/plans/需求分析模块设计_243aff00.plan.md
对应 Skill：openclaw-agent/skills/requirement-analysis/SKILL.md

阶段 1 MVP 范围：
  - Agent 推送类（agent → hub）
      POST /requirements/iterations/<id>/snapshots
      POST /requirements/tapd-cache/iterations
      POST /requirements/tapd-cache/versions
      POST /requirements/tapd-cache/baselines
      POST /requirements/tapd-cache/field-map
  - 前端查询类
      GET  /requirements/iterations/<id>/items
      GET  /requirements/iterations/<id>/changes
      GET  /requirements/iterations/<id>/summary
      GET  /requirements/items/<id>
      GET  /requirements/items/<id>/engineering-changes
      GET  /requirements/tapd-cache/iterations
      GET  /requirements/tapd-cache/versions
      GET  /requirements/tapd-cache/baselines
  - 用例关联
      POST   /requirements/items/<id>/testcase-links
      DELETE /requirements/testcase-links/<id>
  - 实时刷新队列（R1：agent 反向轮询，端到端 ≤ 15s）
      POST /requirements/tapd-refresh-requests
      GET  /requirements/tapd-refresh-requests/<id>
      GET  /requirements/agent/tapd-refresh-queue?claw_id=&limit=
      POST /requirements/agent/tapd-refresh-queue/<id>/done
      POST /requirements/agent/tapd-refresh-queue/<id>/fail

所有路由挂在 api_bp 下，鉴权由 app/api/__init__.py 的 require_auth 统一处理：
  - 用户走 Web session
  - Agent 走 Authorization: Bearer <OpenClawInstance.api_token_plain>
"""
from datetime import datetime, date
from flask import request, jsonify, session
from sqlalchemy import desc, func

from app import db
from app.api import api_bp
from app.api.audit import log_action
from app.api.skills import _get_current_user
from app.models import (
    EngineeringChangeItem,
    OpenClawInstance,
    Project,
    RequirementChangeLog,
    RequirementEngineeringLink,
    RequirementItem,
    RequirementTestcaseLink,
    TapdBaseline,
    TapdFieldMapCache,
    TapdIterationsCache,
    TapdRefreshRequest,
    TapdVersion,
    TestCase,
    TestIteration,
    _now,
)


# ==================== 通用工具 ====================

def _operator():
    user = _get_current_user()
    if not user:
        return 'system'
    return getattr(user, '_claw_name', None) or user.username or 'system'


def _is_missing(v):
    """同 engineering.py：避免 0/空白被误判"""
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def _to_int(v, field_name):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} 必须为整数')


def _parse_dt(v):
    """TAPD 字符串时间 → datetime（容错）"""
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip()
    if not s or s in ('0000-00-00', '0000-00-00 00:00:00'):
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_date(v):
    dt = _parse_dt(v)
    return dt.date() if dt else None


def _strip_html(html):
    """简单的 HTML → 纯文本，给 LLM 用"""
    if not html:
        return ''
    import re
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    return text.strip()


def _current_claw():
    """从 Authorization Bearer 解出 OpenClaw 实例（agent 推送场景使用）。
    没有也不报错，返回 None（用户 Web 调用就走这条路径）。"""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:]
    if not token:
        return None
    for c in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
        if c.verify_token(token):
            return c
    return None


# ==================== 字段比对（生成变更日志） ====================

# 关键字段：纳入 diff，change_type 取 modified
_DIFF_FIELDS = [
    ('title', 'low'),
    ('status', 'high'),                 # 状态变更优先级最高
    ('priority_label', 'medium'),
    ('owner', 'low'),
    ('developer', 'low'),
    ('tapd_module', 'medium'),
    ('feature', 'medium'),
    ('tapd_version', 'high'),
    ('tapd_release_id', 'medium'),
    ('tapd_baseline_id', 'high'),
    ('acceptance_criteria', 'high'),    # 测试验收变了 → 用例必改
    ('test_focus', 'medium'),
    ('need_test', 'high'),
    ('review_progress', 'medium'),
]


def _diff_and_log(old_item, new_values, today):
    """对一条已有 item 比对新值，写 RequirementChangeLog
    返回 (modified_count, status_changed)
    使用 query 提前判重避免唯一约束冲突，保持事务干净。
    """
    if not old_item:
        return 0, False

    modified = 0
    status_changed = False
    for field, default_impact in _DIFF_FIELDS:
        old_v = getattr(old_item, field, None)
        new_v = new_values.get(field)
        if (old_v or '') == (new_v or ''):
            continue
        ct = 'status_changed' if field == 'status' else 'modified'
        # 唯一约束 (item_id, change_date, field_name, change_type)：先查重
        exists = RequirementChangeLog.query.filter_by(
            requirement_item_id=old_item.id,
            change_date=today,
            field_name=field,
            change_type=ct,
        ).first()
        if exists:
            # 同日同字段同类型：把 new_value 更新到最新值，保留首次的 old_value
            exists.new_value = str(new_v) if new_v is not None else ''
            continue
        db.session.add(RequirementChangeLog(
            requirement_item_id=old_item.id,
            iteration_id=old_item.iteration_id,
            change_date=today,
            change_type=ct,
            field_name=field,
            old_value=str(old_v) if old_v is not None else '',
            new_value=str(new_v) if new_v is not None else '',
            impact_level=default_impact,
        ))
        modified += 1
        if ct == 'status_changed':
            status_changed = True
    return modified, status_changed


def _auto_link_engineering_changes(item):
    """对一条 RequirementItem，扫描 EngineeringChangeItem.tapd_story_ids 包含其
    tapd_story_id 的，自动建 RequirementEngineeringLink。
    使用 ORM 加载后 Python 侧匹配（规模小，足够 MVP）。
    """
    sid = str(item.tapd_story_id)
    if not sid:
        return 0
    created = 0
    # 简单全表扫不可行，限定到本迭代相关的 batch -> change_item
    # MVP：扫描全部含 tapd_story_ids 的 change_item（一般规模可控）
    candidates = EngineeringChangeItem.query.filter(
        EngineeringChangeItem.tapd_story_ids.isnot(None)
    ).all()
    for ch in candidates:
        ids = ch.tapd_story_ids or []
        if isinstance(ids, str):
            # MariaDB LONGTEXT 直接读出 JSON 反序列化失败时的兜底
            import json
            try:
                ids = json.loads(ids)
            except Exception:
                continue
        if not isinstance(ids, list):
            continue
        if sid not in [str(x) for x in ids]:
            continue
        exists = RequirementEngineeringLink.query.filter_by(
            requirement_item_id=item.id,
            change_item_id=ch.id,
        ).first()
        if exists:
            continue
        db.session.add(RequirementEngineeringLink(
            requirement_item_id=item.id,
            change_item_id=ch.id,
            link_source='auto_tapd_id',
            confidence=95,
        ))
        created += 1
    return created


# ==================== Agent 推送：需求快照 ====================

@api_bp.route('/requirements/iterations/<int:iteration_id>/snapshots',
              methods=['POST'])
def push_iteration_snapshot(iteration_id):
    """Agent 推送某次迭代的需求快照（全量 upsert + 自动 diff + 自动关联工程变更）

    Request body:
    {
        "tapd_workspace_id": "70202650",
        "stories": [
            {"id": "1170202650...", "name": "...", "status": "status_22",
             "owner": "rajqiu;abc", "priority_label": "High",
             "iteration_id": "...", "version": "M1版本",
             "release_id": "...", "baseline_id": "...",
             "custom_field_eight": "...", ...全字段透传},
            ...
        ],
        "removed_story_ids": ["..."]   // 可选，agent 算好的从迭代中移除的
    }
    """
    iteration = TestIteration.query.get(iteration_id)
    if not iteration:
        return jsonify({'error': '迭代不存在'}), 404

    data = request.get_json() or {}
    workspace_id = data.get('tapd_workspace_id') or ''
    stories = data.get('stories') or []
    removed_ids = [str(x) for x in (data.get('removed_story_ids') or [])]

    if not isinstance(stories, list):
        return jsonify({'error': 'stories 必须为数组'}), 400

    today = _now().date()
    upserted = 0
    diff_total = 0
    auto_linked = 0
    new_item_ids = []

    for s in stories:
        sid = str(s.get('id') or '').strip()
        if not sid:
            continue

        # 字段映射：TAPD 字段名 → 本地字段名
        new_values = {
            'title': (s.get('name') or '')[:500],
            'description': s.get('description'),
            'description_text': _strip_html(s.get('description'))[:65000],
            'status': s.get('status'),
            'priority_label': s.get('priority_label') or s.get('priority'),
            'priority_num': int(s.get('priority') or 0) if str(s.get('priority') or '').isdigit() else 0,
            'owner': (s.get('owner') or '')[:500],
            'creator': (s.get('creator') or '')[:100],
            'developer': (s.get('developer') or '')[:500],
            'category_id': s.get('category_id'),
            'workitem_type_id': s.get('workitem_type_id'),
            'tapd_module': (s.get('module') or '')[:200],
            'feature': (s.get('feature') or '')[:200],
            'local_module_name': (s.get('local_module_name') or '')[:200],
            'tapd_version': s.get('version'),
            'tapd_release_id': s.get('release_id'),
            'tapd_baseline_id': s.get('baseline_id'),
            'acceptance_criteria': s.get('custom_field_eight') or s.get('acceptance_criteria'),
            'test_focus': s.get('test_focus') or s.get('custom_field_three'),
            'test_result': s.get('test_result') or s.get('custom_field_six'),
            'need_test': s.get('custom_field_18') or s.get('need_test'),
            'review_progress': s.get('custom_field_19') or s.get('review_progress'),
            'parent_id': s.get('parent_id'),
            'children_id': s.get('children_id'),
            'tree_path': s.get('path'),
            'tapd_iteration_id': s.get('iteration_id'),
            'tapd_workspace_id': workspace_id or s.get('workspace_id'),
        }
        new_values['progress'] = int(s.get('progress') or 0) if str(s.get('progress') or '0').isdigit() else 0
        try:
            new_values['effort'] = float(s.get('effort') or 0)
        except (TypeError, ValueError):
            new_values['effort'] = 0
        try:
            new_values['effort_completed'] = float(s.get('effort_completed') or 0)
        except (TypeError, ValueError):
            new_values['effort_completed'] = 0
        try:
            new_values['remain'] = float(s.get('remain') or 0)
        except (TypeError, ValueError):
            new_values['remain'] = 0
        new_values['tech_risk'] = (s.get('tech_risk') or '')[:200]

        new_values['tapd_created_at'] = _parse_dt(s.get('created'))
        new_values['tapd_modified_at'] = _parse_dt(s.get('modified'))
        new_values['tapd_completed_at'] = _parse_dt(s.get('completed'))
        new_values['tapd_begin'] = _parse_date(s.get('begin'))
        new_values['tapd_due'] = _parse_date(s.get('due'))

        item = RequirementItem.query.filter_by(
            iteration_id=iteration_id, tapd_story_id=sid
        ).first()

        if item:
            # diff 写日志（在 update 之前比）
            mod, _ = _diff_and_log(item, new_values, today)
            diff_total += mod
            for k, v in new_values.items():
                setattr(item, k, v)
            item.local_synced_at = _now()
            item.raw_payload = s
            db.session.flush()
        else:
            item = RequirementItem(
                iteration_id=iteration_id,
                tapd_story_id=sid,
                local_synced_at=_now(),
                raw_payload=s,
                **new_values,
            )
            db.session.add(item)
            db.session.flush()
            # 新增日志
            db.session.add(RequirementChangeLog(
                requirement_item_id=item.id,
                iteration_id=iteration_id,
                change_date=today,
                change_type='added',
                field_name='',
                new_value=item.title or sid,
                impact_level='medium',
            ))
            new_item_ids.append(item.id)

        # 自动关联工程变更（重复链接已在函数内通过 query 判重避免）
        auto_linked += _auto_link_engineering_changes(item)

        upserted += 1

    # 移除项：标记 removed
    for rid in removed_ids:
        item = RequirementItem.query.filter_by(
            iteration_id=iteration_id, tapd_story_id=rid
        ).first()
        if not item:
            continue
        db.session.add(RequirementChangeLog(
            requirement_item_id=item.id,
            iteration_id=iteration_id,
            change_date=today,
            change_type='removed',
            field_name='',
            old_value=item.title or rid,
            impact_level='high',
        ))

    db.session.commit()

    log_action('sync', 'requirement_snapshot', iteration_id,
               iteration.name,
               operator=_operator(),
               detail=f'upsert={upserted} diff={diff_total} '
                      f'removed={len(removed_ids)} auto_link={auto_linked}')

    return jsonify({
        'iteration_id': iteration_id,
        'upserted': upserted,
        'new_count': len(new_item_ids),
        'diff_count': diff_total,
        'removed_count': len(removed_ids),
        'auto_linked_engineering_changes': auto_linked,
        'synced_at': str(_now()),
    }), 201


# ==================== Agent 推送：TAPD 缓存 ====================

@api_bp.route('/requirements/tapd-cache/iterations', methods=['POST'])
def push_iterations_cache():
    """Agent 推送 TAPD 迭代下拉缓存

    Request body:
    {
        "tapd_workspace_id": "70202650",
        "iterations": [
            {"id": "...", "name": "...", "status": "open",
             "startdate": "2025-01-01", "enddate": "2025-02-01",
             "creator": "...", "description": "...", "parent_id": "..."},
            ...
        ]
    }
    """
    data = request.get_json() or {}
    workspace_id = (data.get('tapd_workspace_id') or '').strip()
    iterations = data.get('iterations') or []
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 必填'}), 400
    if not isinstance(iterations, list):
        return jsonify({'error': 'iterations 必须为数组'}), 400

    upserted = 0
    for it in iterations:
        tid = str(it.get('id') or '').strip()
        if not tid:
            continue
        cache = TapdIterationsCache.query.filter_by(
            tapd_workspace_id=workspace_id, tapd_iteration_id=tid
        ).first()
        fields = {
            'name': (it.get('name') or '')[:200],
            'status': it.get('status'),
            'startdate': _parse_date(it.get('startdate')),
            'enddate': _parse_date(it.get('enddate')),
            'creator': (it.get('creator') or '')[:100],
            'description': it.get('description'),
            'parent_id': it.get('parent_id'),
            'last_synced_at': _now(),
        }
        if cache:
            for k, v in fields.items():
                setattr(cache, k, v)
            cache.cache_version = (cache.cache_version or 1) + 1
        else:
            db.session.add(TapdIterationsCache(
                tapd_workspace_id=workspace_id,
                tapd_iteration_id=tid,
                cache_version=1,
                **fields,
            ))
        upserted += 1

    db.session.commit()
    return jsonify({
        'tapd_workspace_id': workspace_id,
        'upserted': upserted,
        'synced_at': str(_now()),
    }), 201


@api_bp.route('/requirements/tapd-cache/versions', methods=['POST'])
def push_versions_cache():
    """Agent 推送 TAPD 版本缓存"""
    data = request.get_json() or {}
    workspace_id = (data.get('tapd_workspace_id') or '').strip()
    project_id = data.get('project_id')
    versions = data.get('versions') or []
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 必填'}), 400

    upserted = 0
    for v in versions:
        vid = str(v.get('id') or '').strip()
        if not vid:
            continue
        row = TapdVersion.query.filter_by(
            tapd_workspace_id=workspace_id, tapd_version_id=vid
        ).first()
        fields = {
            'project_id': project_id,
            'name': (v.get('name') or '')[:200],
            'description': v.get('description'),
            'status': v.get('status'),
            'version_type': v.get('version_type') or 'Normal version',
            'start': _parse_date(v.get('start')),
            'due': _parse_date(v.get('due')),
            'realbegin': _parse_date(v.get('realbegin')),
            'realend': _parse_date(v.get('realend')),
            'testtime': _parse_date(v.get('testtime')),
            'releasetime': _parse_date(v.get('releasetime')),
            'creator': (v.get('creator') or '')[:100],
            'owner': (v.get('owner') or '')[:500],
            'tapd_created_at': _parse_dt(v.get('created')),
            'tapd_modified_at': _parse_dt(v.get('modified')),
            'local_synced_at': _now(),
        }
        if row:
            for k, val in fields.items():
                setattr(row, k, val)
        else:
            db.session.add(TapdVersion(
                tapd_workspace_id=workspace_id,
                tapd_version_id=vid,
                **fields,
            ))
        upserted += 1

    db.session.commit()
    return jsonify({'upserted': upserted, 'synced_at': str(_now())}), 201


@api_bp.route('/requirements/tapd-cache/baselines', methods=['POST'])
def push_baselines_cache():
    """Agent 推送 TAPD 基线缓存"""
    data = request.get_json() or {}
    workspace_id = (data.get('tapd_workspace_id') or '').strip()
    baselines = data.get('baselines') or []
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 必填'}), 400

    upserted = 0
    for b in baselines:
        bid = str(b.get('id') or '').strip()
        if not bid:
            continue
        version_str = b.get('version_id')
        # 关联本地 tapd_versions
        local_version = None
        if version_str:
            local_version = TapdVersion.query.filter_by(
                tapd_workspace_id=workspace_id,
                tapd_version_id=str(version_str),
            ).first()

        row = TapdBaseline.query.filter_by(
            tapd_workspace_id=workspace_id, tapd_baseline_id=bid
        ).first()

        stories = b.get('stories_snapshot') or []
        fields = {
            'tapd_version_id_str': str(version_str) if version_str else None,
            'version_id': local_version.id if local_version else None,
            'name': (b.get('name') or '')[:200],
            'creator': (b.get('creator') or '')[:100],
            'tapd_created_at': _parse_dt(b.get('created')),
            'story_count': len(stories) if isinstance(stories, list) else 0,
            'stories_snapshot': stories,
            'local_synced_at': _now(),
        }
        if row:
            for k, val in fields.items():
                setattr(row, k, val)
        else:
            db.session.add(TapdBaseline(
                tapd_workspace_id=workspace_id,
                tapd_baseline_id=bid,
                **fields,
            ))
        upserted += 1

    db.session.commit()
    return jsonify({'upserted': upserted, 'synced_at': str(_now())}), 201


@api_bp.route('/requirements/tapd-cache/field-map', methods=['POST'])
def push_field_map_cache():
    """Agent 推送字段映射"""
    data = request.get_json() or {}
    workspace_id = (data.get('tapd_workspace_id') or '').strip()
    entity_type = data.get('entity_type') or 'story'
    field_map = data.get('field_map') or {}
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 必填'}), 400
    if not isinstance(field_map, dict):
        return jsonify({'error': 'field_map 必须为对象'}), 400

    row = TapdFieldMapCache.query.filter_by(
        tapd_workspace_id=workspace_id, entity_type=entity_type
    ).first()
    if row:
        row.field_map = field_map
        row.last_synced_at = _now()
    else:
        db.session.add(TapdFieldMapCache(
            tapd_workspace_id=workspace_id,
            entity_type=entity_type,
            field_map=field_map,
            last_synced_at=_now(),
        ))
    db.session.commit()
    return jsonify({
        'tapd_workspace_id': workspace_id,
        'entity_type': entity_type,
        'field_count': len(field_map),
        'synced_at': str(_now()),
    }), 201


# ==================== 前端查询 ====================

@api_bp.route('/requirements/iterations/<int:iteration_id>/items',
              methods=['GET'])
def list_iteration_items(iteration_id):
    """列出某迭代下所有需求"""
    TestIteration.query.get_or_404(iteration_id)
    query = RequirementItem.query.filter_by(iteration_id=iteration_id)

    status = request.args.get('status')
    if status:
        query = query.filter(RequirementItem.status == status)
    version = request.args.get('tapd_version')
    if version:
        query = query.filter(RequirementItem.tapd_version == version)
    baseline_id = request.args.get('tapd_baseline_id')
    if baseline_id:
        query = query.filter(RequirementItem.tapd_baseline_id == baseline_id)
    risk = request.args.get('risk_level')
    if risk:
        query = query.filter(RequirementItem.risk_level == risk)
    keyword = request.args.get('q')
    if keyword:
        like = f'%{keyword}%'
        query = query.filter(db.or_(
            RequirementItem.title.like(like),
            RequirementItem.tapd_story_id.like(like),
            RequirementItem.owner.like(like),
        ))

    items = query.order_by(
        desc(RequirementItem.priority_num),
        desc(RequirementItem.tapd_modified_at),
    ).all()

    return jsonify({
        'iteration_id': iteration_id,
        'total': len(items),
        'items': [it.to_dict() for it in items],
    })


@api_bp.route('/requirements/iterations/<int:iteration_id>/changes',
              methods=['GET'])
def list_iteration_changes(iteration_id):
    """列出某迭代下的变更日志"""
    TestIteration.query.get_or_404(iteration_id)
    query = RequirementChangeLog.query.filter_by(iteration_id=iteration_id)

    days = request.args.get('days', type=int)
    if days and days > 0:
        from datetime import timedelta
        cutoff = (_now().date() - timedelta(days=days))
        query = query.filter(RequirementChangeLog.change_date >= cutoff)

    impact = request.args.get('impact_level')
    if impact:
        query = query.filter(RequirementChangeLog.impact_level == impact)
    change_type = request.args.get('change_type')
    if change_type:
        query = query.filter(RequirementChangeLog.change_type == change_type)
    processed = request.args.get('processed')
    if processed in ('0', 'false'):
        query = query.filter(RequirementChangeLog.processed == False)  # noqa: E712
    elif processed in ('1', 'true'):
        query = query.filter(RequirementChangeLog.processed == True)   # noqa: E712

    logs = query.order_by(
        desc(RequirementChangeLog.change_date),
        desc(RequirementChangeLog.id),
    ).limit(500).all()

    return jsonify({
        'iteration_id': iteration_id,
        'total': len(logs),
        'changes': [l.to_dict() for l in logs],
    })


@api_bp.route('/requirements/iterations/<int:iteration_id>/summary',
              methods=['GET'])
def iteration_summary(iteration_id):
    """汇总：需求数、按状态分布、按风险分布、近 7 天变更数"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    items = RequirementItem.query.filter_by(iteration_id=iteration_id).all()
    total = len(items)

    status_dist = {}
    risk_dist = {}
    version_dist = {}
    baseline_dist = {}
    for it in items:
        status_dist[it.status or 'unknown'] = status_dist.get(it.status or 'unknown', 0) + 1
        risk_dist[it.risk_level or 'low'] = risk_dist.get(it.risk_level or 'low', 0) + 1
        if it.tapd_version:
            version_dist[it.tapd_version] = version_dist.get(it.tapd_version, 0) + 1
        if it.tapd_baseline_id:
            baseline_dist[it.tapd_baseline_id] = baseline_dist.get(it.tapd_baseline_id, 0) + 1

    from datetime import timedelta
    week_ago = _now().date() - timedelta(days=7)
    recent_changes = RequirementChangeLog.query.filter(
        RequirementChangeLog.iteration_id == iteration_id,
        RequirementChangeLog.change_date >= week_ago,
    ).count()
    high_impact_pending = RequirementChangeLog.query.filter(
        RequirementChangeLog.iteration_id == iteration_id,
        RequirementChangeLog.impact_level.in_(['high', 'medium']),
        RequirementChangeLog.processed == False,  # noqa: E712
    ).count()

    last_synced = (db.session.query(func.max(RequirementItem.local_synced_at))
                   .filter(RequirementItem.iteration_id == iteration_id)
                   .scalar())

    return jsonify({
        'iteration_id': iteration_id,
        'iteration_name': iteration.name,
        'total': total,
        'status_distribution': status_dist,
        'risk_distribution': risk_dist,
        'version_distribution': version_dist,
        'baseline_distribution': baseline_dist,
        'recent_change_count_7d': recent_changes,
        'high_impact_pending': high_impact_pending,
        'last_synced_at': str(last_synced) if last_synced else None,
    })


@api_bp.route('/requirements/items/<int:item_id>', methods=['GET'])
def get_requirement_item(item_id):
    item = RequirementItem.query.get_or_404(item_id)
    with_raw = request.args.get('with_raw') in ('1', 'true')
    data = item.to_dict(with_raw=with_raw)
    data['change_logs'] = [l.to_dict() for l in
                           item.change_logs.order_by(
                               desc(RequirementChangeLog.created_at)
                           ).limit(50).all()]
    data['testcase_links'] = [l.to_dict() for l in
                              item.testcase_links.all()]
    data['engineering_links'] = [l.to_dict() for l in
                                 item.engineering_links.all()]
    return jsonify(data)


@api_bp.route('/requirements/items/<int:item_id>/engineering-changes',
              methods=['GET'])
def list_item_engineering_changes(item_id):
    """看这个需求关联到哪些代码变更（含 EngineeringChangeItem 详情）"""
    item = RequirementItem.query.get_or_404(item_id)
    links = item.engineering_links.all()
    change_ids = [l.change_item_id for l in links]
    if not change_ids:
        return jsonify({'item_id': item_id, 'changes': []})
    changes = EngineeringChangeItem.query.filter(
        EngineeringChangeItem.id.in_(change_ids)
    ).all()
    chmap = {c.id: c.to_dict() for c in changes}
    return jsonify({
        'item_id': item_id,
        'changes': [{
            'link': l.to_dict(),
            'change': chmap.get(l.change_item_id),
        } for l in links],
    })


# ==================== TAPD 缓存查询（前端下拉用） ====================

@api_bp.route('/requirements/tapd-cache/iterations', methods=['GET'])
def get_iterations_cache():
    """前端下拉用，按 workspace_id 查本地缓存"""
    workspace_id = request.args.get('tapd_workspace_id')
    project_id = request.args.get('project_id', type=int)
    if not workspace_id and project_id is not None:
        proj = Project.query.get(project_id)
        if proj and getattr(proj, 'tapd_workspace_id', None):
            workspace_id = proj.tapd_workspace_id
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 或 project_id 必填'}), 400

    query = TapdIterationsCache.query.filter_by(tapd_workspace_id=workspace_id)
    status = request.args.get('status')
    if status:
        query = query.filter(TapdIterationsCache.status == status)

    items = query.order_by(desc(TapdIterationsCache.startdate)).all()
    last_synced = (db.session.query(func.max(TapdIterationsCache.last_synced_at))
                   .filter(TapdIterationsCache.tapd_workspace_id == workspace_id)
                   .scalar())
    return jsonify({
        'tapd_workspace_id': workspace_id,
        'total': len(items),
        'iterations': [it.to_dict() for it in items],
        'last_synced_at': str(last_synced) if last_synced else None,
    })


@api_bp.route('/requirements/tapd-cache/versions', methods=['GET'])
def get_versions_cache():
    workspace_id = request.args.get('tapd_workspace_id')
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 必填'}), 400
    query = TapdVersion.query.filter_by(tapd_workspace_id=workspace_id)
    status = request.args.get('status')
    if status:
        query = query.filter(TapdVersion.status == status)
    items = query.order_by(desc(TapdVersion.start)).all()
    return jsonify({
        'tapd_workspace_id': workspace_id,
        'total': len(items),
        'versions': [v.to_dict() for v in items],
    })


@api_bp.route('/requirements/tapd-cache/baselines', methods=['GET'])
def get_baselines_cache():
    workspace_id = request.args.get('tapd_workspace_id')
    if _is_missing(workspace_id):
        return jsonify({'error': 'tapd_workspace_id 必填'}), 400
    query = TapdBaseline.query.filter_by(tapd_workspace_id=workspace_id)
    version_str = request.args.get('tapd_version_id')
    if version_str:
        query = query.filter(TapdBaseline.tapd_version_id_str == version_str)
    items = query.order_by(desc(TapdBaseline.tapd_created_at)).all()
    return jsonify({
        'tapd_workspace_id': workspace_id,
        'total': len(items),
        'baselines': [b.to_dict() for b in items],
    })


# ==================== 用例关联 ====================

@api_bp.route('/requirements/items/<int:item_id>/testcase-links',
              methods=['POST'])
def create_testcase_link(item_id):
    item = RequirementItem.query.get_or_404(item_id)
    data = request.get_json() or {}
    if _is_missing(data.get('test_case_id')):
        return jsonify({'error': 'test_case_id 为必填项'}), 400
    try:
        tc_id = _to_int(data['test_case_id'], 'test_case_id')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if not TestCase.query.get(tc_id):
        return jsonify({'error': '用例不存在'}), 404

    exists = RequirementTestcaseLink.query.filter_by(
        requirement_item_id=item.id, test_case_id=tc_id
    ).first()
    if exists:
        return jsonify(exists.to_dict()), 200

    link = RequirementTestcaseLink(
        requirement_item_id=item.id,
        test_case_id=tc_id,
        link_type=data.get('link_type') or 'covers',
        coverage_status=data.get('coverage_status') or 'covered',
        created_by=_operator(),
    )
    db.session.add(link)
    db.session.commit()
    log_action('create', 'requirement_testcase_link', link.id,
               f'{item.tapd_story_id} ↔ case#{tc_id}',
               operator=_operator())
    return jsonify(link.to_dict()), 201


@api_bp.route('/requirements/testcase-links/<int:link_id>',
              methods=['DELETE'])
def delete_testcase_link(link_id):
    link = RequirementTestcaseLink.query.get_or_404(link_id)
    db.session.delete(link)
    db.session.commit()
    return jsonify({'deleted': True, 'id': link_id})


# ==================== 实时刷新队列（R1） ====================

@api_bp.route('/requirements/tapd-refresh-requests', methods=['POST'])
def create_refresh_request():
    """用户/前端发起一次实时刷新

    Request body:
    {
        "scope": "iterations" | "stories" | "all",
        "tapd_workspace_id": "70202650",        // 可选
        "iteration_id": 12,                     // 可选，缩小到某 Hub 迭代
        "tapd_iteration_id": "..."              // 可选
    }
    """
    data = request.get_json() or {}
    scope = data.get('scope') or 'iterations'
    if scope not in ('iterations', 'stories', 'all'):
        return jsonify({'error': 'scope 非法'}), 400

    iteration_id = data.get('iteration_id')
    if iteration_id is not None and not _is_missing(iteration_id):
        try:
            iteration_id = _to_int(iteration_id, 'iteration_id')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if not TestIteration.query.get(iteration_id):
            return jsonify({'error': '迭代不存在'}), 404
    else:
        iteration_id = None

    req = TapdRefreshRequest(
        scope=scope,
        tapd_workspace_id=data.get('tapd_workspace_id') or '',
        iteration_id=iteration_id,
        tapd_iteration_id=data.get('tapd_iteration_id') or '',
        status='pending',
        requested_by=_operator(),
    )
    db.session.add(req)
    db.session.commit()
    return jsonify(req.to_dict()), 201


@api_bp.route('/requirements/tapd-refresh-requests/<int:req_id>',
              methods=['GET'])
def get_refresh_request(req_id):
    req = TapdRefreshRequest.query.get_or_404(req_id)
    return jsonify(req.to_dict())


@api_bp.route('/requirements/agent/tapd-refresh-queue', methods=['GET'])
def agent_pick_refresh_queue():
    """Agent 反向轮询：拉取 pending 任务并标记为 picked

    Query: ?limit=10&claw_id=  (claw_id 可选，不传则从 Bearer token 解析)
    """
    claw = _current_claw()
    limit = request.args.get('limit', default=10, type=int)
    if limit <= 0 or limit > 50:
        limit = 10

    # SELECT pending → UPDATE 为 picked（简单 MVP 单表锁）
    pending = (TapdRefreshRequest.query
               .filter(TapdRefreshRequest.status == 'pending')
               .order_by(TapdRefreshRequest.id)
               .limit(limit)
               .all())
    picked = []
    now = _now()
    for r in pending:
        r.status = 'picked'
        r.picked_at = now
        if claw:
            r.picked_by_claw_id = claw.id
        picked.append(r)
    db.session.commit()

    return jsonify({
        'count': len(picked),
        'requests': [r.to_dict() for r in picked],
        'server_time': str(now),
    })


@api_bp.route('/requirements/agent/tapd-refresh-queue/<int:req_id>/done',
              methods=['POST'])
def agent_mark_refresh_done(req_id):
    req = TapdRefreshRequest.query.get_or_404(req_id)
    req.status = 'done'
    req.finished_at = _now()
    db.session.commit()
    return jsonify(req.to_dict())


@api_bp.route('/requirements/agent/tapd-refresh-queue/<int:req_id>/fail',
              methods=['POST'])
def agent_mark_refresh_fail(req_id):
    req = TapdRefreshRequest.query.get_or_404(req_id)
    data = request.get_json() or {}
    req.status = 'failed'
    req.finished_at = _now()
    req.error_message = (data.get('error_message') or '')[:5000]
    db.session.commit()
    return jsonify(req.to_dict())
