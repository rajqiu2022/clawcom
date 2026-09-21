"""Fenced Skill installation acknowledgements, independent of message completion."""
import hashlib
import json
import re
import secrets

from app import db
from app.models import ClawTodo, OpenClawSkill, Skill, SkillInstallationReceipt, _now
from app.services.skill_delivery import SkillDeliveryError, skill_bundle_descriptor, _skill_unavailable_reason


def reset_installation(link, skill=None):
    skill = skill or link.skill
    if skill:
        for todo in ClawTodo.query.filter(
                ClawTodo.openclaw_id == link.openclaw_id,
                ClawTodo.enabled == True,
                ClawTodo.verification_target.in_([
                    'install-skill:' + skill.name, 'reinstall-skill:' + skill.name])).all():
            todo.enabled = False
    link.assigned_at = _now()
    link.installation_generation = secrets.token_hex(16)
    link.installation_status = 'pending'
    link.installation_verified_at = None
    link.installation_receipt_json = None
    link.installation_todo_id = None
    # Preserve installed_at for historical audit; freshness uses verified receipt.


def installation_view(link, descriptor=None):
    receipt = link.installation_receipt_json or {}
    state = link.installation_status or 'pending'
    if descriptor is None and state == 'verified':
        try:
            descriptor = skill_bundle_descriptor(link.skill)
        except ValueError:
            descriptor = {}
    descriptor = descriptor or {}
    verified = bool(link.enabled and not link.skill.is_deleted
        and (link.skill.review_status or 'approved') == 'approved'
        and state == 'verified' and link.installation_verified_at
        and receipt.get('generation') == link.installation_generation
        and descriptor.get('sha256')
        and receipt.get('bundle_sha256') == descriptor.get('sha256')
        and receipt.get('content_version') == descriptor.get('content_version'))
    if not link.enabled or link.skill.is_deleted:
        state = 'unassigned'
    elif state == 'verified' and not verified:
        state = 'pending'
    return {'contract_version': 1, 'state': state, 'verified': verified,
            'todo_id': link.installation_todo_id,
            'generation': link.installation_generation,
            'assigned_at': str(link.assigned_at) if link.assigned_at else None,
            'verified_at': str(link.installation_verified_at) if verified else None,
            'receipt_api': '/api/v1/openclaws/%s/skills/%s/installation-receipts' % (
                link.openclaw_id, link.skill_id),
            'evidence_source': 'authenticated_worker_receipt'}


def _error(code, message, status=409):
    raise SkillDeliveryError(code, message, status)


def accept_receipt(claw, skill_id, data):
    if not isinstance(data, dict) or set(data) - {
        'generation', 'event_id', 'state', 'bundle_sha256', 'content_version', 'files', 'runtime', 'error_code'}:
        _error('SKILL_RECEIPT_INVALID', '回执字段不合法', 400)
    skill = Skill.query.filter_by(id=skill_id).with_for_update().first()
    link = OpenClawSkill.query.filter_by(openclaw_id=claw.id, skill_id=skill_id).with_for_update().first()
    if not skill or skill.is_deleted or not link or not link.enabled:
        _error('SKILL_NOT_ASSIGNED', '当前 Claw 未获此 Skill 的有效分配', 403)
    if _skill_unavailable_reason(skill, claw):
        _error('SKILL_NOT_AVAILABLE', 'Skill 当前不可下发', 403)
    if not link.installation_generation or data.get('generation') != link.installation_generation:
        _error('SKILL_INSTALLATION_STALE', '分配批次已变化，请重新拉取 manifest')
    try:
        descriptor = skill_bundle_descriptor(skill)
    except ValueError:
        _error('SKILL_CONTENT_INVALID', 'Skill 文件路径不合法，请由管理员修复', 409)
    if (data.get('bundle_sha256') != descriptor['sha256']
            or data.get('content_version') != descriptor['content_version']):
        _error('SKILL_CONTENT_STALE', 'Skill 内容已变化或摘要不匹配，请重新拉取 manifest')
    event_id = data.get('event_id')
    if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', event_id):
        _error('SKILL_RECEIPT_INVALID', 'event_id 格式不合法', 400)
    try:
        encoded = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError):
        _error('SKILL_RECEIPT_INVALID', '回执须为有效 JSON', 400)
    if len(encoded) > 128 * 1024:
        _error('SKILL_RECEIPT_INVALID', '回执过大', 413)
    digest = hashlib.sha256(encoded).hexdigest()
    previous = SkillInstallationReceipt.query.filter_by(assignment_id=link.id,
        generation=link.installation_generation, event_id=event_id).first()
    if previous:
        if previous.request_sha256 != digest:
            _error('SKILL_RECEIPT_CONFLICT', '相同 event_id 不得改变内容')
        return dict(previous.response_json, replayed=True)
    state = data.get('state')
    if state not in ('syncing', 'verified', 'failed'):
        _error('SKILL_RECEIPT_INVALID', 'state 仅支持 syncing/verified/failed', 400)
    extras = set(data) & {'runtime', 'files', 'error_code'}
    if extras - ({'runtime', 'files'} if state == 'verified' else {'error_code'} if state == 'failed' else set()):
        _error('SKILL_RECEIPT_INVALID', '回执字段与状态不匹配', 400)
    if installation_view(link, descriptor)['verified']:
        _error('SKILL_INSTALLATION_TERMINAL', '本批次已验证；重装须由管理员重新分配')
    if state == 'verified':
        expected = {f['path']: f['sha256'] for f in descriptor['files']}
        if not expected or data.get('files') != expected:
            _error('SKILL_FILES_MISMATCH', '必须提供全部文件路径与正确 SHA-256')
        runtime = data.get('runtime')
        if (not isinstance(runtime, dict) or set(runtime) != {
                'claw_id', 'instance_id', 'skill_scope', 'manifest_sha256', 'loaded_sha256', 'reload_succeeded'}
                or type(runtime.get('claw_id')) is not int or runtime['claw_id'] != claw.id
                or not isinstance(runtime.get('instance_id'), str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', runtime['instance_id'])
                or runtime.get('skill_scope') != 'instance'
                or runtime.get('manifest_sha256') != descriptor['sha256']
                or runtime.get('loaded_sha256') != descriptor['sha256']
                or runtime.get('reload_succeeded') is not True):
            _error('SKILL_RUNTIME_UNVERIFIED', '须回报当前实例的 manifest、加载摘要及重载成功凭据')
        link.installation_verified_at = _now()
        link.installed_at = link.installation_verified_at
    elif state == 'failed':
        code = data.get('error_code')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9_]{1,80}', code):
            _error('SKILL_RECEIPT_INVALID', '失败回执需要非敏感 error_code', 400)
    link.installation_status = state
    link.installation_receipt_json = dict(data)
    response = {'claw_id': claw.id, 'skill_id': skill_id, 'event_id': event_id,
                'installation': installation_view(link, descriptor), 'replayed': False}
    db.session.add(SkillInstallationReceipt(assignment_id=link.id, generation=link.installation_generation,
        event_id=event_id, request_sha256=digest, response_json=response))
    return response


def require_installation_for_todo(todo):
    prefix, _, name = (todo.verification_target or '').partition(':')
    if prefix not in ('install-skill', 'reinstall-skill'):
        return
    skill = Skill.query.filter_by(name=name).first()
    link = OpenClawSkill.query.filter_by(openclaw_id=todo.openclaw_id,
        skill_id=skill.id).with_for_update().first() if skill else None
    if (not link or link.installation_todo_id != todo.id
            or not installation_view(link)['verified']):
        _error('SKILL_INSTALLATION_RECEIPT_REQUIRED', '安装待办须先收到本批次有效安装回执；聊天完成不是安装凭据')


def control_instructions(claw_id, skill_id, generation):
    return '\n'.join([
        '[Hub Skill 控制面任务；不得交由 LLM 自由执行安装]',
        'claw_id=%s skill_id=%s generation=%s' % (claw_id, skill_id, generation),
        'Worker 使用当前实例受控身份拉取 /api/v1/openclaws/%s/skill-manifest。' % claw_id,
        '按 manifest 的 pack_url 拉取并校验所有文件 SHA；使用实例隔离 Skill 目录，禁止共享 ~/.qclaw/skills。',
        '验证运行时 manifest 和重载结果后，POST 对应 Skill 的 installation.receipt_api；再完成本待办。',
        '未支持此合同的 Worker 应报告阻塞，不得声称安装成功；不要读取、请求或输出凭据。',
    ])
