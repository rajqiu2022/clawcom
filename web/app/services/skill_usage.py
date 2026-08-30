"""Skill 完整内容读取统计。"""

from datetime import timedelta

from flask import current_app

from app import db
from app.models import SkillUsageEvent, _now


RECENT_USAGE_WINDOW = timedelta(days=3)
# 批量完整列表主要用于旧客户端同步市场，不能代表列表中每个 Skill 被实际使用。
# 事件继续保留用于审计，但卡片热度只统计明确面向单个 Skill 的读取。
HEAT_EXCLUDED_ACCESS_TYPES = frozenset({'list_full'})


def record_skill_content_access(skill_id, access_type):
    """记录一次成功读取；统计故障不应影响内容接口。"""
    return record_skill_content_accesses([skill_id], access_type)


def record_skill_content_accesses(skill_ids, access_type):
    """批量记录一次 API 调用中读取的多个 Skill。"""
    try:
        timestamp = _now()
        events = [
            SkillUsageEvent(
                skill_id=int(skill_id),
                access_type=str(access_type or 'unknown')[:32],
                accessed_at=timestamp,
            )
            for skill_id in (skill_ids or [])
        ]
        if not events:
            return True
        db.session.add_all(events)
        db.session.commit()
        return True
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(
            '记录 Skill 使用事件失败（skill_ids=%s, access_type=%s）：%s',
            skill_ids, access_type, exc,
        )
        return False


def recent_skill_usage_counts(skill_ids, now=None):
    """批量返回各 Skill 最近 72 小时的定向完整内容读取次数。"""
    normalized_ids = []
    for value in skill_ids or []:
        try:
            normalized_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not normalized_ids:
        return {}

    cutoff = (now or _now()) - RECENT_USAGE_WINDOW
    try:
        rows = (
            db.session.query(
                SkillUsageEvent.skill_id,
                db.func.count(SkillUsageEvent.id),
            )
            .filter(
                SkillUsageEvent.skill_id.in_(normalized_ids),
                SkillUsageEvent.accessed_at >= cutoff,
                SkillUsageEvent.access_type.notin_(HEAT_EXCLUDED_ACCESS_TYPES),
            )
            .group_by(SkillUsageEvent.skill_id)
            .all()
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(
            '查询 Skill 最近使用次数失败：%s', exc,
        )
        return {}
    return {int(skill_id): int(count) for skill_id, count in rows}
