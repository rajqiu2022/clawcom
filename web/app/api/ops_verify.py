"""执行后校验 API：从 DB 重读真实落库状态并与期望比较。

POST /api/v1/ops/verify
设计文档：docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md
"""
from __future__ import annotations

import json

from flask import g, jsonify, request

from app import db
from app.api import api_bp
from app.models import ClawOpsVerification, _now
from app.services.ops_verification import (
    RESOURCE_MODEL_NAMES,
    compare_expected_actual,
    reread_resource,
)


@api_bp.route('/ops/verify', methods=['POST'])
def ops_verify():
    data = request.get_json(silent=True) or {}
    resource_type = data.get('resource_type')
    resource_id = data.get('resource_id')
    expected = data.get('expected') or {}
    token = data.get('token')

    if resource_type not in RESOURCE_MODEL_NAMES:
        return jsonify({
            'error': f'不支持的资源类型：{resource_type}',
            'valid_types': sorted(RESOURCE_MODEL_NAMES),
        }), 400
    if not isinstance(resource_id, int):
        try:
            resource_id = int(resource_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'resource_id 必须为整数'}), 400
    if not isinstance(expected, dict) or not expected:
        return jsonify({'error': 'expected 必须为非空对象'}), 400

    actual = reread_resource(resource_type, resource_id, list(expected.keys()))
    if actual is None:
        return jsonify({'error': '资源不存在'}), 404

    result = compare_expected_actual(expected, actual)

    claw = getattr(g, '_auth_claw', None)
    record = ClawOpsVerification(
        claw_id=getattr(claw, 'id', None),
        token=token,
        resource_type=resource_type,
        resource_id=resource_id,
        expected=json.dumps(expected, ensure_ascii=False),
        actual=json.dumps(actual, ensure_ascii=False, default=str),
        mismatches=json.dumps(result['mismatches'], ensure_ascii=False, default=str),
        verified=result['verified'],
        verified_at=_now(),
    )
    db.session.add(record)
    db.session.commit()

    return jsonify({
        'verified': result['verified'],
        'actual': actual,
        'mismatches': result['mismatches'],
        'verification_id': record.id,
    })
