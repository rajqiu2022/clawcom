"""工程分析中心 API（Engineering Analysis Center）

设计文档：g:/RacingGoUnity/Documents/Testing/OpenClaw_Engineering_Analysis_Center_Design.md

阶段 1 MVP 范围：
  - 基线（EngineeringBaseline）CRUD
  - 增量刷新（AnalysisRefreshBatch）直录模式：
    一次性提交 changes + impacts，跳过 Agent 异步流程
  - 影响项（EngineeringTestImpactItem）认领/状态/关联用例
  - 一键写入 Hub 测试计划：在指定 iteration 下创建 test_plan + test_task

后续阶段会接入 Agent SSE 任务派发（refresh_type=manual/scheduled/webhook）。
此处先把闭环跑通。
"""
from datetime import datetime
from urllib.parse import urlparse
import re

from flask import request, jsonify, session
from sqlalchemy import desc, or_

from app import db
from app.api import api_bp
from app.api.audit import log_action
from app.api.skills import _get_current_user, _user_project_ids
from app.models import (
    AnalysisRefreshBatch,
    EngineeringArchitectureSnapshot,
    EngineeringBaseline,
    EngineeringChangeItem,
    EngineeringShare,
    EngineeringTestCaseLink,
    EngineeringTestImpactItem,
    Module,
    OpenClawInstance,
    Project,
    TestCase,
    TestCaseLibrary,
    TestIteration,
    TestPlan,
    TestTask,
    TestTaskCase,
    User,
    _now,
)


def _operator():
    user = _get_current_user()
    if not user:
        return 'system'
    return getattr(user, '_claw_name', None) or user.username or 'system'


# ==================================================================
# 权限模型（参见 OPENCLAW_SYSTEM_DESIGN.md 工程分析权限）
# ------------------------------------------------------------------
# 资源三类：baseline / batch / snapshot；都有 project_id（snapshot 通过冗余字段或回退 baseline）
# 角色：
#   super_admin / 全局 admin claw（龙虾王）  → 全部读写删
#   作者 triggered_by / created_by          → 自己创建的可读改删
#   同项目 admin（managed_projects 含本项目）→ 同项目可读可删
#   同项目用户/同项目 OpenClaw              → 只读
#   通过 EngineeringShare 显式开放          → 只读
#   其他                                    → 拒绝
# ==================================================================


def _is_super(user):
    return bool(user and user.role == 'super_admin')


def _is_admin(user):
    return bool(user and user.role in ('super_admin', 'admin'))


def _is_author(resource, user, operator_name):
    """作者判定：triggered_by / created_by 等于当前 operator 或用户名/绑定 claw 名。"""
    if not user:
        return False
    author = (getattr(resource, 'triggered_by', None)
              or getattr(resource, 'created_by', None) or '')
    if not author:
        return False
    candidates = {operator_name}
    candidates.add(getattr(user, 'username', None) or '')
    claw_name = getattr(user, '_claw_name', None) or getattr(user, 'bound_claw_name', None)
    if claw_name:
        candidates.add(claw_name)
    candidates.discard('')
    return author in candidates


def _resource_project_id(resource_type, resource):
    """统一拿资源所属项目 ID。snapshot 的 project_id 可能没回填 → 回退 baseline。"""
    if resource is None:
        return None
    if resource_type == 'snapshot':
        pid = getattr(resource, 'project_id', None)
        if pid:
            return pid
        if resource.baseline:
            return resource.baseline.project_id
        return None
    return getattr(resource, 'project_id', None)


def _user_managed_claw_ids(user):
    """该用户名下绑定的 OpenClaw ID 集合（共享给用户时，自动覆盖名下 claw）。"""
    if not user:
        return set()
    username = getattr(user, 'username', None)
    if not username:
        return set()
    rows = (OpenClawInstance.query
            .filter(OpenClawInstance.owner == username,
                    OpenClawInstance.status != 'deleted')
            .with_entities(OpenClawInstance.id).all())
    return {r[0] for r in rows}


def _list_active_share_grants(user):
    """构建当前调用方对所有资源的「显式共享授权」索引。
    返回 set of (resource_type, resource_id)。包含：
      - public 共享（任何已登录用户/claw 可见）
      - 授权给当前用户的（target_user_id == user.id）
      - 授权给当前用户名下任意 claw 的（target_claw_id ∈ managed claws）
      - 授权给当前 token 绑定 claw 的（target_claw_id == bound_claw_id）
    自动剔除已过期记录。
    """
    if not user:
        return set()

    now = _now()
    user_id = getattr(user, 'id', None)
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    managed_claw_ids = _user_managed_claw_ids(user)
    if bound_claw_id:
        managed_claw_ids.add(bound_claw_id)

    conds = [EngineeringShare.share_type == 'public']
    if user_id:
        conds.append(db.and_(EngineeringShare.share_type == 'user',
                             EngineeringShare.target_user_id == user_id))
    if managed_claw_ids:
        conds.append(db.and_(EngineeringShare.share_type == 'claw',
                             EngineeringShare.target_claw_id.in_(list(managed_claw_ids))))

    rows = (EngineeringShare.query
            .filter(or_(*conds))
            .filter(or_(EngineeringShare.expires_at == None,  # noqa: E711
                        EngineeringShare.expires_at > now))
            .with_entities(EngineeringShare.resource_type,
                           EngineeringShare.resource_id).all())
    return {(r[0], r[1]) for r in rows}


def _can_view(resource_type, resource, user, share_grants=None):
    """判断当前调用方能否查看该资源。"""
    if resource is None:
        return False
    if _is_super(user):
        return True
    operator_name = _operator() if user else None
    if _is_author(resource, user, operator_name):
        return True
    pid = _resource_project_id(resource_type, resource)
    if user and pid is not None:
        # 全局 admin claw（龙虾王，managed_projects=[]）：_is_super 已覆盖；
        # 普通 admin：仅当 managed_projects 含本项目时才视为同项目
        if _is_admin(user):
            mgr = _user_project_ids(user)
            if mgr and pid in mgr:
                return True
        # 同项目普通用户/普通 OpenClaw：用 managed_projects + token 绑定 claw 的项目
        user_pids = _user_project_ids(user)
        if user_pids and pid in user_pids:
            return True
    if share_grants is None:
        share_grants = _list_active_share_grants(user)
    return (resource_type, resource.id) in share_grants


def _can_edit(resource_type, resource, user):
    """编辑：作者本人 / super_admin。"""
    if _is_super(user):
        return True
    return _is_author(resource, user, _operator() if user else None)


def _can_delete(resource_type, resource, user):
    """删除：作者 / super_admin / 项目管理员（本项目 admin）。"""
    if _is_super(user):
        return True
    if _is_author(resource, user, _operator() if user else None):
        return True
    if _is_admin(user):
        pid = _resource_project_id(resource_type, resource)
        mgr = _user_project_ids(user)
        if pid is not None and pid in mgr:
            return True
    return False


def _can_share(resource_type, resource, user):
    """共享授权操作权限：作者 / super_admin / 项目管理员。"""
    return _can_delete(resource_type, resource, user)


def _filter_visible_ids(resource_type, candidate_ids, user, share_grants=None):
    """对一批资源 ID 做可见性裁剪。candidate_ids 必须是同 resource_type 的。
    用于 list 接口高效过滤（避免逐条查询）。
    返回保留的 id set。
    """
    if not candidate_ids:
        return set()
    if _is_super(user):
        return set(candidate_ids)
    if share_grants is None:
        share_grants = _list_active_share_grants(user)
    keep = set()
    # 把 candidate 一次性查出来按需判断
    if resource_type == 'baseline':
        rows = EngineeringBaseline.query.filter(
            EngineeringBaseline.id.in_(list(candidate_ids))).all()
    elif resource_type == 'batch':
        rows = AnalysisRefreshBatch.query.filter(
            AnalysisRefreshBatch.id.in_(list(candidate_ids))).all()
    elif resource_type == 'snapshot':
        rows = EngineeringArchitectureSnapshot.query.filter(
            EngineeringArchitectureSnapshot.id.in_(list(candidate_ids))).all()
    else:
        return set()
    for r in rows:
        if _can_view(resource_type, r, user, share_grants=share_grants):
            keep.add(r.id)
    return keep


def _forbidden(action_zh, hint=None):
    payload = {'error': f'无权{action_zh}：仅作者本人 / 项目管理员 / super_admin 可操作'}
    if hint:
        payload['hint'] = hint
    return jsonify(payload), 403


def _resource_loader(resource_type, resource_id):
    """按 type 加载资源，返回 None 或对象。"""
    if resource_type == 'baseline':
        return EngineeringBaseline.query.get(resource_id)
    if resource_type == 'batch':
        return AnalysisRefreshBatch.query.get(resource_id)
    if resource_type == 'snapshot':
        return EngineeringArchitectureSnapshot.query.get(resource_id)
    return None


def _is_missing(v):
    """统一的"必填"判定。
    避免 `if not x` 误把合法的整数 0 / Decimal 0 当作缺失。
    判定规则：None / 空字符串 / 全空白字符串 视为缺失；数值（含 0）视为有效。
    """
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def _to_int(v, field_name):
    """把可能是 str/int 的入参强转为 int，失败时抛 ValueError 提示字段名。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} 必须为整数')


# ==================== 基线 CRUD ====================

@api_bp.route('/engineering/baselines', methods=['GET'])
def list_baselines():
    """基线列表，支持 project_id / status 过滤。
    可见性：本项目成员 / super_admin / 通过 EngineeringShare 显式开放。
    """
    user = _get_current_user()
    query = EngineeringBaseline.query
    project_id = request.args.get('project_id', type=int)
    status = request.args.get('status')
    if project_id is not None:
        query = query.filter(EngineeringBaseline.project_id == project_id)
    if status:
        query = query.filter(EngineeringBaseline.status == status)
    items = query.order_by(desc(EngineeringBaseline.updated_at)).all()
    if not _is_super(user):
        share_grants = _list_active_share_grants(user)
        items = [b for b in items if _can_view('baseline', b, user, share_grants)]
    return jsonify({
        'baselines': [b.to_dict() for b in items],
        'total': len(items),
    })


@api_bp.route('/engineering/baselines/<int:baseline_id>', methods=['GET'])
def get_baseline(baseline_id):
    b = EngineeringBaseline.query.get_or_404(baseline_id)
    user = _get_current_user()
    if not _can_view('baseline', b, user):
        return _forbidden('查看该基线')
    data = b.to_dict()
    # 附带最近 5 次刷新批次概览（也按可见性过滤）
    recent = AnalysisRefreshBatch.query.filter_by(
        baseline_id=baseline_id
    ).order_by(desc(AnalysisRefreshBatch.created_at)).limit(5).all()
    if not _is_super(user):
        share_grants = _list_active_share_grants(user)
        recent = [r for r in recent if _can_view('batch', r, user, share_grants)]
    data['recent_batches'] = [r.to_dict() for r in recent]
    return jsonify(data)


@api_bp.route('/engineering/baselines', methods=['POST'])
def create_baseline():
    data = request.get_json() or {}
    for f in ('project_id', 'name', 'repo_url'):
        if _is_missing(data.get(f)):
            return jsonify({'error': f'{f} 为必填项'}), 400

    try:
        project_id = _to_int(data['project_id'], 'project_id')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    project = Project.query.get(project_id)
    if not project:
        return jsonify({'error': '项目不存在'}), 404

    # 创建基线：super_admin / 该项目成员（含 OpenClaw owner 持有的项目）
    user = _get_current_user()
    if not _is_super(user):
        user_pids = _user_project_ids(user) if user else set()
        if project_id not in user_pids:
            return _forbidden('在该项目下创建工程分析基线',
                              hint=f'project_id={project_id} 不在你的可管理项目内')

    b = EngineeringBaseline(
        project_id=project_id,
        name=data['name'].strip(),
        repo_url=data['repo_url'].strip(),
        branch=(data.get('branch') or 'main').strip(),
        baseline_commit=(data.get('baseline_commit') or '').strip() or None,
        architecture_doc_url=data.get('architecture_doc_url'),
        module_mapping=data.get('module_mapping') or {},
        risk_rules=data.get('risk_rules') or {},
        status=data.get('status') or 'active',
        created_by=_operator(),
    )
    db.session.add(b)
    db.session.commit()
    log_action('create', 'engineering_baseline', b.id, b.name,
               operator=_operator(), detail=f'项目={project.name} 仓库={b.repo_url}')
    return jsonify(b.to_dict()), 201


@api_bp.route('/engineering/baselines/<int:baseline_id>', methods=['PUT'])
def update_baseline(baseline_id):
    b = EngineeringBaseline.query.get_or_404(baseline_id)
    user = _get_current_user()
    if not _can_edit('baseline', b, user):
        return _forbidden('编辑该基线')
    data = request.get_json() or {}

    for f in ['name', 'repo_url', 'branch', 'baseline_commit',
              'architecture_doc_url', 'status']:
        if f in data and data[f] is not None:
            setattr(b, f, data[f])
    if 'module_mapping' in data:
        b.module_mapping = data['module_mapping'] or {}
    if 'risk_rules' in data:
        b.risk_rules = data['risk_rules'] or {}

    db.session.commit()
    log_action('update', 'engineering_baseline', b.id, b.name,
               operator=_operator())
    return jsonify(b.to_dict())


@api_bp.route('/engineering/baselines/<int:baseline_id>', methods=['DELETE'])
def delete_baseline(baseline_id):
    b = EngineeringBaseline.query.get_or_404(baseline_id)
    user = _get_current_user()
    if not _can_delete('baseline', b, user):
        return _forbidden('删除该基线')
    name = b.name
    # 软删：把 status 改成 inactive，避免误删历史批次
    b.status = 'inactive'
    db.session.commit()
    log_action('delete', 'engineering_baseline', baseline_id, name,
               operator=_operator(), detail='软删除：status=inactive')
    return jsonify({'status': 'deleted', 'soft': True})


# ==================== 刷新批次 ====================

@api_bp.route('/engineering/refresh', methods=['GET'])
def list_refresh_batches():
    """刷新批次列表，支持 project_id / baseline_id / status / iteration_id 过滤。
    可见性：本项目成员 / super_admin / 通过 EngineeringShare 显式开放。
    """
    user = _get_current_user()
    query = AnalysisRefreshBatch.query
    for field, key, cast in [
        ('project_id', 'project_id', int),
        ('baseline_id', 'baseline_id', int),
        ('iteration_id', 'iteration_id', int),
    ]:
        v = request.args.get(field, type=cast)
        if v is not None:
            query = query.filter(getattr(AnalysisRefreshBatch, key) == v)
    status = request.args.get('status')
    if status:
        query = query.filter(AnalysisRefreshBatch.status == status)

    # 不分页拉够鉴权再分页：避免某页被全部过滤掉
    all_items = query.order_by(desc(AnalysisRefreshBatch.created_at)).all()
    if not _is_super(user):
        share_grants = _list_active_share_grants(user)
        all_items = [b for b in all_items if _can_view('batch', b, user, share_grants)]
    total = len(all_items)
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, request.args.get('per_page', 30, type=int))
    start = (page - 1) * per_page
    items = all_items[start:start + per_page]
    return jsonify({
        'batches': [b.to_dict() for b in items],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
    })


@api_bp.route('/engineering/refresh/<int:batch_id>', methods=['GET'])
def get_refresh_batch(batch_id):
    b = AnalysisRefreshBatch.query.get_or_404(batch_id)
    user = _get_current_user()
    if not _can_view('batch', b, user):
        return _forbidden('查看该刷新批次')
    return jsonify(b.to_dict(with_details=True))


@api_bp.route('/engineering/refresh', methods=['POST'])
def create_refresh_batch():
    """触发一次刷新。

    MVP 仅支持 refresh_type='direct'：调用方一次性把 changes/impacts 录入。
    后续会支持 refresh_type='manual'：派发任务给 Agent 异步执行。

    请求体（direct 模式）：
    {
        "baseline_id": 1,
        "refresh_type": "direct",
        "iteration_id": 12,                  // 可选
        "from_commit": "abc1234",
        "to_commit": "def5678",
        "commit_count": 23,
        "changed_file_count": 18,
        "summary": "本次主要影响登录与房间匹配模块...",
        "risk_level": "medium",
        "changes": [
            {
                "file_path": "Assets/Scripts/Network/Login.cs",
                "change_type": "modify",
                "module_name": "network",
                "module_id": 5,              // 可选
                "symbol_names": ["LoginManager.OnReceive"],
                "impact_tags": ["login","network"],
                "risk_score": 70,
                "reason": "修改了断线重连逻辑",
                "tapd_story_ids": ["1234567890"]
            }
        ],
        "impacts": [
            {
                "module_name": "network",
                "library_id": 3,             // 可选
                "feature_chain": "登录 > 断线重连",
                "action_type": "update_case",
                "priority": "P1",
                "suggestion": "补充弱网下连续 3 次重连后切换协议的回归用例",
                "acceptance_criteria": "..."
            }
        ]
    }
    """
    data = request.get_json() or {}
    if _is_missing(data.get('baseline_id')):
        return jsonify({'error': 'baseline_id 为必填项'}), 400
    try:
        baseline_id = _to_int(data['baseline_id'], 'baseline_id')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    baseline = EngineeringBaseline.query.get(baseline_id)
    if not baseline:
        return jsonify({'error': '基线不存在'}), 404

    # 能看到该基线（同项目 / 共享 / super_admin）才能贡献批次
    user = _get_current_user()
    if not _can_view('baseline', baseline, user):
        return _forbidden('在该基线下创建刷新批次')

    refresh_type = data.get('refresh_type') or 'direct'
    if refresh_type not in ('manual', 'scheduled', 'webhook', 'direct'):
        return jsonify({'error': f'非法 refresh_type: {refresh_type}'}), 400

    if refresh_type != 'direct':
        return jsonify({
            'error': '当前 MVP 仅支持 refresh_type=direct，'
                     'Agent 派发模式将在阶段 2 上线'
        }), 400

    iteration_id = data.get('iteration_id')
    if iteration_id is not None and iteration_id != '':
        try:
            iteration_id = _to_int(iteration_id, 'iteration_id')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if not TestIteration.query.get(iteration_id):
            return jsonify({'error': '关联迭代不存在'}), 404
    else:
        iteration_id = None

    changes = data.get('changes') or []
    impacts = data.get('impacts') or []

    batch = AnalysisRefreshBatch(
        project_id=baseline.project_id,
        baseline_id=baseline.id,
        iteration_id=iteration_id,
        refresh_type='direct',
        from_commit=data.get('from_commit'),
        to_commit=data.get('to_commit'),
        commit_count=int(data.get('commit_count') or len(changes)),
        changed_file_count=int(data.get('changed_file_count') or len(changes)),
        summary=data.get('summary'),
        risk_level=data.get('risk_level'),
        status='success',
        result_payload=data.get('result_payload') or None,
        triggered_by=_operator(),
        started_at=_now(),
        finished_at=_now(),
    )
    db.session.add(batch)
    db.session.flush()  # 拿到 batch.id

    for ch in changes:
        if not ch.get('file_path'):
            continue
        db.session.add(EngineeringChangeItem(
            batch_id=batch.id,
            file_path=ch['file_path'],
            change_type=ch.get('change_type') or 'modify',
            module_id=ch.get('module_id'),
            module_name=ch.get('module_name') or '',
            symbol_names=ch.get('symbol_names') or [],
            impact_tags=ch.get('impact_tags') or [],
            risk_score=int(ch.get('risk_score') or 0),
            reason=ch.get('reason'),
            tapd_story_ids=ch.get('tapd_story_ids') or [],
        ))

    for im in impacts:
        db.session.add(EngineeringTestImpactItem(
            batch_id=batch.id,
            library_id=im.get('library_id'),
            module_id=im.get('module_id'),
            module_name=im.get('module_name') or '',
            feature_chain=im.get('feature_chain'),
            action_type=im.get('action_type') or 'update_case',
            priority=im.get('priority') or 'P2',
            suggestion=im.get('suggestion'),
            acceptance_criteria=im.get('acceptance_criteria'),
            owner=im.get('owner') or '',
            status='todo',
        ))

    # ===== Hook：自动建立"工程变更 ↔ 需求"关联 =====
    # 当 change_item 携带 tapd_story_ids 时，根据 batch.iteration_id 在
    # requirement_items 中按 (iteration_id, tapd_story_id) 找匹配项，建链接
    auto_linked = 0
    if batch.iteration_id:
        from app.models import RequirementEngineeringLink, RequirementItem
        db.session.flush()
        new_changes = EngineeringChangeItem.query.filter_by(batch_id=batch.id).all()
        req_items_by_sid = {}
        req_q = RequirementItem.query.filter_by(iteration_id=batch.iteration_id).all()
        for r in req_q:
            req_items_by_sid[str(r.tapd_story_id)] = r
        for ch in new_changes:
            ids = ch.tapd_story_ids or []
            if isinstance(ids, str):
                import json
                try:
                    ids = json.loads(ids)
                except Exception:
                    ids = []
            for sid in ids:
                item = req_items_by_sid.get(str(sid))
                if not item:
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
                auto_linked += 1

    db.session.commit()
    log_action('create', 'engineering_refresh_batch', batch.id,
               f'{baseline.name} {batch.from_commit}..{batch.to_commit}',
               operator=_operator(),
               detail=f'变更={len(changes)} 影响项={len(impacts)} '
                      f'风险={batch.risk_level} 需求关联={auto_linked}')
    return jsonify(batch.to_dict(with_details=True)), 201


@api_bp.route('/engineering/refresh/<int:batch_id>/changes', methods=['GET'])
def list_batch_changes(batch_id):
    b = AnalysisRefreshBatch.query.get_or_404(batch_id)
    user = _get_current_user()
    if not _can_view('batch', b, user):
        return _forbidden('查看该批次的变更明细')
    query = EngineeringChangeItem.query.filter_by(batch_id=batch_id)
    module_name = request.args.get('module_name')
    if module_name:
        query = query.filter(EngineeringChangeItem.module_name == module_name)
    items = query.order_by(desc(EngineeringChangeItem.risk_score),
                           EngineeringChangeItem.id).all()
    return jsonify({
        'changes': [c.to_dict() for c in items],
        'total': len(items),
    })


@api_bp.route('/engineering/refresh/<int:batch_id>/impacts', methods=['GET'])
def list_batch_impacts(batch_id):
    b = AnalysisRefreshBatch.query.get_or_404(batch_id)
    user = _get_current_user()
    if not _can_view('batch', b, user):
        return _forbidden('查看该批次的影响项')
    query = EngineeringTestImpactItem.query.filter_by(batch_id=batch_id)
    status = request.args.get('status')
    if status:
        query = query.filter(EngineeringTestImpactItem.status == status)
    module_name = request.args.get('module_name')
    if module_name:
        query = query.filter(
            EngineeringTestImpactItem.module_name == module_name)
    priority = request.args.get('priority')
    if priority:
        query = query.filter(EngineeringTestImpactItem.priority == priority)

    items = query.order_by(EngineeringTestImpactItem.priority,
                           EngineeringTestImpactItem.id).all()
    return jsonify({
        'impacts': [i.to_dict(with_links=True) for i in items],
        'total': len(items),
    })


@api_bp.route('/engineering/refresh/<int:batch_id>/approve', methods=['POST'])
def approve_batch(batch_id):
    b = AnalysisRefreshBatch.query.get_or_404(batch_id)
    user = _get_current_user()
    # 审批权限与"删除"对齐：作者 / 项目管理员 / super_admin
    if not _can_delete('batch', b, user):
        return _forbidden('审批该批次')
    if b.status not in ('success', 'rejected'):
        return jsonify({'error': f'当前状态 {b.status} 不可审批通过'}), 400
    b.status = 'approved'
    b.approved_by = _operator()
    b.approved_at = _now()
    db.session.commit()
    log_action('review', 'engineering_refresh_batch', b.id,
               f'batch#{b.id}', operator=_operator(),
               detail='审批通过')
    return jsonify(b.to_dict())


@api_bp.route('/engineering/refresh/<int:batch_id>/reject', methods=['POST'])
def reject_batch(batch_id):
    b = AnalysisRefreshBatch.query.get_or_404(batch_id)
    user = _get_current_user()
    if not _can_delete('batch', b, user):
        return _forbidden('驳回该批次')
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if b.status not in ('success', 'approved'):
        return jsonify({'error': f'当前状态 {b.status} 不可驳回'}), 400
    b.status = 'rejected'
    b.reject_reason = reason
    b.approved_by = _operator()
    b.approved_at = _now()
    db.session.commit()
    log_action('review', 'engineering_refresh_batch', b.id,
               f'batch#{b.id}', operator=_operator(),
               detail=f'驳回：{reason}')
    return jsonify(b.to_dict())


@api_bp.route('/engineering/refresh/<int:batch_id>', methods=['DELETE'])
def delete_refresh_batch(batch_id):
    """删除一次刷新批次（含级联：change_items + impact_items + impact 上的 case_links）。

    权限：作者 / 项目管理员（managed_projects 含本项目） / super_admin

    业务约束：
      - 已 'approved' 的批次必须先驳回（避免抹掉已生效的决策）
      - 任何 impact 已经创建了下游 test task（linked_test_task_id 非空）→ 拒绝删除
    """
    b = AnalysisRefreshBatch.query.get_or_404(batch_id)
    user = _get_current_user()
    operator_name = _operator()

    if not _can_delete('batch', b, user):
        return _forbidden(
            '删除该批次',
            hint=f'triggered_by={b.triggered_by} operator={operator_name}')

    # —— 状态拦截：已审批通过的不允许直接删 ——
    if b.status == 'approved':
        return jsonify({
            'error': '该批次已审批通过，请先调用 /reject 驳回再删除',
        }), 409

    # —— 业务拦截：有 impact 已经创建了下游测试任务 ——
    impact_with_task = (EngineeringTestImpactItem.query
                       .filter_by(batch_id=batch_id)
                       .filter(EngineeringTestImpactItem.linked_test_task_id.isnot(None))
                       .all())
    if impact_with_task:
        task_ids = [im.linked_test_task_id for im in impact_with_task]
        return jsonify({
            'error': f'该批次有 {len(impact_with_task)} 条 impact 已写入测试任务（task_id={task_ids[:5]}{"..." if len(task_ids) > 5 else ""}），请先解绑或删除对应任务',
            'linked_task_ids': task_ids,
        }), 409

    # —— 级联硬删 ——
    impact_ids = [im.id for im in EngineeringTestImpactItem.query
                  .filter_by(batch_id=batch_id).all()]

    deleted_links = 0
    if impact_ids:
        deleted_links = (EngineeringTestCaseLink.query
                        .filter(EngineeringTestCaseLink.impact_item_id.in_(impact_ids))
                        .delete(synchronize_session=False))

    deleted_impacts = (EngineeringTestImpactItem.query
                       .filter_by(batch_id=batch_id)
                       .delete(synchronize_session=False))
    deleted_changes = (EngineeringChangeItem.query
                       .filter_by(batch_id=batch_id)
                       .delete(synchronize_session=False))

    batch_summary = (b.summary or '')[:80]
    db.session.delete(b)
    db.session.commit()

    log_action('delete', 'engineering_refresh_batch', batch_id,
               f'batch#{batch_id}', operator=operator_name,
               detail=(
                   f'硬删除：触发人={b.triggered_by} 状态={b.status} '
                   f'摘要="{batch_summary}" 级联删除 '
                   f'changes={deleted_changes} impacts={deleted_impacts} '
                   f'case_links={deleted_links}'
               ))
    return jsonify({
        'status': 'deleted',
        'soft': False,
        'batch_id': batch_id,
        'cascaded': {
            'change_items': deleted_changes,
            'impact_items': deleted_impacts,
            'case_links': deleted_links,
        },
    })


# ==================== 工程架构分析快照（双子模块改版）====================
#
# 一条 baseline 最多保留 10 个架构快照（LRU），支持 2 种分析方式：
#   - scope='full'   全工程级总览（模块清单 / 通信机制 / 关键链路 / 全局架构图）
#   - scope='module' 单模块深度分析（针对某模块出详细子架构 + 时序图）
#
# 内容三件套：structured(JSON) + content_md(Markdown 含 ```mermaid```) + summary
# Agent / Web / 人工录入共用同一个 POST 协议。

ARCH_LRU_KEEP = 10
ARCH_MD_MAX = 60000  # MariaDB Text 上限约 64KB，留点 buffer


def _trim_arch_snapshots(baseline_id, keep=ARCH_LRU_KEEP):
    """LRU：保留最新 keep 条，多余按 created_at asc 删除。返回删除数量。"""
    olds = (EngineeringArchitectureSnapshot.query
            .filter_by(baseline_id=baseline_id)
            .order_by(desc(EngineeringArchitectureSnapshot.created_at))
            .offset(keep).all())
    deleted = 0
    for s in olds:
        db.session.delete(s)
        deleted += 1
    return deleted


def _validate_arch_payload(data, partial=False):
    """校验创建 / 更新架构快照入参。partial=True 用于 PUT 部分更新。
    返回 (ok, error_message_or_None)。
    """
    if not partial:
        scope = data.get('scope')
        if scope not in ('full', 'module'):
            return False, 'scope 必须是 "full" 或 "module"'
        target = (data.get('target_module') or '').strip()
        if scope == 'full' and target:
            return False, 'scope=full 时 target_module 必须为空'
        if scope == 'module' and not target:
            return False, 'scope=module 时 target_module 必填'

    src = data.get('source_type')
    if src is not None and src not in ('agent', 'manual', 'memos_import'):
        return False, 'source_type 必须是 agent / manual / memos_import 之一'

    md = data.get('content_md')
    if md is not None and len(md) > ARCH_MD_MAX:
        return False, (f'content_md 长度 {len(md)} 超过上限 {ARCH_MD_MAX}，'
                       f'建议将"全工程"拆成多个"单模块"快照')

    structured = data.get('structured')
    if structured is not None and not isinstance(structured, (dict, list)):
        return False, 'structured 必须是 JSON 对象或数组'
    return True, None


@api_bp.route('/engineering/baselines/<int:baseline_id>/architecture',
              methods=['GET'])
def list_arch_snapshots(baseline_id):
    """列出该 baseline 下所有架构快照（最多 10 条，按时间倒序）。
    可选过滤：?scope=full|module&target_module=登录
    返回结构：{'snapshots': [...], 'total': N, 'lru_keep': 10}
    列表场景不返回 content_md（仅 200 字预览），减小响应体。
    可见性：和 baseline 相同（同项目成员 / super_admin / 共享授权）。
    """
    baseline = EngineeringBaseline.query.get_or_404(baseline_id)
    user = _get_current_user()
    if not _can_view('baseline', baseline, user):
        return _forbidden('查看该基线的架构快照')

    query = EngineeringArchitectureSnapshot.query.filter_by(
        baseline_id=baseline_id)
    scope = request.args.get('scope')
    if scope in ('full', 'module'):
        query = query.filter(EngineeringArchitectureSnapshot.scope == scope)
    target = request.args.get('target_module')
    if target:
        query = query.filter(
            EngineeringArchitectureSnapshot.target_module == target)
    items = query.order_by(
        desc(EngineeringArchitectureSnapshot.created_at)).all()
    # snapshot 自己也可能有独立共享/同项目可见性，这里保留全量返回
    # （因为已通过 baseline 鉴权，能看 baseline 就能看其下所有 snapshot）
    return jsonify({
        'snapshots': [s.to_dict(with_content=False) for s in items],
        'total': len(items),
        'lru_keep': ARCH_LRU_KEEP,
    })


@api_bp.route('/engineering/architecture/<int:snap_id>', methods=['GET'])
def get_arch_snapshot(snap_id):
    """单个架构快照详情（含完整 content_md + structured，供 marked + mermaid 渲染）"""
    s = EngineeringArchitectureSnapshot.query.get_or_404(snap_id)
    user = _get_current_user()
    if not _can_view('snapshot', s, user):
        return _forbidden('查看该架构快照')
    return jsonify(s.to_dict(with_content=True))


@api_bp.route('/engineering/baselines/<int:baseline_id>/architecture',
              methods=['POST'])
def create_arch_snapshot(baseline_id):
    """创建架构快照（Agent 推送 / Web 人工录入共用）。
    写入后立即触发 LRU 修剪（保留最新 10 条）。

    请求体：
    {
      "scope": "full" | "module",
      "target_module": "登录",          // scope=module 时必填
      "title": "v1.2.0 全工程总览",
      "summary": "...",
      "content_md": "# ...\n```mermaid\nflowchart LR\n...\n```",
      "structured": {modules:[], communications:[], key_logic:[], risks:[], suggestions:[]},
      "analyzed_commit": "abc1234",
      "source_type": "agent" | "manual" | "memos_import"
    }
    """
    baseline = EngineeringBaseline.query.get_or_404(baseline_id)
    user = _get_current_user()
    # 创建权限：能看该基线的人就能往里写快照（同项目成员均可贡献分析）
    if not _can_view('baseline', baseline, user):
        return _forbidden('在该基线下创建架构快照')
    data = request.get_json() or {}
    ok, err = _validate_arch_payload(data, partial=False)
    if not ok:
        return jsonify({'error': err}), 400

    snap = EngineeringArchitectureSnapshot(
        baseline_id=baseline_id,
        # 同步项目归属，避免后续鉴权再去 join baseline
        project_id=baseline.project_id,
        scope=data['scope'],
        target_module=(data.get('target_module') or '').strip(),
        title=(data.get('title') or '').strip()[:200],
        summary=data.get('summary') or '',
        content_md=data.get('content_md') or '',
        structured=data.get('structured') or {},
        analyzed_commit=(data.get('analyzed_commit') or '').strip()[:64],
        source_type=data.get('source_type') or 'agent',
        triggered_by=_operator(),
    )
    db.session.add(snap)
    db.session.flush()

    trimmed = _trim_arch_snapshots(baseline_id, ARCH_LRU_KEEP)
    db.session.commit()

    log_action(
        'create', 'engineering_arch_snapshot', snap.id,
        snap.title or f'arch#{snap.id}',
        operator=_operator(),
        detail=(f'scope={snap.scope} target={snap.target_module or "-"} '
                f'commit={snap.analyzed_commit or "-"} '
                f'md_bytes={len(snap.content_md or "")} '
                f'lru_trimmed={trimmed}'))
    return jsonify({
        **snap.to_dict(with_content=True),
        'lru_trimmed': trimmed,
        'lru_keep': ARCH_LRU_KEEP,
    }), 201


@api_bp.route('/engineering/architecture/<int:snap_id>', methods=['PUT'])
def update_arch_snapshot(snap_id):
    """更新架构快照。权限：作者本人 / super_admin。
    用途：AI 起草后人工微调内容、修正图表等。
    """
    snap = EngineeringArchitectureSnapshot.query.get_or_404(snap_id)
    user = _get_current_user()
    operator_name = _operator()

    if not _can_edit('snapshot', snap, user):
        return _forbidden('编辑该架构快照',
                          hint=f'triggered_by={snap.triggered_by} operator={operator_name}')

    data = request.get_json() or {}
    ok, err = _validate_arch_payload(data, partial=True)
    if not ok:
        return jsonify({'error': err}), 400

    # 允许更新的字段
    for f in ['title', 'summary', 'content_md', 'analyzed_commit']:
        if f in data:
            v = data[f]
            if f in ('title', 'analyzed_commit') and v is not None:
                v = str(v).strip()
                if f == 'title':
                    v = v[:200]
                else:
                    v = v[:64]
            setattr(snap, f, v)
    if 'structured' in data:
        snap.structured = data['structured'] or {}
    if 'target_module' in data and snap.scope == 'module':
        t = (data['target_module'] or '').strip()
        if not t:
            return jsonify({'error': 'scope=module 的快照 target_module 不能清空'}), 400
        snap.target_module = t
    if 'source_type' in data and data['source_type']:
        snap.source_type = data['source_type']

    db.session.commit()
    log_action('update', 'engineering_arch_snapshot', snap.id,
               snap.title or f'arch#{snap.id}', operator=operator_name,
               detail=f'scope={snap.scope} target={snap.target_module or "-"}')
    return jsonify(snap.to_dict(with_content=True))


@api_bp.route('/engineering/architecture/<int:snap_id>', methods=['DELETE'])
def delete_arch_snapshot(snap_id):
    """删除架构快照。权限：作者 / 项目管理员 / super_admin。"""
    snap = EngineeringArchitectureSnapshot.query.get_or_404(snap_id)
    user = _get_current_user()
    operator_name = _operator()

    if not _can_delete('snapshot', snap, user):
        return _forbidden('删除该架构快照',
                          hint=f'triggered_by={snap.triggered_by} operator={operator_name}')

    title = snap.title or f'arch#{snap_id}'
    scope = snap.scope
    target = snap.target_module
    db.session.delete(snap)
    db.session.commit()

    log_action('delete', 'engineering_arch_snapshot', snap_id, title,
               operator=operator_name,
               detail=f'硬删除：scope={scope} target={target or "-"}')
    return jsonify({
        'status': 'deleted',
        'soft': False,
        'snapshot_id': snap_id,
    })


# ==================== 影响项操作 ====================
#
# 影响项 (EngineeringTestImpactItem) 通过 batch_id 挂在批次下，没有自己的 project_id；
# 鉴权一律走"它所属批次"的可见/可编辑性。

def _impact_view_check(im, user):
    """impact 的查看权限 = 它所属 batch 的查看权限。"""
    batch = AnalysisRefreshBatch.query.get(im.batch_id) if im.batch_id else None
    return batch is not None and _can_view('batch', batch, user), batch


def _impact_edit_check(im, user):
    """impact 的编辑权限 = 它所属 batch 的编辑权限（作者 / super_admin）。"""
    batch = AnalysisRefreshBatch.query.get(im.batch_id) if im.batch_id else None
    return batch is not None and _can_edit('batch', batch, user), batch


@api_bp.route('/engineering/impacts/<int:impact_id>', methods=['GET'])
def get_impact(impact_id):
    im = EngineeringTestImpactItem.query.get_or_404(impact_id)
    user = _get_current_user()
    ok, _ = _impact_view_check(im, user)
    if not ok:
        return _forbidden('查看该影响项')
    return jsonify(im.to_dict(with_links=True))


@api_bp.route('/engineering/impacts/<int:impact_id>', methods=['PUT'])
def update_impact(impact_id):
    im = EngineeringTestImpactItem.query.get_or_404(impact_id)
    user = _get_current_user()
    ok, _ = _impact_edit_check(im, user)
    if not ok:
        return _forbidden('编辑该影响项')
    data = request.get_json() or {}
    for f in ['module_name', 'feature_chain', 'action_type', 'priority',
              'suggestion', 'acceptance_criteria', 'owner', 'status',
              'library_id', 'module_id']:
        if f in data:
            setattr(im, f, data[f])
    db.session.commit()
    log_action('update', 'engineering_impact', im.id,
               im.feature_chain or f'impact#{im.id}', operator=_operator())
    return jsonify(im.to_dict(with_links=True))


@api_bp.route('/engineering/impacts/<int:impact_id>/links', methods=['POST'])
def add_impact_case_link(impact_id):
    """关联一条 Hub 已有用例到该影响项。
    请求体：{"test_case_id": 123, "link_type": "affected"}
    权限：影响项可编辑（作者 / super_admin）。
    """
    im = EngineeringTestImpactItem.query.get_or_404(impact_id)
    user = _get_current_user()
    ok, _ = _impact_edit_check(im, user)
    if not ok:
        return _forbidden('给该影响项关联用例')
    data = request.get_json() or {}
    if _is_missing(data.get('test_case_id')):
        return jsonify({'error': 'test_case_id 为必填项'}), 400
    try:
        case_id = _to_int(data['test_case_id'], 'test_case_id')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if not TestCase.query.get(case_id):
        return jsonify({'error': '用例不存在'}), 404

    existing = EngineeringTestCaseLink.query.filter_by(
        impact_item_id=im.id, test_case_id=case_id).first()
    if existing:
        return jsonify(existing.to_dict()), 200

    link = EngineeringTestCaseLink(
        impact_item_id=im.id,
        test_case_id=case_id,
        link_type=data.get('link_type') or 'affected',
        created_by=_operator(),
    )
    db.session.add(link)
    db.session.commit()
    return jsonify(link.to_dict()), 201


@api_bp.route('/engineering/impacts/<int:impact_id>/links/<int:link_id>',
              methods=['DELETE'])
def delete_impact_case_link(impact_id, link_id):
    im = EngineeringTestImpactItem.query.get_or_404(impact_id)
    user = _get_current_user()
    ok, _ = _impact_edit_check(im, user)
    if not ok:
        return _forbidden('解绑该影响项的用例关联')
    link = EngineeringTestCaseLink.query.filter_by(
        id=link_id, impact_item_id=impact_id).first_or_404()
    db.session.delete(link)
    db.session.commit()
    return jsonify({'status': 'deleted'})


@api_bp.route('/engineering/impacts/<int:impact_id>/create-task',
              methods=['POST'])
def create_test_task_from_impact(impact_id):
    """一键把影响项写入 Hub 的测试任务。

    要求该 impact 所在的 batch 已绑定 iteration_id。
    操作：
      1. 在该 iteration 下找/建一个 plan（默认沿用 batch 标识，或调用方传 plan_id）
      2. 在该 plan 下新建一个 test_task，并把所有 linked_cases 加入 test_task_cases
    请求体（全部可选）：
    {
        "plan_id": 12,                        // 不传则自动找/建
        "task_name": "登录-断线重连回归",       // 不传则自动生成
        "task_type": "regression",
        "assignee_claw_id": 5
    }
    """
    im = EngineeringTestImpactItem.query.get_or_404(impact_id)
    user = _get_current_user()
    ok, batch = _impact_edit_check(im, user)
    if not ok:
        return _forbidden('从该影响项创建测试任务')
    if batch is None:
        batch = AnalysisRefreshBatch.query.get_or_404(im.batch_id)
    if not batch.iteration_id:
        return jsonify({
            'error': '该批次未绑定测试迭代（iteration_id），无法写入测试任务'
        }), 400

    iteration = TestIteration.query.get_or_404(batch.iteration_id)
    data = request.get_json() or {}

    plan_id = data.get('plan_id')
    if plan_id:
        plan = TestPlan.query.filter_by(
            id=plan_id, iteration_id=iteration.id).first()
        if not plan:
            return jsonify({'error': 'plan_id 不存在或不属于该迭代'}), 404
    else:
        plan_name = f'工程分析回归 - {batch.from_commit or ""}..{batch.to_commit or ""}'
        plan = TestPlan.query.filter_by(
            iteration_id=iteration.id, name=plan_name).first()
        if not plan:
            from datetime import date as _date
            plan = TestPlan(
                iteration_id=iteration.id,
                project_id=batch.project_id,
                name=plan_name,
                description=f'由工程分析批次 #{batch.id} 自动创建',
                # TestPlan.start/end_date 为 NOT NULL，缺失时兜底为今天
                start_date=iteration.start_date or _date.today(),
                end_date=iteration.end_date or _date.today(),
                status='active',
                created_by=_operator(),
            )
            db.session.add(plan)
            db.session.flush()

    task_name = (data.get('task_name')
                 or f'[{im.priority}] {im.feature_chain or "影响项 #" + str(im.id)}')
    task = TestTask(
        plan_id=plan.id,
        name=task_name,
        description=im.suggestion or '',
        task_type=data.get('task_type') or 'regression',
        priority=im.priority or 'P2',
        library_id=im.library_id,
        assignee_claw_id=data.get('assignee_claw_id'),
        status='pending',
        created_by=_operator(),
    )
    db.session.add(task)
    db.session.flush()

    case_count = 0
    for link in im.case_links:
        existing = TestTaskCase.query.filter_by(
            task_id=task.id, case_id=link.test_case_id).first()
        if existing:
            continue
        db.session.add(TestTaskCase(
            task_id=task.id, case_id=link.test_case_id, status='pending'))
        case_count += 1
    task.total_cases = case_count

    im.linked_test_task_id = task.id
    if im.status == 'todo':
        im.status = 'in_progress'

    db.session.commit()
    log_action('create', 'test_task', task.id, task.name,
               operator=_operator(),
               detail=f'由工程分析 impact#{im.id} 创建，绑定用例 {case_count}')
    return jsonify({
        'plan': plan.to_dict() if hasattr(plan, 'to_dict') else {'id': plan.id},
        'task': task.to_dict() if hasattr(task, 'to_dict') else {'id': task.id},
        'impact': im.to_dict(with_links=True),
        'linked_case_count': case_count,
    }), 201


# ==================== 辅助查询 ====================

@api_bp.route('/engineering/lookups/projects', methods=['GET'])
def lookup_projects():
    """供前端下拉框用：返回项目列表（去重）"""
    projects = Project.query.order_by(Project.name).all()
    return jsonify({
        'projects': [{'id': p.id, 'name': p.name} for p in projects],
    })


@api_bp.route('/engineering/lookups/libraries', methods=['GET'])
def lookup_libraries():
    """用例库列表。TestCaseLibrary 用 project_name(str) 关联项目，故按名字过滤。"""
    project_id = request.args.get('project_id', type=int)
    query = TestCaseLibrary.query
    if project_id is not None:
        proj = Project.query.get(project_id)
        if proj:
            query = query.filter(TestCaseLibrary.project_name == proj.name)
    libs = query.order_by(TestCaseLibrary.name).all()
    return jsonify({
        'libraries': [{
            'id': l.id, 'name': l.name,
            'project_name': l.project_name,
            'module_name': l.module_name,
        } for l in libs],
    })


@api_bp.route('/engineering/lookups/iterations', methods=['GET'])
def lookup_iterations():
    project_id = request.args.get('project_id', type=int)
    query = TestIteration.query
    if project_id is not None:
        query = query.filter(TestIteration.project_id == project_id)
    iters = query.order_by(desc(TestIteration.created_at)).all()
    return jsonify({
        'iterations': [{
            'id': i.id, 'name': i.name,
            'status': i.status,
            'project_id': i.project_id,
        } for i in iters],
    })


@api_bp.route('/engineering/lookups/modules', methods=['GET'])
def lookup_modules():
    project_id = request.args.get('project_id', type=int)
    query = Module.query
    if project_id is not None:
        query = query.filter(Module.project_id == project_id)
    items = query.order_by(Module.name).all()
    return jsonify({
        'modules': [{
            'id': m.id, 'name': m.name,
            'project_id': m.project_id,
        } for m in items],
    })


# ==================== 共享授权（跨项目临时开放）====================
#
# 使用场景：
#   - 作者 / 项目管理员 / super_admin 想把某条分析结果（baseline/batch/snapshot）
#     临时开放给：指定用户（其名下所有 OpenClaw 自动继承）/ 指定 OpenClaw / 全部登录用户
#   - 讨论结束后撤销即可关闭
#
# 资源类型枚举：baseline / batch / snapshot

_RESOURCE_TYPES = ('baseline', 'batch', 'snapshot')


def _parse_resource_or_404(resource_type, resource_id):
    if resource_type not in _RESOURCE_TYPES:
        return None, (jsonify({
            'error': f'非法 resource_type: {resource_type}（合法值：{_RESOURCE_TYPES}）'
        }), 400)
    obj = _resource_loader(resource_type, resource_id)
    if obj is None:
        return None, (jsonify({'error': '资源不存在'}), 404)
    return obj, None


def _parse_expires_at(raw):
    """允许字符串 'YYYY-MM-DD HH:MM:SS' 或 ISO8601；返回 datetime 或 None；非法抛 ValueError。"""
    if raw in (None, '', 'null'):
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f'expires_at 格式不识别: {raw}')


@api_bp.route('/engineering/<string:resource_type>/<int:resource_id>/shares',
              methods=['GET'])
def list_resource_shares(resource_type, resource_id):
    """列出该资源当前所有共享授权（含已过期，前端自行展示状态）。"""
    obj, err = _parse_resource_or_404(resource_type, resource_id)
    if err:
        return err
    user = _get_current_user()
    # 能看就能看授权列表（便于跨项目协作时知道"我被开放了"）
    if not _can_view(resource_type, obj, user):
        return _forbidden('查看该资源的共享授权')

    shares = (EngineeringShare.query
              .filter_by(resource_type=resource_type, resource_id=resource_id)
              .order_by(desc(EngineeringShare.created_at)).all())
    return jsonify({
        'resource_type': resource_type,
        'resource_id': resource_id,
        'shares': [s.to_dict() for s in shares],
        'total': len(shares),
        'can_share': _can_share(resource_type, obj, user),
    })


@api_bp.route('/engineering/<string:resource_type>/<int:resource_id>/shares',
              methods=['POST'])
def create_resource_share(resource_type, resource_id):
    """新增共享授权。
    请求体：
    {
      "share_type": "user" | "claw" | "public",
      "target_user_id":   123,    // share_type=user 时必填（互斥）
      "target_claw_id":   45,     // share_type=claw 时必填（互斥）
      "expires_at": "2026-04-30 18:00:00",  // 可选；不填=永久（手动撤销）
      "note": "课题 #45 临时讨论"            // 可选
    }
    权限：作者 / 项目管理员 / super_admin。
    幂等：相同 (resource, share_type, target) 已存在且仍有效 → 返回旧记录 200。
    """
    obj, err = _parse_resource_or_404(resource_type, resource_id)
    if err:
        return err
    user = _get_current_user()
    if not _can_share(resource_type, obj, user):
        return _forbidden('共享该资源')

    data = request.get_json() or {}
    share_type = data.get('share_type')
    if share_type not in ('user', 'claw', 'public'):
        return jsonify({'error': 'share_type 必须是 user/claw/public'}), 400

    target_user_id = data.get('target_user_id')
    target_claw_id = data.get('target_claw_id')
    if share_type == 'user':
        if _is_missing(target_user_id):
            return jsonify({'error': 'share_type=user 时 target_user_id 必填'}), 400
        if not User.query.get(target_user_id):
            return jsonify({'error': '目标用户不存在'}), 404
        target_claw_id = None
    elif share_type == 'claw':
        if _is_missing(target_claw_id):
            return jsonify({'error': 'share_type=claw 时 target_claw_id 必填'}), 400
        if not OpenClawInstance.query.get(target_claw_id):
            return jsonify({'error': '目标 OpenClaw 不存在'}), 404
        target_user_id = None
    else:  # public
        target_user_id = None
        target_claw_id = None

    try:
        expires_at = _parse_expires_at(data.get('expires_at'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # 幂等：相同维度已存在且未过期 → 直接返回
    existing = (EngineeringShare.query
                .filter_by(resource_type=resource_type, resource_id=resource_id,
                           share_type=share_type,
                           target_user_id=target_user_id,
                           target_claw_id=target_claw_id)
                .all())
    for ex in existing:
        if ex.is_active():
            # 如果带新的 expires_at / note，刷新一下
            updated = False
            if expires_at and ex.expires_at != expires_at:
                ex.expires_at = expires_at
                updated = True
            note = (data.get('note') or '').strip()
            if note and ex.note != note:
                ex.note = note
                updated = True
            if updated:
                db.session.commit()
            return jsonify({**ex.to_dict(), 'idempotent': True}), 200

    share = EngineeringShare(
        resource_type=resource_type,
        resource_id=resource_id,
        share_type=share_type,
        target_user_id=target_user_id,
        target_claw_id=target_claw_id,
        granted_by=(getattr(user, 'username', None)
                    or getattr(user, '_claw_name', None) or 'system'),
        note=(data.get('note') or '').strip()[:500],
        expires_at=expires_at,
    )
    db.session.add(share)
    db.session.commit()

    log_action('share_grant', f'engineering_{resource_type}', resource_id,
               f'{resource_type}#{resource_id}', operator=_operator(),
               detail=(f'share_type={share_type} '
                       f'target_user={target_user_id or "-"} '
                       f'target_claw={target_claw_id or "-"} '
                       f'expires_at={expires_at or "永久"} '
                       f'note={(data.get("note") or "")[:80]}'))
    return jsonify(share.to_dict()), 201


@api_bp.route('/engineering/shares/<int:share_id>', methods=['DELETE'])
def revoke_resource_share(share_id):
    """撤销共享授权（"讨论完关闭"）。权限：原资源的可共享方。"""
    share = EngineeringShare.query.get_or_404(share_id)
    obj = _resource_loader(share.resource_type, share.resource_id)
    user = _get_current_user()
    if obj is None or not _can_share(share.resource_type, obj, user):
        return _forbidden('撤销该共享授权')

    rt = share.resource_type
    rid = share.resource_id
    info = (f'share_type={share.share_type} '
            f'target_user={share.target_user_id or "-"} '
            f'target_claw={share.target_claw_id or "-"}')
    db.session.delete(share)
    db.session.commit()

    log_action('share_revoke', f'engineering_{rt}', rid,
               f'{rt}#{rid}', operator=_operator(), detail=info)
    return jsonify({'status': 'revoked', 'share_id': share_id})


@api_bp.route('/engineering/<string:resource_type>/<int:resource_id>/shares/public',
              methods=['POST'])
def toggle_public_share_on(resource_type, resource_id):
    """一键全部开放（语义：所有登录用户/claw 可见）。幂等。
    可选 body: {"expires_at": "...", "note": "..."}
    """
    obj, err = _parse_resource_or_404(resource_type, resource_id)
    if err:
        return err
    user = _get_current_user()
    if not _can_share(resource_type, obj, user):
        return _forbidden('开放该资源')

    data = request.get_json() or {}
    try:
        expires_at = _parse_expires_at(data.get('expires_at'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    existing = (EngineeringShare.query
                .filter_by(resource_type=resource_type, resource_id=resource_id,
                           share_type='public',
                           target_user_id=None, target_claw_id=None)
                .first())
    if existing and existing.is_active():
        if expires_at and existing.expires_at != expires_at:
            existing.expires_at = expires_at
            db.session.commit()
        return jsonify({**existing.to_dict(), 'idempotent': True}), 200

    if existing:  # 已过期 → 复用记录刷新
        existing.expires_at = expires_at
        existing.granted_by = (getattr(user, 'username', None)
                               or getattr(user, '_claw_name', None) or 'system')
        existing.note = (data.get('note') or '').strip()[:500]
        db.session.commit()
        log_action('share_grant', f'engineering_{resource_type}', resource_id,
                   f'{resource_type}#{resource_id}', operator=_operator(),
                   detail=f'public 开放（复用过期记录）expires_at={expires_at or "永久"}')
        return jsonify(existing.to_dict()), 200

    share = EngineeringShare(
        resource_type=resource_type,
        resource_id=resource_id,
        share_type='public',
        granted_by=(getattr(user, 'username', None)
                    or getattr(user, '_claw_name', None) or 'system'),
        note=(data.get('note') or '').strip()[:500],
        expires_at=expires_at,
    )
    db.session.add(share)
    db.session.commit()
    log_action('share_grant', f'engineering_{resource_type}', resource_id,
               f'{resource_type}#{resource_id}', operator=_operator(),
               detail=f'public 开放 expires_at={expires_at or "永久"}')
    return jsonify(share.to_dict()), 201


@api_bp.route('/engineering/<string:resource_type>/<int:resource_id>/shares/public',
              methods=['DELETE'])
def toggle_public_share_off(resource_type, resource_id):
    """一键关闭全部开放（删除该资源所有 share_type=public 记录）。"""
    obj, err = _parse_resource_or_404(resource_type, resource_id)
    if err:
        return err
    user = _get_current_user()
    if not _can_share(resource_type, obj, user):
        return _forbidden('关闭该资源的全部开放')

    deleted = (EngineeringShare.query
               .filter_by(resource_type=resource_type, resource_id=resource_id,
                          share_type='public')
               .delete(synchronize_session=False))
    db.session.commit()
    log_action('share_revoke', f'engineering_{resource_type}', resource_id,
               f'{resource_type}#{resource_id}', operator=_operator(),
               detail=f'关闭 public 开放 deleted={deleted}')
    return jsonify({'status': 'closed', 'deleted': deleted})


# ==================== Resolve（贴 URL 取数据）====================
#
# OpenClaw 之间或人 → claw 之间引用分析结果时的反向解析助手：
#   贴一个 web 链接（http://hub:18800/engineering/architecture/123）
#   返回 { type, id, title, summary, project_id, viewable, api_endpoint, ...}
# 让接收方完全免去 URL 解析。

# 支持的 deep link 路径模板：
#   /engineering/baselines/{id}            → baseline
#   /engineering/refresh/{id}              → batch
#   /engineering/architecture/{id}         → snapshot
#   /api/v1/engineering/baselines/{id}     → baseline   （API 形式也认）
#   /api/v1/engineering/refresh/{id}       → batch
#   /api/v1/engineering/architecture/{id}  → snapshot
_DEEP_LINK_PATTERNS = [
    (re.compile(r'/engineering/baselines/(\d+)'), 'baseline'),
    (re.compile(r'/engineering/refresh/(\d+)'), 'batch'),
    (re.compile(r'/engineering/architecture/(\d+)'), 'snapshot'),
]


def _resolve_url_to_resource(raw_url):
    """从 URL（或纯路径）中识别出 resource_type 与 id。
    返回 (resource_type, resource_id) 或 (None, None)。
    """
    if not raw_url:
        return None, None
    path = raw_url.strip()
    try:
        parsed = urlparse(path)
        if parsed.path:
            path = parsed.path
            if parsed.fragment:
                # 兼容 /engineering/refresh/123#impact-45
                path = path + '#' + parsed.fragment
    except Exception:
        pass

    for pat, rtype in _DEEP_LINK_PATTERNS:
        m = pat.search(path)
        if m:
            try:
                return rtype, int(m.group(1))
            except ValueError:
                return None, None
    return None, None


@api_bp.route('/engineering/resolve', methods=['GET'])
def resolve_engineering_url():
    """通用反向解析：贴一个工程分析 URL 即可拿到结构化引用信息。

    Query: ?url=http://hub:18800/engineering/architecture/123
           或 ?url=/engineering/refresh/45

    返回：
    {
      "type": "snapshot",
      "id": 123,
      "title": "...",
      "summary": "...",
      "project_id": 5,
      "viewable": true,                  # 当前调用方是否有权查看
      "share_required": false,           # viewable=false 时为 true，提示需要共享
      "web_url":      "/engineering/architecture/123",
      "api_endpoint": "/api/v1/engineering/architecture/123"
    }
    """
    raw = request.args.get('url') or ''
    rtype, rid = _resolve_url_to_resource(raw)
    if not rtype:
        return jsonify({
            'error': '无法识别工程分析链接',
            'hint': '支持 /engineering/{baselines|refresh|architecture}/<id>',
            'input': raw,
        }), 400

    obj = _resource_loader(rtype, rid)
    if obj is None:
        return jsonify({'error': f'{rtype}#{rid} 不存在', 'type': rtype, 'id': rid}), 404

    user = _get_current_user()
    viewable = _can_view(rtype, obj, user)

    payload = {
        'type': rtype,
        'id': rid,
        'project_id': _resource_project_id(rtype, obj),
        'viewable': viewable,
        'share_required': not viewable,
    }

    if rtype == 'baseline':
        payload.update({
            'title': obj.name,
            'summary': obj.repo_url,
            'web_url': f'/engineering/baselines/{rid}',
            'api_endpoint': f'/api/v1/engineering/baselines/{rid}',
        })
    elif rtype == 'batch':
        payload.update({
            'title': f'刷新批次 #{rid}',
            'summary': (obj.summary or '')[:200],
            'risk_level': obj.risk_level,
            'status': obj.status,
            'baseline_id': obj.baseline_id,
            'web_url': f'/engineering/refresh/{rid}',
            'api_endpoint': f'/api/v1/engineering/refresh/{rid}',
        })
    elif rtype == 'snapshot':
        payload.update({
            'title': obj.title or f'架构快照 #{rid}',
            'summary': obj.summary or '',
            'scope': obj.scope,
            'target_module': obj.target_module or '',
            'baseline_id': obj.baseline_id,
            'web_url': f'/engineering/architecture/{rid}',
            'api_endpoint': f'/api/v1/engineering/architecture/{rid}',
        })

    if not viewable:
        payload['hint'] = '当前账号/OpenClaw 无权查看；请联系作者或项目管理员通过 POST /engineering/{type}/{id}/shares 授权'
    return jsonify(payload)
