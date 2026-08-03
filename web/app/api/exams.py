"""Agent 考试系统 API

题库管理 + 考试流程 + 自动判分 + 同行互评。

权限：
- 题库（试卷/题目）的 CRUD：仅 super_admin
- 应考（开始/答题/提交）：登录用户 或 持 Bearer Token 的 OpenClaw
- 考试结果查看：本人 + super_admin
- 同行互评：被分配为 reviewer 的 agent / 用户

设计要点：
- 考生拿到的题目数据**不含 standard_answer / grading_criteria**
- 客观题（single/multiple/judge）提交时立即自动判分
- 主观题（essay/case_design 等）依赖同行互评 + 超管确认
"""
import json
from datetime import datetime, timedelta

from flask import request, jsonify, session
from sqlalchemy import desc

from app import db
from app.api import api_bp
from app.models import (
    ExamPaper, ExamCampaign, ExamQuestion, ExamSession, ExamAnswer,
    ExamPeerReview, OpenClawInstance, User, Project,
)
from app.services.exam_campaigns import (
    can_launch_campaign, agent_can_participate, campaign_is_open,
)


# ===== 工具 =====

def _get_current_user():
    from app.api.auth_utils import get_current_user
    return get_current_user()


def _is_super_admin(user):
    if not user:
        return False
    return getattr(user, 'role', None) == 'super_admin'


def _examinee_display(user):
    if not user:
        return 'anonymous'
    name = getattr(user, '_claw_name', None)
    if name:
        return name
    return getattr(user, 'display_name', None) or getattr(user, 'username', None) or 'unknown'


def _parse_datetime(raw, field_name):
    if not raw:
        return None, None
    if isinstance(raw, datetime):
        return raw, None
    value = str(raw).strip()
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt), None
        except ValueError:
            continue
    return None, f'{field_name} 格式不正确'


def _current_claw(user):
    claw_id = getattr(user, '_claw_id', None) or getattr(user, 'bound_claw_id', None)
    if claw_id:
        return OpenClawInstance.query.get(claw_id)
    claw_name = getattr(user, '_claw_name', None)
    if claw_name:
        return OpenClawInstance.query.filter_by(name=claw_name).first()
    return None


def _user_project_ids(user):
    ids = set()
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(pid))
        except Exception:
            continue
    claw = _current_claw(user)
    if claw and claw.project_id:
        ids.add(int(claw.project_id))
    return ids


def _is_global_admin(user):
    return _is_super_admin(user) or bool(getattr(user, 'is_global', False))


def _campaign_visible_to_user(user, campaign):
    if not user or not campaign:
        return False
    if _is_global_admin(user):
        return True
    claw = _current_claw(user)
    if campaign.launched_by_user_id and getattr(user, 'id', None) == campaign.launched_by_user_id:
        return True
    if claw and campaign.launched_by_claw_id == claw.id:
        return True
    if campaign.scope == 'global':
        return True
    if campaign.scope == 'project':
        return campaign.project_id in _user_project_ids(user)
    if campaign.scope == 'personal':
        return bool(claw and claw.id in (campaign.target_claw_ids or []))
    return False


def _can_delete_session(user, sess):
    if not user or not sess:
        return False
    if _is_global_admin(user):
        return True
    campaign = sess.campaign
    claw = _current_claw(user)
    if campaign:
        if campaign.launched_by_user_id and getattr(user, 'id', None) == campaign.launched_by_user_id:
            return True
        if claw and campaign.launched_by_claw_id == claw.id:
            return True
    if sess.examinee_claw_id:
        examinee = OpenClawInstance.query.get(sess.examinee_claw_id)
        if examinee and examinee.owner == getattr(user, 'username', None):
            return True
    return False


def _can_view_session(user, sess):
    if _is_global_admin(user) or _can_access_session(user, sess):
        return True
    return bool(sess and sess.campaign and _campaign_visible_to_user(user, sess.campaign))


def _session_duration_seconds(sess):
    if not sess or not sess.started_at or not sess.submitted_at:
        return None
    return int((sess.submitted_at - sess.started_at).total_seconds())


def _session_summary_dict(sess, include_answers=False):
    d = sess.to_dict(include_answers=include_answers)
    d['duration_seconds'] = _session_duration_seconds(sess)
    d['answer_count'] = ExamAnswer.query.filter_by(session_id=sess.id).count()
    return d


def _campaign_stats(sessions):
    submitted = [s for s in sessions if s.status in ('submitted', 'grading', 'completed')]
    passed = [s for s in submitted if s.passed]
    scores = [s.total_score or 0 for s in submitted]
    return {
        'participant_count': len(sessions),
        'submitted_count': len(submitted),
        'in_progress_count': sum(1 for s in sessions if s.status == 'in_progress'),
        'passed_count': len(passed),
        'pass_rate': round(len(passed) * 100 / len(submitted), 1) if submitted else 0,
        'avg_score': round(sum(scores) / len(scores), 1) if scores else 0,
    }


# ===== 试卷管理（仅 super_admin） =====

@api_bp.route('/exams/papers', methods=['GET'])
def list_exam_papers():
    """获取试卷列表

    查询参数：
    - status: draft/published/archived
    - category: hub_ops/testcase/review/specialty/risk/general
    - include_deleted: 1=包含已删除（仅 super_admin 有效）
    """
    user = _get_current_user()
    status = request.args.get('status')
    category = request.args.get('category')
    include_deleted = request.args.get('include_deleted') == '1'

    q = ExamPaper.query
    if not (include_deleted and _is_super_admin(user)):
        q = q.filter_by(is_deleted=False)
    # 非超管只能看 published
    if not _is_super_admin(user):
        q = q.filter_by(status='published')
    if status:
        q = q.filter_by(status=status)
    if category:
        q = q.filter_by(category=category)

    papers = q.order_by(desc(ExamPaper.created_at)).all()
    return jsonify([p.to_dict() for p in papers])


@api_bp.route('/exams/papers/<int:paper_id>', methods=['GET'])
def get_exam_paper(paper_id):
    """获取试卷详情。super_admin 看到完整答案；考生看到无答案版本。"""
    user = _get_current_user()
    paper = ExamPaper.query.get_or_404(paper_id)
    if paper.is_deleted and not _is_super_admin(user):
        return jsonify({'error': '试卷不存在'}), 404
    if paper.status != 'published' and not _is_super_admin(user):
        return jsonify({'error': '试卷未发布'}), 404

    hide_answer = not _is_super_admin(user)
    return jsonify(paper.to_dict(include_questions=True, hide_answer=hide_answer))


@api_bp.route('/exams/papers', methods=['POST'])
def create_exam_paper():
    """创建试卷（仅 super_admin）"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可创建试卷'}), 403

    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '试卷名称不能为空'}), 400

    paper = ExamPaper(
        name=name,
        description=data.get('description'),
        category=data.get('category', 'general'),
        difficulty=data.get('difficulty', 'normal'),
        total_score=int(data.get('total_score') or 100),
        pass_score=int(data.get('pass_score') or 60),
        time_limit_min=int(data.get('time_limit_min') or 60),
        applicable_skill_ids=data.get('applicable_skill_ids') or [],
        remark=data.get('remark'),
        status=data.get('status', 'draft'),
        created_by=getattr(user, 'username', None) or _examinee_display(user),
        created_by_user_id=getattr(user, 'id', None),
        created_by_claw_id=getattr(user, '_claw_id', None) or getattr(user, 'bound_claw_id', None),
    )
    db.session.add(paper)
    db.session.commit()
    return jsonify(paper.to_dict()), 201


@api_bp.route('/exams/papers/<int:paper_id>', methods=['PUT'])
def update_exam_paper(paper_id):
    """更新试卷（仅 super_admin）"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可编辑试卷'}), 403

    paper = ExamPaper.query.get_or_404(paper_id)
    data = request.get_json(force=True) or {}

    for f in ('name', 'description', 'category', 'difficulty', 'status', 'remark'):
        if f in data:
            setattr(paper, f, data[f])
    for f in ('total_score', 'pass_score', 'time_limit_min'):
        if f in data and data[f] is not None:
            setattr(paper, f, int(data[f]))
    if 'applicable_skill_ids' in data:
        paper.applicable_skill_ids = data['applicable_skill_ids'] or []

    db.session.commit()
    return jsonify(paper.to_dict())


@api_bp.route('/exams/papers/<int:paper_id>', methods=['DELETE'])
def delete_exam_paper(paper_id):
    """软删除试卷（仅 super_admin）"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可删除试卷'}), 403

    paper = ExamPaper.query.get_or_404(paper_id)
    paper.is_deleted = True
    db.session.commit()
    return jsonify({'ok': True})


@api_bp.route('/exams/papers/<int:paper_id>/publish', methods=['POST'])
def publish_exam_paper(paper_id):
    """发布试卷"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可发布试卷'}), 403

    paper = ExamPaper.query.get_or_404(paper_id)
    qcnt = ExamQuestion.query.filter_by(paper_id=paper_id, is_deleted=False).count()
    if qcnt == 0:
        return jsonify({'error': '试卷没有题目，无法发布'}), 400

    paper.status = 'published'
    db.session.commit()
    return jsonify(paper.to_dict())


# ===== 题目管理（仅 super_admin） =====

@api_bp.route('/exams/papers/<int:paper_id>/questions', methods=['GET'])
def list_exam_questions(paper_id):
    """获取试卷下所有题目（超管含答案，普通用户隐藏）"""
    user = _get_current_user()
    paper = ExamPaper.query.get_or_404(paper_id)
    hide_answer = not _is_super_admin(user)
    if not _is_super_admin(user) and (paper.is_deleted or paper.status != 'published'):
        return jsonify({'error': '试卷不可见'}), 404

    qs = ExamQuestion.query.filter_by(paper_id=paper_id, is_deleted=False)\
        .order_by(ExamQuestion.order_index, ExamQuestion.id).all()
    return jsonify([q.to_dict(hide_answer=hide_answer) for q in qs])


@api_bp.route('/exams/papers/<int:paper_id>/questions', methods=['POST'])
def create_exam_question(paper_id):
    """新增题目"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可添加题目'}), 403

    paper = ExamPaper.query.get_or_404(paper_id)
    data = request.get_json(force=True) or {}

    qtype = data.get('type')
    if qtype not in ('single', 'multiple', 'judge', 'fill', 'essay',
                     'api_op', 'case_design', 'scenario'):
        return jsonify({'error': f'不支持的题型 {qtype}'}), 400
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': '题目标题不能为空'}), 400

    # 自动设 order_index 为最大 + 1
    max_order = db.session.query(db.func.max(ExamQuestion.order_index))\
        .filter_by(paper_id=paper_id).scalar() or 0

    q = ExamQuestion(
        paper_id=paper_id,
        order_index=data.get('order_index', max_order + 1),
        type=qtype,
        title=title,
        description=data.get('description'),
        options=data.get('options'),
        points=int(data.get('points') or 10),
        standard_answer=data.get('standard_answer'),
        grading_criteria=data.get('grading_criteria'),
        auto_grade_script=data.get('auto_grade_script'),
        skill_tag=data.get('skill_tag'),
    )
    db.session.add(q)
    db.session.commit()
    return jsonify(q.to_dict(hide_answer=False)), 201


@api_bp.route('/exams/questions/<int:question_id>', methods=['PUT'])
def update_exam_question(question_id):
    """更新题目"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可编辑题目'}), 403

    q = ExamQuestion.query.get_or_404(question_id)
    data = request.get_json(force=True) or {}

    for f in ('title', 'description', 'type', 'skill_tag',
              'grading_criteria', 'auto_grade_script'):
        if f in data:
            setattr(q, f, data[f])
    for f in ('order_index', 'points'):
        if f in data and data[f] is not None:
            setattr(q, f, int(data[f]))
    for f in ('options', 'standard_answer'):
        if f in data:
            setattr(q, f, data[f])

    db.session.commit()
    return jsonify(q.to_dict(hide_answer=False))


@api_bp.route('/exams/questions/<int:question_id>', methods=['DELETE'])
def delete_exam_question(question_id):
    """软删除题目"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可删除题目'}), 403

    q = ExamQuestion.query.get_or_404(question_id)
    q.is_deleted = True
    db.session.commit()
    return jsonify({'ok': True})


# ===== 考试流程 =====

@api_bp.route('/exams/campaigns', methods=['GET'])
def list_exam_campaigns():
    """列出当前用户可见的考试场次。"""
    user = _get_current_user()
    if not user:
        return jsonify({'error': '请登录'}), 401
    q = ExamCampaign.query
    paper_id = request.args.get('paper_id', type=int)
    if paper_id:
        q = q.filter_by(paper_id=paper_id)
    rows = q.order_by(desc(ExamCampaign.created_at)).limit(200).all()
    return jsonify([c.to_dict() for c in rows if _campaign_visible_to_user(user, c)])


@api_bp.route('/exams/campaigns/<int:campaign_id>', methods=['GET'])
def get_exam_campaign(campaign_id):
    """考试场次详情：含试卷、统计、各考生作答摘要。"""
    user = _get_current_user()
    if not user:
        return jsonify({'error': '请登录'}), 401
    campaign = ExamCampaign.query.get_or_404(campaign_id)
    if not _campaign_visible_to_user(user, campaign):
        return jsonify({'error': '无权查看该考试'}), 403

    include_answers = request.args.get('include_answers') == '1'
    paper = campaign.paper
    sessions = ExamSession.query.filter_by(campaign_id=campaign_id)\
        .order_by(desc(ExamSession.submitted_at), desc(ExamSession.started_at)).all()
    visible_sessions = [s for s in sessions if _can_view_session(user, s)]

    hide_answer = not _is_global_admin(user)
    result = campaign.to_dict()
    if paper:
        result['paper'] = paper.to_dict(include_questions=True, hide_answer=hide_answer)
    result['sessions'] = [
        _session_summary_dict(s, include_answers=include_answers) for s in visible_sessions
    ]
    result['stats'] = _campaign_stats(visible_sessions)
    return jsonify(result)


@api_bp.route('/exams/campaigns', methods=['POST'])
def create_exam_campaign():
    """发起一场考试。"""
    user = _get_current_user()
    if not user:
        return jsonify({'error': '请登录或提供 Bearer Token'}), 401
    data = request.get_json(force=True) or {}
    paper_id = data.get('paper_id')
    if not paper_id:
        return jsonify({'error': 'paper_id 必填'}), 400
    paper = ExamPaper.query.get_or_404(int(paper_id))
    if paper.is_deleted or paper.status != 'published':
        return jsonify({'error': '试卷未发布，不能发起考试'}), 400

    scope = data.get('scope') or 'personal'
    project_id = data.get('project_id')
    if project_id not in (None, ''):
        project_id = int(project_id)
    else:
        project_id = None
    if not can_launch_campaign(user, scope, project_id=project_id):
        return jsonify({'error': '无权发起该范围的考试'}), 403
    if scope == 'project' and not Project.query.get(project_id):
        return jsonify({'error': '项目不存在'}), 400

    starts_at, err = _parse_datetime(data.get('starts_at'), 'starts_at')
    if err:
        return jsonify({'error': err}), 400
    ends_at, err = _parse_datetime(data.get('ends_at'), 'ends_at')
    if err:
        return jsonify({'error': err}), 400
    if not ends_at:
        return jsonify({'error': 'ends_at 必填'}), 400
    if starts_at and starts_at >= ends_at:
        return jsonify({'error': '结束时间必须晚于开始时间'}), 400

    target_claw_ids = data.get('target_claw_ids') or []
    if scope == 'personal':
        owner = getattr(user, 'username', None)
        claw = _current_claw(user)
        if not target_claw_ids:
            if claw:
                target_claw_ids = [claw.id]
            elif owner:
                target_claw_ids = [
                    c.id for c in OpenClawInstance.query.filter_by(owner=owner)
                    .filter(OpenClawInstance.status != 'deleted').all()
                ]
        allowed_ids = {
            c.id for c in OpenClawInstance.query.filter_by(owner=owner)
            .filter(OpenClawInstance.status != 'deleted').all()
        } if owner else set()
        if claw:
            allowed_ids.add(claw.id)
        target_claw_ids = [int(x) for x in target_claw_ids if int(x) in allowed_ids]
        if not target_claw_ids:
            return jsonify({'error': '个人考试至少需要一个名下 Agent'}), 400
    else:
        target_claw_ids = []

    claw = _current_claw(user)
    campaign = ExamCampaign(
        paper_id=paper.id,
        name=(data.get('name') or f'{paper.name} 考试').strip(),
        scope=scope,
        project_id=project_id if scope == 'project' else None,
        target_claw_ids=target_claw_ids,
        starts_at=starts_at,
        ends_at=ends_at,
        status='scheduled',
        remark=data.get('remark') or '',
        launched_by_user_id=getattr(user, 'id', None),
        launched_by_claw_id=claw.id if claw else None,
        launched_by_display=_examinee_display(user),
    )
    db.session.add(campaign)
    db.session.commit()
    return jsonify(campaign.to_dict()), 201


@api_bp.route('/exams/campaigns/<int:campaign_id>/cancel', methods=['POST'])
def cancel_exam_campaign(campaign_id):
    user = _get_current_user()
    campaign = ExamCampaign.query.get_or_404(campaign_id)
    if not _campaign_visible_to_user(user, campaign):
        return jsonify({'error': '无权操作该考试'}), 403
    if not (_is_global_admin(user)
            or campaign.launched_by_user_id == getattr(user, 'id', None)
            or (campaign.launched_by_claw_id and _current_claw(user)
                and campaign.launched_by_claw_id == _current_claw(user).id)):
        return jsonify({'error': '只有发起人可取消考试'}), 403
    campaign.status = 'cancelled'
    db.session.commit()
    return jsonify(campaign.to_dict())


@api_bp.route('/exams/campaigns/<int:campaign_id>/start', methods=['POST'])
def start_exam_campaign(campaign_id):
    campaign = ExamCampaign.query.get_or_404(campaign_id)
    return _start_exam_for_campaign(campaign)

@api_bp.route('/exams/papers/<int:paper_id>/start', methods=['POST'])
def start_exam(paper_id):
    """开始考试 — 创建一个 ExamSession

    POST body 可选：
    - examinee_claw_id (super_admin 代为开考时使用)
    """
    paper = ExamPaper.query.get_or_404(paper_id)
    if paper.is_deleted or paper.status != 'published':
        return jsonify({'error': '试卷不可考'}), 400
    campaign = ExamCampaign.query.filter_by(paper_id=paper_id)\
        .order_by(desc(ExamCampaign.created_at)).first()
    if campaign:
        return _start_exam_for_campaign(campaign)
    return _start_exam_for_campaign(None, paper=paper)


def _start_exam_for_campaign(campaign, paper=None):
    user = _get_current_user()
    if not user:
        return jsonify({'error': '请登录或提供 Bearer Token'}), 401
    paper = paper or campaign.paper
    if campaign:
        if not _campaign_visible_to_user(user, campaign):
            return jsonify({'error': '无权参加该考试'}), 403
        if not campaign_is_open(campaign):
            return jsonify({'error': '考试未开始或已结束，不能参与'}), 400

    # 确定考生身份
    examinee_claw_id = None
    examinee_user_id = None
    examinee_display = _examinee_display(user)

    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        examinee_claw_id = bound_claw_id
    elif getattr(user, 'id', None) and not getattr(user, '_claw_name', None):
        examinee_user_id = user.id
    if getattr(user, '_claw_name', None):
        # admin claw 直接走 token，按 claw 名映射 OpenClaw
        examinee_display = user._claw_name
        c = OpenClawInstance.query.filter_by(name=user._claw_name).first()
        if c:
            examinee_claw_id = c.id
    examinee_claw = OpenClawInstance.query.get(examinee_claw_id) if examinee_claw_id else None
    if campaign and examinee_claw and not agent_can_participate(campaign, examinee_claw):
        return jsonify({'error': '当前 Agent 不在本场考试范围内'}), 403

    q = ExamSession.query.filter_by(
        paper_id=paper.id,
        campaign_id=campaign.id if campaign else None,
        examinee_claw_id=examinee_claw_id,
        examinee_user_id=examinee_user_id,
    )
    existing = q.first()
    if existing:
        if existing.status != 'in_progress':
            return jsonify({'error': '本场考试已提交，不能重复参加'}), 400
        return jsonify(existing.to_dict(include_answers=True))

    deadline = None
    if campaign:
        deadline = campaign.ends_at
    elif paper.time_limit_min and paper.time_limit_min > 0:
        deadline = datetime.now() + timedelta(minutes=paper.time_limit_min)

    sess = ExamSession(
        paper_id=paper.id,
        campaign_id=campaign.id if campaign else None,
        examinee_claw_id=examinee_claw_id,
        examinee_user_id=examinee_user_id,
        examinee_display=examinee_display,
        deadline_at=deadline,
        status='in_progress',
    )
    db.session.add(sess)
    db.session.commit()

    # 返回 session + 题目（无答案）
    d = sess.to_dict()
    d['paper'] = paper.to_dict(include_questions=True, hide_answer=True)
    return jsonify(d), 201


@api_bp.route('/exams/sessions/<int:session_id>', methods=['GET'])
def get_exam_session(session_id):
    """获取会话详情。考生只能看自己的；super_admin 看所有。"""
    user = _get_current_user()
    sess = ExamSession.query.get_or_404(session_id)
    if not _can_view_session(user, sess):
        return jsonify({'error': '无权查看'}), 403

    d = sess.to_dict(include_answers=True)
    paper = sess.paper
    if paper:
        # 已完成时返回标准答案
        hide_answer = sess.status not in ('completed', 'submitted', 'grading') \
            and not _is_super_admin(user)
        d['paper'] = paper.to_dict(include_questions=True, hide_answer=hide_answer)
    return jsonify(d)


def _can_access_session(user, sess):
    """判断 user 能否查看自己的 session"""
    if not user or not sess:
        return False
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if sess.examinee_claw_id and bound_claw_id and sess.examinee_claw_id == bound_claw_id:
        return True
    claw_name = getattr(user, '_claw_name', None)
    if claw_name and sess.examinee_claw_id:
        c = OpenClawInstance.query.get(sess.examinee_claw_id)
        if c and c.name == claw_name:
            return True
    if sess.examinee_user_id and getattr(user, 'id', None) == sess.examinee_user_id:
        return True
    return False


@api_bp.route('/exams/sessions/<int:session_id>/answer', methods=['POST'])
def submit_answer(session_id):
    """单题作答（可多次保存，最后一次为准）

    body:
        question_id: int
        answer_content: str | list | object
    """
    user = _get_current_user()
    sess = ExamSession.query.get_or_404(session_id)
    if not _can_access_session(user, sess) and not _is_super_admin(user):
        return jsonify({'error': '无权答题'}), 403
    if sess.status != 'in_progress':
        return jsonify({'error': f'会话已 {sess.status}，不可答题'}), 400
    if sess.campaign and not campaign_is_open(sess.campaign):
        sess.status = 'expired'
        db.session.commit()
        return jsonify({'error': '考试已结束，无法答题'}), 400
    if sess.deadline_at and datetime.now() > sess.deadline_at:
        sess.status = 'expired'
        db.session.commit()
        return jsonify({'error': '已超时，无法答题'}), 400

    data = request.get_json(force=True) or {}
    qid = data.get('question_id')
    content = data.get('answer_content')
    if qid is None:
        return jsonify({'error': 'question_id 必填'}), 400
    q = ExamQuestion.query.filter_by(id=qid, paper_id=sess.paper_id, is_deleted=False).first()
    if not q:
        return jsonify({'error': '题目不存在或不属于本卷'}), 400

    # 序列化答案
    if not isinstance(content, str):
        content_str = json.dumps(content, ensure_ascii=False)
    else:
        content_str = content

    ans = ExamAnswer.query.filter_by(session_id=session_id, question_id=qid).first()
    if not ans:
        ans = ExamAnswer(session_id=session_id, question_id=qid)
        db.session.add(ans)
    ans.answer_content = content_str
    ans.answered_at = datetime.now()
    db.session.commit()
    return jsonify(ans.to_dict())


@api_bp.route('/exams/sessions/<int:session_id>/submit', methods=['POST'])
def finalize_session(session_id):
    """交卷：自动判分客观题，主观题置 None 等待评审"""
    user = _get_current_user()
    sess = ExamSession.query.get_or_404(session_id)
    if not _can_access_session(user, sess) and not _is_super_admin(user):
        return jsonify({'error': '无权交卷'}), 403
    if sess.status != 'in_progress':
        return jsonify({'error': f'会话已 {sess.status}'}), 400
    if sess.campaign and not campaign_is_open(sess.campaign):
        sess.status = 'expired'
        db.session.commit()
        return jsonify({'error': '考试已结束，无法交卷'}), 400

    paper = sess.paper
    questions = ExamQuestion.query.filter_by(paper_id=paper.id, is_deleted=False).all()
    qmap = {q.id: q for q in questions}

    answers = ExamAnswer.query.filter_by(session_id=session_id).all()
    auto_total = 0
    has_subjective_pending = False

    for a in answers:
        q = qmap.get(a.question_id)
        if not q:
            continue
        score, is_objective = _auto_grade(q, a.answer_content)
        if is_objective:
            a.auto_score = score
            a.final_score = score
            auto_total += score
        else:
            a.auto_score = None
            has_subjective_pending = True
    db.session.flush()

    sess.submitted_at = datetime.now()
    sess.auto_score = auto_total
    sess.manual_score = 0
    sess.total_score = auto_total
    sess.passed = bool(auto_total >= (paper.pass_score or 0)) if not has_subjective_pending else False
    sess.status = 'grading' if has_subjective_pending else 'completed'

    db.session.commit()
    return jsonify(sess.to_dict(include_answers=True))


@api_bp.route('/exams/sessions/<int:session_id>', methods=['DELETE'])
def delete_exam_session(session_id):
    """删除提交/作答记录，允许该 Agent 重新参加同一场考试。"""
    user = _get_current_user()
    sess = ExamSession.query.get_or_404(session_id)
    if not _can_delete_session(user, sess):
        return jsonify({'error': '无权删除该考试记录'}), 403
    ExamPeerReview.query.filter_by(session_id=session_id).delete()
    ExamAnswer.query.filter_by(session_id=session_id).delete()
    db.session.delete(sess)
    db.session.commit()
    return jsonify({'ok': True})


def _auto_grade(question, answer_content):
    """自动判分。

    Returns: (score, is_objective)
    """
    qtype = (question.type or '').lower()
    pts = int(question.points or 0)
    sa = question.standard_answer

    # 解析答案
    try:
        ans_obj = json.loads(answer_content) if answer_content else None
    except Exception:
        ans_obj = answer_content

    if qtype == 'single':
        # 标准答案是 "A" / 单个 key
        if isinstance(sa, str) and isinstance(ans_obj, str) and sa.strip().upper() == ans_obj.strip().upper():
            return pts, True
        return 0, True

    if qtype == 'multiple':
        # 标准答案是 ["A","C"]，全选对才得分
        if not isinstance(sa, (list, tuple)):
            return 0, True
        if not isinstance(ans_obj, (list, tuple)):
            return 0, True
        sa_set = {str(x).upper().strip() for x in sa}
        ans_set = {str(x).upper().strip() for x in ans_obj}
        if sa_set == ans_set:
            return pts, True
        return 0, True

    if qtype == 'judge':
        # 标准答案 true/false
        sa_norm = str(sa).strip().lower() in ('true', '1', 'yes', '对', '是')
        ans_norm = str(ans_obj).strip().lower() in ('true', '1', 'yes', '对', '是')
        return (pts if sa_norm == ans_norm else 0), True

    if qtype == 'fill':
        # 标准答案是 "xxx" 或 ["xxx","yyy"] 任一匹配（不分大小写、去空白）
        if isinstance(sa, str):
            sa_list = [sa]
        elif isinstance(sa, (list, tuple)):
            sa_list = list(sa)
        else:
            return 0, True
        a_norm = str(ans_obj).strip().lower()
        for s in sa_list:
            if str(s).strip().lower() == a_norm:
                return pts, True
        return 0, True

    # 其他题型走主观判分
    return 0, False


# ===== 考试会话查询 =====

@api_bp.route('/exams/sessions', methods=['GET'])
def list_exam_sessions():
    """会话列表

    查询参数：
    - paper_id: int
    - examinee_claw_id: int
    - status: str
    - mine: 1=只看自己的
    """
    user = _get_current_user()
    q = ExamSession.query

    paper_id = request.args.get('paper_id', type=int)
    if paper_id:
        q = q.filter_by(paper_id=paper_id)
    campaign_id = request.args.get('campaign_id', type=int)
    if campaign_id:
        q = q.filter_by(campaign_id=campaign_id)
    status_arg = request.args.get('status')
    if status_arg:
        q = q.filter_by(status=status_arg)
    examinee_claw_id = request.args.get('examinee_claw_id', type=int)
    if examinee_claw_id:
        q = q.filter_by(examinee_claw_id=examinee_claw_id)

    mine_only = request.args.get('mine') == '1'
    if mine_only:
        bound_claw_id = getattr(user, 'bound_claw_id', None)
        claw_name = getattr(user, '_claw_name', None)
        if claw_name:
            c = OpenClawInstance.query.filter_by(name=claw_name).first()
            if c:
                q = q.filter_by(examinee_claw_id=c.id)
        elif bound_claw_id:
            q = q.filter_by(examinee_claw_id=bound_claw_id)
        elif getattr(user, 'id', None):
            q = q.filter_by(examinee_user_id=user.id)

    sessions = q.order_by(desc(ExamSession.started_at)).limit(200).all()
    if not mine_only and not _is_global_admin(user):
        sessions = [s for s in sessions if _can_view_session(user, s)]
    return jsonify([s.to_dict() for s in sessions])


# ===== 同行互评 =====

@api_bp.route('/exams/sessions/<int:session_id>/peer-reviews', methods=['POST'])
def assign_peer_reviewers(session_id):
    """超管邀请 reviewer 评卷

    body:
        reviewer_claw_ids: [int]
    """
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员可分配评审'}), 403

    sess = ExamSession.query.get_or_404(session_id)
    data = request.get_json(force=True) or {}
    reviewer_claw_ids = data.get('reviewer_claw_ids') or []

    created = []
    for rcid in reviewer_claw_ids:
        # 不能自评
        if sess.examinee_claw_id and int(rcid) == int(sess.examinee_claw_id):
            continue
        existing = ExamPeerReview.query.filter_by(
            session_id=session_id, reviewer_claw_id=int(rcid)).first()
        if existing:
            continue
        c = OpenClawInstance.query.get(int(rcid))
        pr = ExamPeerReview(
            session_id=session_id,
            reviewer_claw_id=int(rcid),
            reviewer_display=c.name if c else f'claw#{rcid}',
            status='pending',
        )
        db.session.add(pr)
        created.append(pr)
    db.session.commit()
    return jsonify({'created': len(created), 'items': [p.to_dict() for p in created]})


@api_bp.route('/exams/answers/<int:answer_id>/peer-score', methods=['POST'])
def submit_peer_score(answer_id):
    """同行评分 — 给某条答案打分

    body:
        score: int (0..points)
        comment: str
    """
    user = _get_current_user()
    if not user:
        return jsonify({'error': '请登录'}), 401

    ans = ExamAnswer.query.get_or_404(answer_id)
    sess = ExamSession.query.get(ans.session_id)
    if not sess:
        return jsonify({'error': '会话不存在'}), 404

    # 验证 reviewer 身份：必须在 peer_reviews 中（或 super_admin）
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    claw_name = getattr(user, '_claw_name', None)
    reviewer_claw_id = bound_claw_id
    if claw_name:
        c = OpenClawInstance.query.filter_by(name=claw_name).first()
        if c:
            reviewer_claw_id = c.id

    is_super = _is_super_admin(user)
    pr = None
    if reviewer_claw_id:
        pr = ExamPeerReview.query.filter_by(
            session_id=sess.id, reviewer_claw_id=reviewer_claw_id).first()
    if not pr and not is_super:
        return jsonify({'error': '你未被分配评审此卷'}), 403

    data = request.get_json(force=True) or {}
    score = int(data.get('score') or 0)
    comment = data.get('comment') or ''

    # 更新 peer_scores
    peer_scores = list(ans.peer_scores or [])
    # 同一 reviewer 多次提交：覆盖
    reviewer_key = reviewer_claw_id or f'user:{getattr(user, "id", "")}'
    peer_scores = [p for p in peer_scores if p.get('reviewer_key') != reviewer_key]
    peer_scores.append({
        'reviewer_key': reviewer_key,
        'reviewer_claw_id': reviewer_claw_id,
        'reviewer_display': _examinee_display(user),
        'score': score,
        'comment': comment,
        'at': str(datetime.now()),
    })
    ans.peer_scores = peer_scores

    if pr:
        pr.status = 'completed'
        pr.completed_at = datetime.now()

    # 重新计算 final_score：取所有 peer_scores 的平均
    if peer_scores:
        avg = sum(int(p.get('score') or 0) for p in peer_scores) / len(peer_scores)
        ans.final_score = int(round(avg))

    db.session.commit()

    # 累计 manual_score
    _recalc_session_score(sess)

    return jsonify(ans.to_dict())


def _recalc_session_score(sess):
    """根据所有 answer.final_score 重新计算 session 总分"""
    answers = ExamAnswer.query.filter_by(session_id=sess.id).all()
    qids = {a.question_id for a in answers}
    questions = ExamQuestion.query.filter(ExamQuestion.id.in_(qids)).all() if qids else []
    qmap = {q.id: q for q in questions}

    auto_total = 0
    manual_total = 0
    has_pending = False
    for a in answers:
        q = qmap.get(a.question_id)
        if not q:
            continue
        if a.auto_score is not None:
            auto_total += int(a.auto_score or 0)
        else:
            if a.peer_scores:
                manual_total += int(a.final_score or 0)
            else:
                has_pending = True

    sess.auto_score = auto_total
    sess.manual_score = manual_total
    sess.total_score = auto_total + manual_total
    paper = sess.paper
    if paper:
        sess.passed = bool(sess.total_score >= (paper.pass_score or 0))
    if not has_pending and sess.status == 'grading':
        sess.status = 'completed'
    db.session.commit()


@api_bp.route('/exams/sessions/<int:session_id>/recalc', methods=['POST'])
def force_recalc_session(session_id):
    """超管强制重算分数"""
    user = _get_current_user()
    if not _is_super_admin(user):
        return jsonify({'error': '仅超级管理员'}), 403
    sess = ExamSession.query.get_or_404(session_id)
    _recalc_session_score(sess)
    return jsonify(sess.to_dict(include_answers=True))


# ===== 统计 =====

@api_bp.route('/exams/stats', methods=['GET'])
def exam_stats():
    """全局考试统计（超管看全量，普通用户看自己）"""
    user = _get_current_user()
    is_super = _is_super_admin(user)

    paper_count = ExamPaper.query.filter_by(is_deleted=False).count()
    published = ExamPaper.query.filter_by(is_deleted=False, status='published').count()
    sess_q = ExamSession.query
    if not is_super:
        bound_claw_id = getattr(user, 'bound_claw_id', None)
        claw_name = getattr(user, '_claw_name', None)
        if claw_name:
            c = OpenClawInstance.query.filter_by(name=claw_name).first()
            if c:
                sess_q = sess_q.filter_by(examinee_claw_id=c.id)
        elif bound_claw_id:
            sess_q = sess_q.filter_by(examinee_claw_id=bound_claw_id)
        elif getattr(user, 'id', None):
            sess_q = sess_q.filter_by(examinee_user_id=user.id)

    total_sessions = sess_q.count()
    completed = sess_q.filter(ExamSession.status.in_(['completed', 'submitted'])).count()
    passed = sess_q.filter_by(passed=True).count()

    return jsonify({
        'paper_count': paper_count,
        'published_count': published,
        'session_total': total_sessions,
        'session_completed': completed,
        'session_passed': passed,
        'pass_rate': round(passed / completed * 100, 1) if completed else 0,
    })
