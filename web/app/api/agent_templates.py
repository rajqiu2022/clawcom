from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

from flask import current_app, jsonify, request, send_file, session
from werkzeug.utils import secure_filename

from app import db
from app.api import api_bp
from app.api.auth import _require_super_admin
from app.models import (
    AgentRoleTemplate,
    AgentRoleTemplateFile,
    AgentRoleTemplateVersion,
    ClawMessage,
    ClawTodo,
    OpenClawInstance,
    OpenClawRule,
    OpenClawSkill,
    Rule,
    Skill,
    User,
    _now,
)

_REF_RE = re.compile(r"\{\{file:([^}]+)\}\}")
_MAX_UPLOAD_SIZE = 20 * 1024 * 1024
_REFERENCE_FIELDS = ('main_responsibility', 'skills_summary', 'rules_summary', 'schedules_summary')


def _normalize_template_key(raw: str) -> str:
    key = (raw or '').strip().lower()
    key = re.sub(r'[^a-z0-9\-_]+', '-', key).strip('-')
    if not key:
        raise ValueError('template_key 不能为空')
    if len(key) > 80:
        raise ValueError('template_key 最长 80 字符')
    return key


def _normalize_relpath(raw: str) -> str:
    rel = (raw or '').strip().replace('\\', '/')
    rel = rel.lstrip('/')
    if not rel:
        raise ValueError('relative_path 不能为空')
    if '..' in rel.split('/'):
        raise ValueError('relative_path 不允许包含 ..')
    if len(rel) > 500:
        raise ValueError('relative_path 过长')
    return rel


def _storage_root() -> str:
    root = current_app.config.get('AGENT_TEMPLATE_STORAGE_ROOT') or ''
    if not root:
        raise RuntimeError('AGENT_TEMPLATE_STORAGE_ROOT 未配置')
    os.makedirs(root, exist_ok=True)
    return root


def _template_dir(template: AgentRoleTemplate) -> str:
    return os.path.join(_storage_root(), template.template_key)


def _resolve_references(text: str, files: list[AgentRoleTemplateFile]) -> list[dict]:
    if not text:
        return []
    file_map = {f.relative_path: f for f in files}
    refs = []
    for match in _REF_RE.findall(text):
        rel = match.strip()
        item = file_map.get(rel)
        refs.append({
            'reference': rel,
            'exists': bool(item),
            'file_id': item.id if item else None,
            'file_name': item.file_name if item else '',
        })
    return refs


def _template_snapshot(template: AgentRoleTemplate) -> dict:
    return {
        'name': template.name or '',
        'template_key': template.template_key or '',
        'profile_name': template.profile_name or '',
        'role_name': template.role_name or '',
        'main_responsibility': template.main_responsibility or '',
        'skills_summary': template.skills_summary or '',
        'rules_summary': template.rules_summary or '',
        'schedules_summary': template.schedules_summary or '',
        'installed_skill_ids': template.installed_skill_ids or [],
        'rule_ids': template.rule_ids or [],
        'schedule_items': template.schedule_items or [],
        'status': template.status or 'draft',
        'review_comment': template.review_comment or '',
        'owner_claw_id': template.owner_claw_id,
    }


def _save_template_version(template: AgentRoleTemplate, action: str, change_note: str,
                           created_by: str, version_no: int | None = None) -> AgentRoleTemplateVersion:
    if version_no is None:
        version_no = int(template.current_version or 1)
    row = AgentRoleTemplateVersion(
        template_id=template.id,
        version_no=version_no,
        action=action,
        change_note=(change_note or '').strip(),
        snapshot_payload=json.dumps(_template_snapshot(template), ensure_ascii=False),
        created_by=created_by or 'system',
    )
    db.session.add(row)
    return row


def _parse_snapshot(version: AgentRoleTemplateVersion) -> dict:
    try:
        payload = json.loads(version.snapshot_payload or '{}')
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _collect_reference_map(template: AgentRoleTemplate, files: list[AgentRoleTemplateFile]) -> list[dict]:
    file_map = {f.relative_path: f for f in files}
    refs = []
    for field in _REFERENCE_FIELDS:
        text = (getattr(template, field, None) or '').strip()
        for match in _REF_RE.findall(text):
            rel = match.strip()
            item = file_map.get(rel)
            refs.append({
                'field': field,
                'reference': rel,
                'exists': bool(item),
                'file_id': item.id if item else None,
                'file_name': item.file_name if item else '',
            })
    return refs


def _validate_template_references(template: AgentRoleTemplate) -> dict:
    files = AgentRoleTemplateFile.query.filter_by(template_id=template.id).all()
    refs = _collect_reference_map(template, files)
    missing = [r for r in refs if not r['exists']]
    seen = {}
    duplicate_refs = []
    for r in refs:
        key = (r['field'], r['reference'])
        seen[key] = seen.get(key, 0) + 1
    for (field, reference), count in seen.items():
        if count > 1:
            duplicate_refs.append({'field': field, 'reference': reference, 'count': count})
    return {
        'valid': len(missing) == 0,
        'total_references': len(refs),
        'missing_count': len(missing),
        'duplicate_count': len(duplicate_refs),
        'references': refs,
        'missing_references': missing,
        'duplicate_references': duplicate_refs,
    }


def _next_template_version(template: AgentRoleTemplate) -> int:
    current = int(template.current_version or 0)
    return current + 1 if current > 0 else 1


def _normalize_schedule_item(item: dict, default_key: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = (item.get('title') or '').strip()
    if not title:
        return None
    return {
        'title': title,
        'description': (item.get('description') or '').strip(),
        'schedule_type': (item.get('schedule_type') or 'daily').strip(),
        'schedule_time': (item.get('schedule_time') or '').strip() or None,
        'schedule_day': item.get('schedule_day'),
        'urgency_level': (item.get('urgency_level') or 'flexible').strip(),
        'priority': (item.get('priority') or 'P1').strip(),
        'task_category': (item.get('task_category') or 'routine').strip(),
        'retry_delay': int(item.get('retry_delay') or 5),
        'retry_max': int(item.get('retry_max') or 1),
        'verification_target': (item.get('verification_target') or default_key).strip(),
    }


def _get_agent_from_bearer():
    from app.api.auth_utils import get_current_claw
    return get_current_claw()


def _operator_name(default='system'):
    uid = session.get('user_id')
    if not uid:
        return default
    user = User.query.get(uid)
    if not user:
        return default
    return user.username or user.display_name or default


def _require_template_admin():
    """模板管理权限：Web super_admin/admin，或 Bearer 的 admin/test_manager OpenClaw。
    返回 (actor, actor_type, err)。actor_type = 'user' | 'claw'。
    """
    uid = session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user and user.role in ('super_admin', 'admin'):
            return user, 'user', None
        return None, None, (jsonify({'error': '需要管理员权限'}), 403)

    claw = _get_agent_from_bearer()
    if claw and claw.role in ('admin', 'test_manager'):
        return claw, 'claw', None
    return None, None, (jsonify({'error': '需要管理员权限或 Agent Token'}), 403)


def _require_template_file_operator():
    """模板文件权限：Web 管理员，或 Bearer 的 admin/test_manager OpenClaw。"""
    return _require_template_admin()


def _check_template_file_access(template: AgentRoleTemplate, actor, actor_type: str):
    if actor_type == 'user':
        return None
    if actor_type == 'claw':
        # 非 Web 管理员通过 Bearer 操作文件时，仅允许维护自己提交的模板
        if template.owner_claw_id and template.owner_claw_id == actor.id:
            return None
        return jsonify({'error': '仅可操作自己提交的模板文件'}), 403
    return jsonify({'error': '权限校验失败'}), 403


@api_bp.route('/agent-templates', methods=['GET'])
def list_agent_templates():
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    status = (request.args.get('status') or '').strip()
    q = AgentRoleTemplate.query.order_by(AgentRoleTemplate.updated_at.desc())
    if status:
        q = q.filter(AgentRoleTemplate.status == status)
    # Bearer Token（非超管）只能看自己提交的模板
    if actor_type == 'claw':
        q = q.filter(AgentRoleTemplate.owner_claw_id == actor.id)
    rows = q.all()
    return jsonify([r.to_dict() for r in rows])


@api_bp.route('/agent-templates', methods=['POST'])
def create_agent_template():
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    data = request.get_json() or {}
    try:
        template_key = _normalize_template_key(data.get('template_key') or data.get('name'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if AgentRoleTemplate.query.filter_by(template_key=template_key).first():
        return jsonify({'error': 'template_key 已存在'}), 409
    if not (data.get('name') or '').strip():
        return jsonify({'error': 'name 不能为空'}), 400

    creator_name = (actor.name if actor_type == 'claw' else
                    (actor.username or actor.display_name or 'system'))
    row = AgentRoleTemplate(
        template_key=template_key,
        name=(data.get('name') or '').strip(),
        profile_name=(data.get('profile_name') or '').strip(),
        role_name=(data.get('role_name') or '').strip(),
        main_responsibility=(data.get('main_responsibility') or '').strip(),
        skills_summary=(data.get('skills_summary') or '').strip(),
        rules_summary=(data.get('rules_summary') or '').strip(),
        schedules_summary=(data.get('schedules_summary') or '').strip(),
        installed_skill_ids=data.get('installed_skill_ids') or [],
        rule_ids=data.get('rule_ids') or [],
        schedule_items=data.get('schedule_items') or [],
        status='draft',
        current_version=1,
        created_by=creator_name,
        owner_claw_id=(actor.id if actor_type == 'claw' else None),
    )
    db.session.add(row)
    db.session.flush()
    row.current_version = 1
    _save_template_version(
        row, action='create', change_note='创建模板',
        created_by=creator_name,
        version_no=1,
    )
    db.session.commit()
    os.makedirs(_template_dir(row), exist_ok=True)
    return jsonify(row.to_dict()), 201


@api_bp.route('/agent-templates/<int:template_id>', methods=['GET'])
def get_agent_template(template_id: int):
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    # Bearer Token 只能查自己的模板
    if actor_type == 'claw' and row.owner_claw_id != actor.id:
        return jsonify({'error': '仅可查看自己提交的模板'}), 403
    payload = row.to_dict()
    file_rows = AgentRoleTemplateFile.query.filter_by(
        template_id=row.id
    ).order_by(AgentRoleTemplateFile.created_at.desc()).all()
    payload['files'] = [f.to_dict() for f in file_rows]
    payload['responsibility_references'] = _resolve_references(row.main_responsibility or '', file_rows)
    payload['reference_validation'] = _validate_template_references(row)
    return jsonify(payload)


@api_bp.route('/agent-templates/<int:template_id>', methods=['PUT'])
def update_agent_template(template_id: int):
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    # Bearer Token 只能更新自己的模板
    if actor_type == 'claw' and row.owner_claw_id != actor.id:
        return jsonify({'error': '仅可修改自己提交的模板'}), 403
    data = request.get_json() or {}

    creator_name = (actor.name if actor_type == 'claw' else
                    (actor.username or actor.display_name or 'system'))

    if 'name' in data:
        row.name = (data.get('name') or '').strip()
        if not row.name:
            return jsonify({'error': 'name 不能为空'}), 400
    if 'template_key' in data:
        try:
            key = _normalize_template_key(data.get('template_key') or '')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        conflict = AgentRoleTemplate.query.filter(
            AgentRoleTemplate.template_key == key,
            AgentRoleTemplate.id != row.id
        ).first()
        if conflict:
            return jsonify({'error': 'template_key 已存在'}), 409
        if key != row.template_key:
            old_dir = _template_dir(row)
            row.template_key = key
            new_dir = _template_dir(row)
            os.makedirs(os.path.dirname(new_dir), exist_ok=True)
            if os.path.isdir(old_dir):
                os.rename(old_dir, new_dir)
    for key in (
        'profile_name', 'role_name', 'main_responsibility',
        'skills_summary', 'rules_summary', 'schedules_summary'
    ):
        if key in data:
            setattr(row, key, (data.get(key) or '').strip())
    for key in ('installed_skill_ids', 'rule_ids', 'schedule_items'):
        if key in data:
            setattr(row, key, data.get(key) or [])

    if data.get('bump_version'):
        row.current_version = _next_template_version(row)
        _save_template_version(
            row,
            action='save',
            change_note=(data.get('change_note') or '手动保存模板'),
            created_by=creator_name,
            version_no=row.current_version,
        )
    row.review_comment = (data.get('review_comment') or row.review_comment or '').strip()
    db.session.commit()
    return jsonify(row.to_dict())


@api_bp.route('/agent-templates/<int:template_id>', methods=['DELETE'])
def delete_agent_template(template_id: int):
    _, err = _require_super_admin()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    tdir = _template_dir(row)
    # 先删子表，避免 SQLAlchemy 在删主表时尝试把版本行 template_id 置空，
    # 触发旧 MySQL 上 uq_artv_template_version 的唯一键冲突。
    AgentRoleTemplateVersion.query.filter_by(template_id=row.id).delete(synchronize_session=False)
    AgentRoleTemplateFile.query.filter_by(template_id=row.id).delete(synchronize_session=False)
    db.session.delete(row)
    db.session.commit()
    if os.path.isdir(tdir):
        shutil.rmtree(tdir, ignore_errors=True)
    return jsonify({'message': '模板已删除'})


@api_bp.route('/agent-templates/<int:template_id>/review', methods=['POST'])
def review_agent_template(template_id: int):
    """审核 Agent 角色模板。

    请求体（支持 v2 统一字段与历史字段两种写法）：
      v2 推荐:  { "action": "approve" | "reject", "comment": "审核意见" }
      兼容:    { "status": "approved"|"rejected"|...,
                "review_comment"|"comment"|"notes": "审核意见" }

    注：模板状态机比 skill/rule/knowledge 更宽（draft/pending_review/approved/
    rejected/archived），所以传 status 时仍按原 enum 校验。
    """
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    # Bearer Token 只能审核自己提交的模板
    if actor_type == 'claw' and row.owner_claw_id != actor.id:
        return jsonify({'error': '仅可审核自己提交的模板'}), 403
    data = request.get_json() or {}

    # 优先识别 v2 action，映射到模板 status；否则用历史 status 字段
    raw_action = (data.get('action') or '').strip().lower()
    ACTION_TO_STATUS = {'approve': 'approved', 'approved': 'approved',
                        'reject': 'rejected', 'rejected': 'rejected'}
    status = ACTION_TO_STATUS.get(raw_action) or (data.get('status') or '').strip()
    if status not in ('draft', 'pending_review', 'approved', 'rejected', 'archived'):
        return jsonify({
            'error': ('status / action 非法。推荐 action=approve|reject；'
                      '兼容 status=draft|pending_review|approved|rejected|archived'),
        }), 400

    # 评论字段三套别名（v2 统一）
    comment = (
        data.get('comment')
        or data.get('review_comment')
        or data.get('notes')
        or ''
    )
    comment = str(comment).strip()

    reviewer_name = (actor.name if actor_type == 'claw' else
                     (actor.username or actor.display_name or ''))
    row.status = status
    row.review_comment = comment
    row.reviewed_by = reviewer_name
    row.current_version = _next_template_version(row)
    _save_template_version(
        row,
        action='review',
        change_note=f'审核状态更新为 {status}' + (f'：{comment}' if comment else ''),
        created_by=reviewer_name or 'system',
        version_no=row.current_version,
    )
    # 注：暂未写 review_comments 时间线，因为 ReviewComment.resource_type enum
    # 不含 'agent_template'。后续如需统一，需 ALTER TABLE 扩 enum。见 MEMORY #131。
    db.session.commit()
    payload = row.to_dict()
    payload.update({'action': raw_action or None, 'comment': comment, 'review_comment': comment})
    return jsonify(payload)


@api_bp.route('/agent-templates/submit', methods=['POST'])
def submit_agent_template():
    """给 Agent 侧提交入口：允许 Bearer Token 直接提交（生成 pending_review）。"""
    agent = _get_agent_from_bearer()
    data = request.get_json() or {}
    if not agent:
        _, err = _require_super_admin()
        if err:
            return jsonify({'error': '仅支持 Agent Bearer Token 或超管提交'}), 403

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name 不能为空'}), 400
    key_seed = data.get('template_key') or f"{name}-{agent.id if agent else _operator_name('sa')}"
    try:
        template_key = _normalize_template_key(key_seed)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    existing = AgentRoleTemplate.query.filter_by(template_key=template_key).first()
    if existing:
        row = existing
        row.name = name
        row.profile_name = (data.get('profile_name') or row.profile_name or '').strip()
        row.role_name = (data.get('role_name') or row.role_name or '').strip()
        row.main_responsibility = (data.get('main_responsibility') or row.main_responsibility or '').strip()
        row.skills_summary = (data.get('skills_summary') or row.skills_summary or '').strip()
        row.rules_summary = (data.get('rules_summary') or row.rules_summary or '').strip()
        row.schedules_summary = (data.get('schedules_summary') or row.schedules_summary or '').strip()
        row.installed_skill_ids = data.get('installed_skill_ids') or row.installed_skill_ids or []
        row.rule_ids = data.get('rule_ids') or row.rule_ids or []
        row.schedule_items = data.get('schedule_items') or row.schedule_items or []
        row.current_version = _next_template_version(row)
    else:
        row = AgentRoleTemplate(
            template_key=template_key,
            name=name,
            profile_name=(data.get('profile_name') or '').strip(),
            role_name=(data.get('role_name') or '').strip(),
            main_responsibility=(data.get('main_responsibility') or '').strip(),
            skills_summary=(data.get('skills_summary') or '').strip(),
            rules_summary=(data.get('rules_summary') or '').strip(),
            schedules_summary=(data.get('schedules_summary') or '').strip(),
            installed_skill_ids=data.get('installed_skill_ids') or [],
            rule_ids=data.get('rule_ids') or [],
            schedule_items=data.get('schedule_items') or [],
            current_version=1,
            created_by=(agent.name if agent else _operator_name()),
        )
        db.session.add(row)
        db.session.flush()
    row.status = 'pending_review'
    row.owner_claw_id = agent.id if agent else row.owner_claw_id
    _save_template_version(
        row,
        action='submit',
        change_note='Agent 提交模板',
        created_by=(agent.name if agent else _operator_name()),
        version_no=int(row.current_version or 1),
    )
    db.session.commit()
    os.makedirs(_template_dir(row), exist_ok=True)
    return jsonify({
        'message': '模板已提交',
        'template': row.to_dict(),
    })


@api_bp.route('/agent-templates/<int:template_id>/files', methods=['GET'])
def list_agent_template_files(template_id: int):
    actor, actor_type, err = _require_template_file_operator()
    if err:
        return err
    tpl = AgentRoleTemplate.query.get_or_404(template_id)
    deny = _check_template_file_access(tpl, actor, actor_type)
    if deny:
        return deny
    rows = AgentRoleTemplateFile.query.filter_by(template_id=template_id).order_by(
        AgentRoleTemplateFile.created_at.desc()
    ).all()
    return jsonify([r.to_dict() for r in rows])


@api_bp.route('/agent-templates/<int:template_id>/references/validate', methods=['GET'])
def validate_agent_template_references(template_id: int):
    actor, actor_type, err = _require_template_file_operator()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    deny = _check_template_file_access(row, actor, actor_type)
    if deny:
        return deny
    return jsonify(_validate_template_references(row))


@api_bp.route('/agent-templates/<int:template_id>/files', methods=['POST'])
def upload_agent_template_file(template_id: int):
    actor, actor_type, err = _require_template_file_operator()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    deny = _check_template_file_access(row, actor, actor_type)
    if deny:
        return deny
    if 'file' not in request.files:
        return jsonify({'error': '请使用 file 字段上传文件'}), 400
    fs = request.files['file']
    if not fs or not fs.filename:
        return jsonify({'error': '未选择文件'}), 400
    payload = fs.read()
    if len(payload) > _MAX_UPLOAD_SIZE:
        return jsonify({'error': '文件超过 20MB 限制'}), 400

    rel_dir = (request.form.get('dir') or '').strip()
    safe_name = secure_filename(fs.filename) or 'file.bin'
    rel_path = f"{rel_dir.rstrip('/')}/{safe_name}" if rel_dir else safe_name
    try:
        rel_path = _normalize_relpath(rel_path)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    tdir = _template_dir(row)
    abs_path = os.path.abspath(os.path.join(tdir, rel_path))
    if not abs_path.startswith(os.path.abspath(tdir)):
        return jsonify({'error': '路径非法'}), 400
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, 'wb') as fh:
        fh.write(payload)

    sha = hashlib.sha256(payload).hexdigest()
    item = AgentRoleTemplateFile.query.filter_by(template_id=row.id, relative_path=rel_path).first()
    if not item:
        item = AgentRoleTemplateFile(template_id=row.id, relative_path=rel_path)
        db.session.add(item)
    item.file_name = safe_name
    item.mime_type = fs.mimetype or ''
    item.file_size = len(payload)
    item.sha256 = sha
    item.uploaded_by = _operator_name('system')
    db.session.commit()
    return jsonify(item.to_dict()), 201


@api_bp.route('/agent-templates/<int:template_id>/files/<int:file_id>', methods=['DELETE'])
def delete_agent_template_file(template_id: int, file_id: int):
    actor, actor_type, err = _require_template_file_operator()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    deny = _check_template_file_access(row, actor, actor_type)
    if deny:
        return deny
    item = AgentRoleTemplateFile.query.filter_by(template_id=row.id, id=file_id).first_or_404()
    abs_path = os.path.abspath(os.path.join(_template_dir(row), item.relative_path))
    db.session.delete(item)
    db.session.commit()
    if abs_path.startswith(os.path.abspath(_template_dir(row))) and os.path.isfile(abs_path):
        os.remove(abs_path)
    return jsonify({'message': '文件已删除'})


@api_bp.route('/agent-templates/<int:template_id>/files/<int:file_id>/download', methods=['GET'])
def download_agent_template_file(template_id: int, file_id: int):
    actor, actor_type, err = _require_template_file_operator()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    deny = _check_template_file_access(row, actor, actor_type)
    if deny:
        return deny
    item = AgentRoleTemplateFile.query.filter_by(template_id=row.id, id=file_id).first_or_404()
    abs_path = os.path.abspath(os.path.join(_template_dir(row), item.relative_path))
    if not os.path.isfile(abs_path):
        return jsonify({'error': '文件不存在或已被移除'}), 404
    inline = (request.args.get('inline') or '').strip() == '1'
    return send_file(abs_path, as_attachment=not inline, download_name=item.file_name)


@api_bp.route('/agent-templates/<int:template_id>/files/<int:file_id>/preview', methods=['GET'])
def preview_agent_template_file(template_id: int, file_id: int):
    actor, actor_type, err = _require_template_file_operator()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    deny = _check_template_file_access(row, actor, actor_type)
    if deny:
        return deny
    item = AgentRoleTemplateFile.query.filter_by(template_id=row.id, id=file_id).first_or_404()
    abs_path = os.path.abspath(os.path.join(_template_dir(row), item.relative_path))
    if not os.path.isfile(abs_path):
        return jsonify({'error': '文件不存在或已被移除'}), 404

    mime = (item.mime_type or '').lower()
    is_image = mime.startswith('image/')
    if is_image:
        return send_file(abs_path, as_attachment=False, download_name=item.file_name)

    with open(abs_path, 'rb') as fh:
        raw = fh.read()
    is_binary = b'\x00' in raw
    if is_binary:
        return jsonify({
            'file': item.to_dict(),
            'previewable': False,
            'error': '二进制文件不支持文本预览，请下载查看',
        }), 400

    text = raw.decode('utf-8', errors='replace')
    truncated = False
    if len(text) > 200000:
        text = text[:200000]
        truncated = True
    return jsonify({
        'file': item.to_dict(),
        'previewable': True,
        'content': text,
        'truncated': truncated,
    })


@api_bp.route('/agent-templates/<int:template_id>/versions', methods=['GET'])
def list_agent_template_versions(template_id: int):
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    tpl = AgentRoleTemplate.query.get_or_404(template_id)
    if actor_type == 'claw' and tpl.owner_claw_id != actor.id:
        return jsonify({'error': '仅可查看自己提交的模板版本'}), 403
    rows = AgentRoleTemplateVersion.query.filter_by(template_id=template_id).order_by(
        AgentRoleTemplateVersion.version_no.desc()
    ).all()
    return jsonify([r.to_dict() for r in rows])


@api_bp.route('/agent-templates/<int:template_id>/rollback', methods=['POST'])
def rollback_agent_template(template_id: int):
    user, err = _require_super_admin()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    data = request.get_json() or {}
    version_no = data.get('version_no')
    if not version_no:
        return jsonify({'error': 'version_no 必填'}), 400
    target = AgentRoleTemplateVersion.query.filter_by(
        template_id=row.id, version_no=int(version_no)
    ).first()
    if not target:
        return jsonify({'error': '目标版本不存在'}), 404
    snapshot = _parse_snapshot(target)
    if not snapshot:
        return jsonify({'error': '目标版本快照不可用'}), 400

    row.name = (snapshot.get('name') or row.name or '').strip()
    row.template_key = (snapshot.get('template_key') or row.template_key or '').strip()
    row.profile_name = snapshot.get('profile_name') or ''
    row.role_name = snapshot.get('role_name') or ''
    row.main_responsibility = snapshot.get('main_responsibility') or ''
    row.skills_summary = snapshot.get('skills_summary') or ''
    row.rules_summary = snapshot.get('rules_summary') or ''
    row.schedules_summary = snapshot.get('schedules_summary') or ''
    row.installed_skill_ids = snapshot.get('installed_skill_ids') or []
    row.rule_ids = snapshot.get('rule_ids') or []
    row.schedule_items = snapshot.get('schedule_items') or []
    row.status = snapshot.get('status') or row.status
    row.review_comment = snapshot.get('review_comment') or ''
    row.owner_claw_id = snapshot.get('owner_claw_id')

    row.current_version = _next_template_version(row)
    _save_template_version(
        row,
        action='rollback',
        change_note=f'回滚到 v{int(version_no)}',
        created_by=user.username or user.display_name or 'system',
        version_no=row.current_version,
    )
    db.session.commit()
    return jsonify({
        'message': f'已回滚到 v{int(version_no)} 并生成新版本 v{row.current_version}',
        'template': row.to_dict(),
    })


@api_bp.route('/agent-templates/<int:template_id>/apply', methods=['POST'])
def apply_agent_template(template_id: int):
    """模板应用到目标 OpenClaw：安装 skills/rules 并同步定时任务。"""
    actor, actor_type, err = _require_template_admin()
    if err:
        return err
    row = AgentRoleTemplate.query.get_or_404(template_id)
    # Bearer Token 只能应用自己的模板到自己
    if actor_type == 'claw' and row.owner_claw_id != actor.id:
        return jsonify({'error': '仅可应用自己提交的模板'}), 403
    data = request.get_json() or {}
    target_claw_id = data.get('target_claw_id')
    if not target_claw_id:
        return jsonify({'error': 'target_claw_id 必填'}), 400
    # Bearer Token 只能应用到自己
    if actor_type == 'claw' and int(target_claw_id) != actor.id:
        return jsonify({'error': 'Agent 只能将模板应用到自己'}), 403
    claw = OpenClawInstance.query.get(target_claw_id)
    if not claw:
        return jsonify({'error': '目标 OpenClaw 不存在'}), 404
    ref_check = _validate_template_references(row)
    strict_refs = bool(data.get('strict_references', True))
    if strict_refs and not ref_check['valid']:
        return jsonify({
            'error': '模板引用文件校验未通过，禁止应用',
            'reference_validation': ref_check,
        }), 400

    summary = {
        'skills_installed': 0,
        'skills_reenabled': 0,
        'skills_missing': [],
        'rules_installed': 0,
        'rules_reenabled': 0,
        'rules_missing': [],
        'schedule_created': 0,
        'schedule_updated': 0,
        'schedule_disabled': 0,
        'todo_ids': [],
    }

    now_dt = _now()
    skill_ids = [int(x) for x in (row.installed_skill_ids or []) if str(x).isdigit()]
    for sid in skill_ids:
        skill = Skill.query.get(sid)
        if not skill or skill.is_deleted:
            summary['skills_missing'].append(sid)
            continue
        assoc = OpenClawSkill.query.filter_by(openclaw_id=claw.id, skill_id=sid).first()
        is_reinstall = False
        if assoc:
            if not assoc.enabled:
                summary['skills_reenabled'] += 1
            assoc.enabled = True
            assoc.installed_at = now_dt
            is_reinstall = True
        else:
            db.session.add(OpenClawSkill(
                openclaw_id=claw.id,
                skill_id=sid,
                enabled=True,
                installed_at=now_dt,
            ))
            summary['skills_installed'] += 1
        todo = ClawTodo(
            openclaw_id=claw.id,
            title=f'{"重新安装" if is_reinstall else "安装"} Skill：{skill.display_name}',
            description=f'模板「{row.name}」应用触发，请{"重新" if is_reinstall else ""}拉取 Skill {skill.name}。',
            schedule_type='once',
            urgency_level='interrupt',
            priority='P0',
            task_category='routine',
            verification_target=f'{"reinstall" if is_reinstall else "install"}-skill:{skill.name}',
            enabled=True,
            created_by='hub',
        )
        db.session.add(todo)
        db.session.flush()
        summary['todo_ids'].append(todo.id)

    rule_ids = [int(x) for x in (row.rule_ids or []) if str(x).isdigit()]
    for rid in rule_ids:
        rule = Rule.query.get(rid)
        if not rule or rule.is_deleted:
            summary['rules_missing'].append(rid)
            continue
        assoc = OpenClawRule.query.filter_by(openclaw_id=claw.id, rule_id=rid).first()
        is_reinstall = False
        if assoc:
            if not assoc.enabled:
                summary['rules_reenabled'] += 1
            assoc.enabled = True
            assoc.applied = True
            assoc.applied_at = now_dt
            is_reinstall = True
        else:
            db.session.add(OpenClawRule(
                openclaw_id=claw.id,
                rule_id=rid,
                enabled=True,
                applied=True,
                applied_at=now_dt,
            ))
            summary['rules_installed'] += 1

        todo = ClawTodo(
            openclaw_id=claw.id,
            title=f'{"重新安装" if is_reinstall else "安装"} Rule：{rule.display_name}',
            description=f'模板「{row.name}」应用触发，请{"重新" if is_reinstall else ""}拉取 Rule {rule.name}。',
            schedule_type='once',
            urgency_level='interrupt',
            priority='P0',
            task_category='routine',
            verification_target=f'{"reinstall" if is_reinstall else "install"}-rule:{rule.name}',
            enabled=True,
            created_by='hub',
        )
        db.session.add(todo)
        db.session.flush()
        summary['todo_ids'].append(todo.id)

    schedule_items = row.schedule_items or []
    expected_schedule_targets = set()
    for idx, item in enumerate(schedule_items):
        default_key = f'template-schedule:{row.id}:{idx + 1}'
        normalized = _normalize_schedule_item(item, default_key=default_key)
        if not normalized:
            continue
        vt = normalized['verification_target']
        expected_schedule_targets.add(vt)
        existing = ClawTodo.query.filter_by(
            openclaw_id=claw.id,
            verification_target=vt
        ).first()
        if existing:
            existing.title = normalized['title']
            existing.description = normalized['description']
            existing.schedule_type = normalized['schedule_type']
            existing.schedule_time = normalized['schedule_time']
            existing.schedule_day = normalized['schedule_day']
            existing.urgency_level = normalized['urgency_level']
            existing.priority = normalized['priority']
            existing.task_category = normalized['task_category']
            existing.retry_delay = normalized['retry_delay']
            existing.retry_max = normalized['retry_max']
            existing.enabled = True
            summary['schedule_updated'] += 1
        else:
            db.session.add(ClawTodo(
                openclaw_id=claw.id,
                title=normalized['title'],
                description=normalized['description'],
                schedule_type=normalized['schedule_type'],
                schedule_time=normalized['schedule_time'],
                schedule_day=normalized['schedule_day'],
                urgency_level=normalized['urgency_level'],
                priority=normalized['priority'],
                task_category=normalized['task_category'],
                retry_delay=normalized['retry_delay'],
                retry_max=normalized['retry_max'],
                verification_target=normalized['verification_target'],
                enabled=True,
                created_by='hub',
            ))
            summary['schedule_created'] += 1

    old_template_todos = ClawTodo.query.filter(
        ClawTodo.openclaw_id == claw.id,
        ClawTodo.verification_target.like(f'template-schedule:{row.id}:%'),
        ClawTodo.enabled == True
    ).all()
    for old in old_template_todos:
        if old.verification_target not in expected_schedule_targets:
            old.enabled = False
            summary['schedule_disabled'] += 1

    msg = ClawMessage(
        claw_id=claw.id,
        sender_name='Hub',
        content=(
            f'[应用模板] {row.name} 已应用：'
            f'Skill {summary["skills_installed"] + summary["skills_reenabled"]} 条，'
            f'Rule {summary["rules_installed"] + summary["rules_reenabled"]} 条，'
            f'定时任务新增 {summary["schedule_created"]} / 更新 {summary["schedule_updated"]}'
        ),
        msg_type='sync_config',
        direction='to_claw',
        status='pending',
    )
    db.session.add(msg)

    row.current_version = _next_template_version(row)
    _save_template_version(
        row,
        action='apply',
        change_note=f'应用到 OpenClaw#{claw.id}({claw.name})',
        created_by=user.username or user.display_name or 'system',
        version_no=row.current_version,
    )
    db.session.commit()

    from app.api.agent_client import notify_claw, notify_claw_todo
    notify_claw(claw.id)
    notify_claw_todo(claw.id)

    return jsonify({
        'message': '模板应用成功',
        'target_claw_id': claw.id,
        'target_claw_name': claw.name,
        'reference_validation': ref_check,
        'summary': summary,
    })

