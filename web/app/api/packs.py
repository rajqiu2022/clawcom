"""
标准包 API — Skills 标准包 / Rules 标准包
包里只记录被包含对象的 id 列表，不混合语义。
下发时 Hub 负责把对应 skill/rule 的安装状态同步到具体 claw。
"""

import json
from flask import request, jsonify
from app import db
from app.models import (StandardPack, Skill, Rule,
                        OpenClawSkill, OpenClawRule, OpenClawInstance,
                        ClawMessage)
from app.api import api_bp


# ==================== CRUD ====================

@api_bp.route('/packs', methods=['GET'])
def list_packs():
    """获取所有标准包"""
    pack_type = request.args.get('type')  # skill / rule
    query = StandardPack.query
    if pack_type:
        query = query.filter_by(pack_type=pack_type)
    packs = query.order_by(StandardPack.pack_type, StandardPack.name).all()

    result = []
    for p in packs:
        d = p.to_dict()
        d['items'] = _resolve_items(p)
        result.append(d)
    return jsonify(result)


@api_bp.route('/packs', methods=['POST'])
def create_pack():
    """创建标准包"""
    data = request.get_json()
    if not data or not data.get('name') or not data.get('display_name'):
        return jsonify({'error': 'name 和 display_name 为必填项'}), 400
    if data.get('pack_type') not in ('skill', 'rule'):
        return jsonify({'error': 'pack_type 必须为 skill 或 rule'}), 400

    if StandardPack.query.filter_by(name=data['name']).first():
        return jsonify({'error': f'标准包 "{data["name"]}" 已存在'}), 409

    pack = StandardPack(
        name=data['name'],
        display_name=data['display_name'],
        description=data.get('description', ''),
        pack_type=data['pack_type'],
        item_ids=json.dumps(data.get('item_ids', [])),
        is_active=data.get('is_active', True),
        created_by=data.get('created_by', 'system'),
    )
    db.session.add(pack)
    db.session.commit()
    return jsonify(pack.to_dict()), 201


@api_bp.route('/packs/<int:pack_id>', methods=['GET'])
def get_pack(pack_id):
    """获取标准包详情"""
    pack = StandardPack.query.get_or_404(pack_id)
    d = pack.to_dict()
    d['items'] = _resolve_items(pack)
    return jsonify(d)


@api_bp.route('/packs/<int:pack_id>', methods=['PUT'])
def update_pack(pack_id):
    """更新标准包"""
    pack = StandardPack.query.get_or_404(pack_id)
    data = request.get_json()

    for field in ['display_name', 'description', 'is_active']:
        if field in data:
            setattr(pack, field, data[field])
    if 'item_ids' in data:
        pack._set_item_ids(data['item_ids'])

    db.session.commit()
    d = pack.to_dict()
    d['items'] = _resolve_items(pack)
    return jsonify(d)


@api_bp.route('/packs/<int:pack_id>', methods=['DELETE'])
def delete_pack(pack_id):
    """删除标准包"""
    pack = StandardPack.query.get_or_404(pack_id)
    db.session.delete(pack)
    db.session.commit()
    return jsonify({'message': f'标准包 "{pack.name}" 已删除'})


# ==================== 下发 ====================

@api_bp.route('/packs/<int:pack_id>/apply', methods=['POST'])
def apply_pack(pack_id):
    """将标准包下发到指定 claw（或所有 claw）"""
    pack = StandardPack.query.get_or_404(pack_id)
    data = request.get_json() or {}
    claw_ids = data.get('claw_ids')

    if claw_ids:
        claws = OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(claw_ids),
            OpenClawInstance.status != 'deleted'
        ).all()
    else:
        claws = OpenClawInstance.query.filter(
            OpenClawInstance.status != 'deleted'
        ).all()

    installed_count = 0
    for claw in claws:
        count = _apply_pack_to_claw(pack, claw)
        installed_count += count

    db.session.commit()
    # 通知所有 claw SSE 长连接立即推送
    from app.api.agent_client import notify_claw
    for claw in claws:
        notify_claw(claw.id)
    return jsonify({
        'message': f'标准包 "{pack.display_name}" 已下发',
        'claw_count': len(claws),
        'installed_count': installed_count,
    })


@api_bp.route('/packs/apply-all-active', methods=['POST'])
def apply_all_active_packs():
    """将所有激活的标准包下发到指定 claw"""
    data = request.get_json() or {}
    claw_id = data.get('claw_id')
    if not claw_id:
        return jsonify({'error': 'claw_id 为必填项'}), 400

    claw = OpenClawInstance.query.get_or_404(claw_id)
    active_packs = StandardPack.query.filter_by(is_active=True).all()

    total = 0
    for pack in active_packs:
        total += _apply_pack_to_claw(pack, claw)

    db.session.commit()
    # 通知 SSE 长连接立即推送
    from app.api.agent_client import notify_claw
    notify_claw(claw.id)
    return jsonify({
        'message': f'已为 {claw.name} 下发 {len(active_packs)} 个标准包',
        'pack_count': len(active_packs),
        'installed_count': total,
    })


# ==================== 辅助函数 ====================

def _resolve_items(pack):
    """解析包内 ID 列表为资源摘要"""
    items = []
    for item_id in pack._get_item_ids():
        if pack.pack_type == 'skill':
            s = Skill.query.get(item_id)
            if s:
                items.append({
                    'id': s.id, 'name': s.name,
                    'display_name': s.display_name,
                    'type': 'skill',
                })
        else:
            r = Rule.query.get(item_id)
            if r:
                items.append({
                    'id': r.id, 'name': r.name,
                    'display_name': r.display_name,
                    'type': 'rule',
                })
    return items


def _apply_pack_to_claw(pack, claw):
    """将一个标准包同步到一个 claw，返回新安装的数量"""
    from app.services.skill_delivery import (
        skill_assignment_unavailable_reason,
    )

    count = 0
    for item_id in pack._get_item_ids():
        if pack.pack_type == 'skill':
            skill = Skill.query.get(item_id)
            if (not skill or skill.name == 'registration-skill'
                    or skill_assignment_unavailable_reason(skill, claw)):
                continue
            existing = OpenClawSkill.query.filter_by(
                openclaw_id=claw.id, skill_id=item_id
            ).first()
            if existing:
                if not existing.enabled:
                    existing.enabled = True
                    count += 1
            else:
                db.session.add(OpenClawSkill(
                    openclaw_id=claw.id, skill_id=item_id, enabled=True
                ))
                count += 1
        else:
            rule = Rule.query.get(item_id)
            if not rule:
                continue
            existing = OpenClawRule.query.filter_by(
                openclaw_id=claw.id, rule_id=item_id
            ).first()
            if existing:
                if not existing.enabled:
                    existing.enabled = True
                    count += 1
            else:
                db.session.add(OpenClawRule(
                    openclaw_id=claw.id, rule_id=item_id, enabled=True
                ))
                count += 1

    if count > 0:
        msg = ClawMessage(
            claw_id=claw.id, sender_name='Hub',
            content=f'[标准包下发] {pack.display_name}（{pack.pack_type}），新安装 {count} 项，请同步配置',
            msg_type='sync_config', direction='to_claw', status='pending',
        )
        db.session.add(msg)

    return count
