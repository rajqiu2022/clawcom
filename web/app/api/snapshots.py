"""
用例库版本快照 API — 类 git 的版本管理

核心概念：
- commit: 创建快照（全量保存当前所有用例）
- log: 查看版本历史
- show: 查看某个版本的完整内容
- checkout: 回滚到某个版本
- diff: 对比两个版本的差异

快照存储的是全量 JSON，适合用例数据量（几十~几百条）。
"""

import json
from datetime import datetime
from flask import request, jsonify
from app import db
from app.models import TestCaseLibrary, TestCase, TestCaseSnapshot
from app.api import api_bp


def _snapshot_cases(library):
    """将用例库当前所有用例序列化为 JSON 字符串"""
    cases = library.cases.order_by(TestCase.case_id).all()
    data = []
    for c in cases:
        data.append({
            'case_id': c.case_id,
            'title': c.title,
            'priority': c.priority,
            'type': c.type,
            'content': c.content,
            'tags': c.tags or [],
            'ai_generated': c.ai_generated,
            'mindmap_node_id': c.mindmap_node_id,
        })
    return json.dumps(data, ensure_ascii=False), len(data)


def _next_version(library_id):
    """获取下一个版本号"""
    last = TestCaseSnapshot.query.filter_by(library_id=library_id) \
        .order_by(TestCaseSnapshot.version.desc()).first()
    return (last.version + 1) if last else 1


def _compute_diff(old_json, new_json):
    """计算两个版本间的差异摘要"""
    try:
        old = json.loads(old_json) if old_json else []
        new = json.loads(new_json) if new_json else []
    except (json.JSONDecodeError, TypeError):
        return json.dumps({'error': 'parse failed'})

    old_map = {c['case_id']: c for c in old if c.get('case_id')}
    new_map = {c['case_id']: c for c in new if c.get('case_id')}

    added = [cid for cid in new_map if cid not in old_map]
    removed = [cid for cid in old_map if cid not in new_map]
    modified = []
    for cid in new_map:
        if cid in old_map and new_map[cid] != old_map[cid]:
            # 找出具体改了什么字段
            changes = []
            for key in ('title', 'priority', 'type', 'content', 'tags'):
                if new_map[cid].get(key) != old_map[cid].get(key):
                    changes.append(key)
            if changes:
                modified.append({'case_id': cid, 'title': new_map[cid].get('title', ''), 'fields': changes})

    return json.dumps({
        'added': added,
        'removed': removed,
        'modified': modified,
        'added_count': len(added),
        'removed_count': len(removed),
        'modified_count': len(modified),
    }, ensure_ascii=False)


# ==================== commit — 创建快照 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/snapshots', methods=['POST'])
def create_snapshot(library_id):
    """创建版本快照（类似 git commit）

    请求体：
    {
        "message": "提交说明",
        "tag": "v1.0",                    // 可选标签
        "snapshot_type": "manual",         // manual/auto/ai/rollback
        "created_by": "操作者"
    }
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    data = request.get_json() or {}

    cases_json, case_count = _snapshot_cases(library)
    version = _next_version(library_id)

    # 获取上一版本用于 diff
    prev = TestCaseSnapshot.query.filter_by(library_id=library_id) \
        .order_by(TestCaseSnapshot.version.desc()).first()
    diff_summary = _compute_diff(prev.cases_data if prev else '[]', cases_json)

    snapshot = TestCaseSnapshot(
        library_id=library_id,
        version=version,
        tag=data.get('tag'),
        message=data.get('message', f'版本 {version}'),
        snapshot_type=data.get('snapshot_type', 'manual'),
        case_count=case_count,
        cases_data=cases_json,
        diff_summary=diff_summary,
        created_by=data.get('created_by', 'system'),
    )
    db.session.add(snapshot)
    db.session.commit()

    result = snapshot.to_dict()
    result['diff'] = json.loads(diff_summary)
    return jsonify(result), 201


# ==================== log — 查看版本历史 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/snapshots', methods=['GET'])
def list_snapshots(library_id):
    """查看版本历史（类似 git log）"""
    TestCaseLibrary.query.get_or_404(library_id)

    limit = request.args.get('limit', 50, type=int)
    snapshots = TestCaseSnapshot.query.filter_by(library_id=library_id) \
        .order_by(TestCaseSnapshot.version.desc()) \
        .limit(limit).all()

    result = []
    for s in snapshots:
        d = s.to_dict()
        try:
            d['diff'] = json.loads(s.diff_summary) if s.diff_summary else None
        except Exception:
            d['diff'] = None
        result.append(d)
    return jsonify(result)


# ==================== show — 查看某个版本 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/snapshots/<int:version>', methods=['GET'])
def get_snapshot(library_id, version):
    """查看某个版本的完整内容（类似 git show）"""
    snapshot = TestCaseSnapshot.query.filter_by(
        library_id=library_id, version=version
    ).first_or_404()

    return jsonify(snapshot.to_dict(with_data=True))


# ==================== checkout — 回滚到某个版本 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/snapshots/<int:version>/checkout', methods=['POST'])
def checkout_snapshot(library_id, version):
    """回滚到某个版本（类似 git checkout）

    流程：
    1. 先自动创建当前状态的快照（防丢失）
    2. 删除当前所有用例
    3. 从目标版本的快照数据重建所有用例
    4. 创建一个 rollback 类型的快照记录
    """
    library = TestCaseLibrary.query.get_or_404(library_id)
    target = TestCaseSnapshot.query.filter_by(
        library_id=library_id, version=version
    ).first_or_404()

    data = request.get_json() or {}

    # 1. 先保存当前状态（安全网）
    current_json, current_count = _snapshot_cases(library)
    save_version = _next_version(library_id)
    prev = TestCaseSnapshot.query.filter_by(library_id=library_id) \
        .order_by(TestCaseSnapshot.version.desc()).first()

    save_snapshot = TestCaseSnapshot(
        library_id=library_id,
        version=save_version,
        message=f'回滚前自动保存（当前状态）',
        snapshot_type='auto',
        case_count=current_count,
        cases_data=current_json,
        diff_summary=_compute_diff(prev.cases_data if prev else '[]', current_json),
        created_by=data.get('created_by', 'system'),
    )
    db.session.add(save_snapshot)

    # 2. 清空当前用例
    TestCase.query.filter_by(library_id=library_id).delete()

    # 3. 从快照重建
    try:
        cases_data = json.loads(target.cases_data) if target.cases_data else []
    except (json.JSONDecodeError, TypeError):
        cases_data = []

    import uuid
    for cd in cases_data:
        case = TestCase(
            library_id=library_id,
            case_id=cd.get('case_id'),
            title=cd.get('title', '未命名'),
            priority=cd.get('priority', 'P2'),
            type=cd.get('type', 'functional'),
            content=cd.get('content'),
            tags=cd.get('tags', []),
            ai_generated=cd.get('ai_generated', False),
            mindmap_node_id=cd.get('mindmap_node_id', f'node_{uuid.uuid4().hex[:8]}'),
        )
        db.session.add(case)

    # 4. 创建 rollback 快照记录
    rollback_version = save_version + 1
    rollback_json, rollback_count = json.dumps(cases_data, ensure_ascii=False), len(cases_data)
    rollback_snapshot = TestCaseSnapshot(
        library_id=library_id,
        version=rollback_version,
        tag=target.tag,
        message=data.get('message', f'回滚到 v{version}: {target.message or ""}'),
        snapshot_type='rollback',
        case_count=rollback_count,
        cases_data=rollback_json,
        diff_summary=_compute_diff(current_json, rollback_json),
        created_by=data.get('created_by', 'system'),
    )
    db.session.add(rollback_snapshot)
    db.session.commit()

    return jsonify({
        'message': f'已回滚到 v{version}',
        'restored_cases': rollback_count,
        'saved_version': save_version,
        'new_version': rollback_version,
    })


# ==================== diff — 对比两个版本 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/snapshots/diff', methods=['GET'])
def diff_snapshots(library_id):
    """对比两个版本（类似 git diff v1..v2）

    参数：?from=1&to=3
    """
    TestCaseLibrary.query.get_or_404(library_id)

    from_v = request.args.get('from', type=int)
    to_v = request.args.get('to', type=int)

    if not from_v or not to_v:
        return jsonify({'error': '需要 from 和 to 版本号参数'}), 400

    snap_from = TestCaseSnapshot.query.filter_by(
        library_id=library_id, version=from_v
    ).first_or_404()
    snap_to = TestCaseSnapshot.query.filter_by(
        library_id=library_id, version=to_v
    ).first_or_404()

    diff = json.loads(_compute_diff(snap_from.cases_data, snap_to.cases_data))
    diff['from_version'] = from_v
    diff['to_version'] = to_v
    diff['from_case_count'] = snap_from.case_count
    diff['to_case_count'] = snap_to.case_count

    return jsonify(diff)


# ==================== 标签管理 ====================

@api_bp.route('/testcase-libraries/<int:library_id>/snapshots/<int:version>/tag', methods=['PUT'])
def tag_snapshot(library_id, version):
    """给版本打标签（类似 git tag）"""
    snapshot = TestCaseSnapshot.query.filter_by(
        library_id=library_id, version=version
    ).first_or_404()

    data = request.get_json() or {}
    snapshot.tag = data.get('tag', '')
    db.session.commit()
    return jsonify(snapshot.to_dict())


# ==================== 便捷函数（供其他模块调用） ====================

def auto_snapshot(library_id, message, snapshot_type='auto', created_by='system'):
    """自动创建快照（供 testcases.py / ai_generator.py 调用）

    用法：
        from app.api.snapshots import auto_snapshot
        auto_snapshot(library_id, '批量删除前', 'auto', 'admin')
    """
    library = TestCaseLibrary.query.get(library_id)
    if not library:
        return None

    cases_json, case_count = _snapshot_cases(library)
    version = _next_version(library_id)

    prev = TestCaseSnapshot.query.filter_by(library_id=library_id) \
        .order_by(TestCaseSnapshot.version.desc()).first()
    diff_summary = _compute_diff(prev.cases_data if prev else '[]', cases_json)

    snapshot = TestCaseSnapshot(
        library_id=library_id,
        version=version,
        message=message,
        snapshot_type=snapshot_type,
        case_count=case_count,
        cases_data=cases_json,
        diff_summary=diff_summary,
        created_by=created_by,
    )
    db.session.add(snapshot)
    # 注意：不 commit，让调用方统一 commit
    return snapshot
