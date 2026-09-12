"""Super-admin control plane for immutable Claw Worker releases."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

from flask import jsonify, request

from app import db
from app.api import api_bp
from app.api.openclaws import _actor_display_name, _get_user
from app.models import AuditLog, WorkerRelease
from app.services.worker_releases import (
    import_latest_candidate,
    SUPPORTED_PLATFORMS,
    verify_record_artifact,
)


def _super_admin():
    user = _get_user()
    return user if user and user.role == 'super_admin' else None


def _audit(action: str, record: WorkerRelease, actor: str, detail: dict) -> None:
    db.session.add(AuditLog(
        action=action,
        resource_type='worker_release',
        resource_id=record.id,
        resource_name=record.release_id,
        operator=actor,
        ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
    ))


@api_bp.route('/worker-releases', methods=['GET'])
def list_worker_releases():
    if not _super_admin():
        return jsonify({'error': '仅超级管理员可查看 Worker 发布版本'}), 403
    rows = WorkerRelease.query.order_by(
        WorkerRelease.imported_at.desc(), WorkerRelease.id.desc()).all()
    return jsonify({
        'items': [row.to_dict() for row in rows],
        'default': next((row.to_dict() for row in rows
                         if row.is_default and row.approval_status == 'approved'), None),
    })


@api_bp.route('/worker-releases/refresh', methods=['POST'])
def refresh_worker_releases():
    user = _super_admin()
    if not user:
        return jsonify({'error': '仅超级管理员可刷新 Worker 发布版本'}), 403
    actor = _actor_display_name(user)
    data = request.get_json(silent=True) or {}
    platform = str(data.get('platform') or 'linux-x86_64').strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        return jsonify({'error': 'platform 仅支持 linux-x86_64 / windows-x86_64'}), 400
    try:
        record, created = import_latest_candidate(actor, platform=platform)
        _audit('import', record, actor, {
            'created': created,
            'source_commit': record.source_commit,
            'release_manifest_sha256': record.release_manifest_sha256,
            'artifact_sha256': record.artifact_sha256,
        })
        db.session.commit()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    return jsonify({
        'message': 'Worker 候选版本已刷新' if created else 'Worker 候选版本已存在且校验一致',
        'created': created,
        'release': record.to_dict(),
    }), 201 if created else 200


@api_bp.route('/worker-releases/<int:record_id>/approve', methods=['POST'])
def approve_worker_release(record_id: int):
    user = _super_admin()
    if not user:
        return jsonify({'error': '仅超级管理员可批准 Worker 发布版本'}), 403
    record = WorkerRelease.query.get_or_404(record_id)
    data = request.get_json(silent=True) or {}
    if record.signature_status != 'verified' and data.get('confirm_unsigned') is not True:
        return jsonify({'error': '当前制品未签名，必须显式确认 confirm_unsigned=true'}), 400
    try:
        verify_record_artifact(record)
    except (OSError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400
    actor = _actor_display_name(user)
    WorkerRelease.query.filter(
        WorkerRelease.platform == record.platform,
        WorkerRelease.id != record.id,
    ).update({WorkerRelease.is_default: False}, synchronize_session=False)
    before = record.approval_status
    record.approval_status = 'approved'
    record.is_default = True
    record.approved_by = actor
    record.approved_at = datetime.now()
    record.rejected_by = ''
    record.rejected_at = None
    _audit('approve', record, actor, {
        'before': before,
        'source_commit': record.source_commit,
        'artifact_sha256': record.artifact_sha256,
        'set_default': True,
        'confirmed_unsigned': bool(data.get('confirm_unsigned')),
    })
    db.session.commit()
    return jsonify({'message': 'Worker 发布版本已批准并设为默认',
                    'release': record.to_dict()})


@api_bp.route('/worker-releases/<int:record_id>/reject', methods=['POST'])
def reject_worker_release(record_id: int):
    user = _super_admin()
    if not user:
        return jsonify({'error': '仅超级管理员可拒绝 Worker 发布版本'}), 403
    record = WorkerRelease.query.get_or_404(record_id)
    actor = _actor_display_name(user)
    before = record.approval_status
    record.approval_status = 'rejected'
    record.is_default = False
    record.rejected_by = actor
    record.rejected_at = datetime.now()
    _audit('reject', record, actor, {'before': before})
    db.session.commit()
    return jsonify({'message': 'Worker 发布版本已拒绝',
                    'release': record.to_dict()})
