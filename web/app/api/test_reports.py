"""全局测试报告中心 API（MEMORY #134）

路由树：
  GET    /api/v1/test-reports/types                         报告类型/风险等级字典（前端 tab 用）
  GET    /api/v1/test-reports                               列表（按 project/iteration/type/risk/search 筛选 + 分页）
  POST   /api/v1/test-reports                               创建
  GET    /api/v1/test-reports/<id>                          详情（含 content）
  PUT    /api/v1/test-reports/<id>                          更新
  DELETE /api/v1/test-reports/<id>                          软删
  POST   /api/v1/test-reports/<id>/share                    生成/启用分享
  DELETE /api/v1/test-reports/<id>/share                    撤销分享
  POST   /api/v1/test-reports/<id>/attachments              上传附件（multipart，10MB 上限）
  GET    /api/v1/test-reports/<id>/attachments/<aid>/download  下载附件（登录态）
  DELETE /api/v1/test-reports/<id>/attachments/<aid>        删除附件
  GET    /api/v1/test-reports/shared/<token>                公开匿名只读（PUBLIC_PATHS 放行）

权限模型：
  - 列表/详情：项目成员可见；admin/super_admin 全部可见
  - 创建/编辑/删除：作者本人 + admin/super_admin（项目 admin 限本项目）
  - 分享开关：仅作者本人 + admin/super_admin
  - 附件上传/删除：与编辑权限相同
  - 公开外链：完全匿名只读；附件下载仍需登录（用户选 anon_no_attach）
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime
from urllib.parse import quote

from flask import (
    request, jsonify, session as flask_session,
    send_from_directory, current_app, abort,
)
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from app import db
from app.models import (
    TestReport,
    TestReportAttachment,
    TEST_REPORT_TYPES,
    TEST_REPORT_RISK_LEVELS,
    TEST_REPORT_SOURCE_REF_TYPES,
    TEST_REPORT_STATUSES,
    Project,
    TestIteration,
    User,
    OpenClawInstance,
    _now,
)
from app.api import api_bp


# ============================================================
# 配置常量
# ============================================================
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB（用户需求）


def _upload_root() -> str:
    """附件根目录：优先环境变量 TEST_REPORT_UPLOAD_DIR，回退 static/test_reports/。"""
    env_dir = os.getenv('TEST_REPORT_UPLOAD_DIR')
    if env_dir:
        return env_dir
    static_dir = current_app.static_folder or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        'static',
    )
    return os.path.join(static_dir, 'test_reports')


def _report_dir(report_id: int) -> str:
    d = os.path.join(_upload_root(), str(report_id))
    os.makedirs(d, exist_ok=True)
    return d


# ============================================================
# 调用方识别 + 权限
# ============================================================
def _get_caller() -> dict | None:
    """返回当前调用方信息，统一封装 user / openclaw 两种身份。"""
    uid = flask_session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user:
            return {
                'type': 'user',
                'user_id': user.id,
                'claw_id': user.bound_claw_id,
                'name': user.display_name or user.username,
                'username': user.username,
                'role': user.role,
                'is_admin': user.role in ('super_admin', 'admin'),
                'is_super_admin': user.role == 'super_admin',
                'managed_projects': set(int(p) for p in (user.managed_projects or [])
                                        if str(p).isdigit()),
            }
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(
                OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                return {
                    'type': 'openclaw',
                    'user_id': None,
                    'claw_id': claw.id,
                    'name': claw.name,
                    'username': None,
                    'role': claw.role or 'user',
                    'is_admin': claw.role == 'admin',
                    # 全局 admin claw（无 project_id）视为 super_admin 等效
                    'is_super_admin': (claw.role == 'admin'
                                       and not claw.project_id),
                    'managed_projects': (set([int(claw.project_id)])
                                         if claw.project_id else set()),
                    'project_id': claw.project_id,
                }
    return None


def _is_author(caller: dict, report: TestReport) -> bool:
    """是否是报告作者本人（直接匹配）。"""
    if not caller:
        return False
    if (caller['type'] == 'user'
            and report.submitter_type == 'user'
            and report.submitter_user_id == caller.get('user_id')):
        return True
    if (caller['type'] == 'openclaw'
            and report.submitter_type == 'openclaw'
            and report.submitter_claw_id == caller.get('claw_id')):
        return True
    return False


def _is_owner_user(caller: dict, report: TestReport) -> bool:
    """是否是报告的"所属用户"——即:
       - 报告由 openclaw 提交时，所属用户 = claw.owner（与 caller.username 比对）
       - 报告由 user 提交时，所属用户即作者，已经被 _is_author 覆盖；这里再补一层：
         若当前调用方是 openclaw 且其 owner = report.submitter_user.username，
         视为"代为提交人"也算所属（通用兜底）
    """
    if not caller:
        return False
    if report.submitter_type == 'openclaw' and report.submitter_claw_id:
        owner_claw = OpenClawInstance.query.get(report.submitter_claw_id)
        if not owner_claw or not owner_claw.owner:
            return False
        owner_username = owner_claw.owner
        if caller['type'] == 'user' and caller.get('username') == owner_username:
            return True
        # caller 是 openclaw 且其 owner 跟报告 claw 的 owner 相同 → 同主人的兄弟 claw
        if caller['type'] == 'openclaw' and caller.get('claw_id'):
            cl = OpenClawInstance.query.get(caller['claw_id'])
            if cl and cl.owner and cl.owner == owner_username:
                return True
    elif report.submitter_type == 'user' and report.submitter_user_id:
        if caller['type'] == 'user' and caller.get('user_id') == report.submitter_user_id:
            return True
        # 调用方是 openclaw，其 owner 等于报告作者 user 的 username
        u = User.query.get(report.submitter_user_id)
        if u and caller['type'] == 'openclaw' and caller.get('claw_id'):
            cl = OpenClawInstance.query.get(caller['claw_id'])
            if cl and cl.owner and cl.owner == u.username:
                return True
    return False


def _is_project_admin(caller: dict, report: TestReport) -> bool:
    """是否是该报告所属项目的 admin（或全局 admin claw）。"""
    if not caller:
        return False
    if not caller.get('is_admin'):
        return False
    if not caller.get('managed_projects'):
        return True  # 全局 admin / 全局 admin claw 视同 super_admin
    return report.project_id in caller['managed_projects']


def _can_edit(caller: dict | None, report: TestReport) -> bool:
    """允许编辑（含状态切换、附件、删除）：
       super_admin / 龙虾王 / 报告作者 / 所属用户 / 项目 admin 及其 agent。
    """
    if not caller:
        return False
    if caller.get('is_super_admin'):
        return True
    if _is_project_admin(caller, report):
        return True
    if _is_author(caller, report):
        return True
    if _is_owner_user(caller, report):
        return True
    return False


def _can_view(caller: dict | None, report: TestReport) -> bool:
    """可见性受 status 影响（MEMORY #134.C）：
       - published：项目内所有人可见（最宽松："项目内可见可读"原始语义）
       - draft / revised / abandoned：仅 _can_edit 名单（作者、所属用户、项目 admin、super_admin）可见
    """
    if not caller:
        return False
    status = report.status or 'draft'
    if status == 'published':
        # 项目内可见——这里取最宽松的口径：登录态用户/已认证 claw 默认可见
        # 严格按项目过滤的话，改成判断 managed_projects/claw.project_id 即可
        if caller.get('is_super_admin') or caller.get('is_admin'):
            return True
        if caller['type'] == 'openclaw' and caller.get('project_id') == report.project_id:
            return True
        # user 默认全部可见
        if caller['type'] == 'user':
            return True
        return False
    # 草稿/修改中/已废弃：受限可见，等于编辑名单
    return _can_edit(caller, report)


# ============================================================
# 元数据
# ============================================================
@api_bp.route('/test-reports/types', methods=['GET'])
def list_test_report_types():
    """返回报告类型 + 风险等级 + 状态字典，供前端 tab 和表单使用。"""
    return jsonify({
        'types': [{'key': k, 'label': v} for k, v in TEST_REPORT_TYPES.items()],
        'risk_levels': [
            {'key': k, 'label': v} for k, v in TEST_REPORT_RISK_LEVELS.items()
        ],
        'statuses': [
            {'key': k, 'label': v} for k, v in TEST_REPORT_STATUSES.items()
        ],
        'source_ref_types': list(TEST_REPORT_SOURCE_REF_TYPES),
        'max_attachment_size': MAX_ATTACHMENT_SIZE,
    })


# ============================================================
# 列表 / 创建
# ============================================================
def _parse_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@api_bp.route('/test-reports', methods=['GET'])
def list_test_reports():
    """列表查询。

    参数：
      project_id, iteration_id, report_type, risk_level
      source_ref_type, source_ref_id  （旧入口反查用）
      search                         （标题/备注/版本名模糊）
      page, page_size                （默认 1 / 20，page_size 上限 100）
    """
    caller = _get_caller()
    q = TestReport.query.filter_by(is_deleted=False)

    # 隐藏的报告：Web 普通用户默认不显示
    # Agent（Bearer Token）、admin、super_admin 默认可见隐藏报告
    # Web 用户可传 show_hidden=1 强制查看（实际也只有 admin 会用）
    show_hidden = request.args.get('show_hidden') == '1'
    if not show_hidden:
        is_privileged = (caller and (
            caller['type'] == 'openclaw'
            or caller.get('is_admin')
            or caller.get('is_super_admin')
        ))
        if not is_privileged:
            q = q.filter(TestReport.is_hidden == False)

    project_id = _parse_int(request.args.get('project_id'))
    if project_id is not None:
        q = q.filter(TestReport.project_id == project_id)
    iteration_id = _parse_int(request.args.get('iteration_id'))
    if iteration_id is not None:
        q = q.filter(TestReport.iteration_id == iteration_id)

    report_type = (request.args.get('report_type') or '').strip()
    if report_type:
        q = q.filter(TestReport.report_type == report_type)
    risk_level = (request.args.get('risk_level') or '').strip()
    if risk_level:
        q = q.filter(TestReport.risk_level == risk_level)
    status_filter = (request.args.get('status') or '').strip()
    if status_filter and status_filter in TEST_REPORT_STATUSES:
        q = q.filter(TestReport.status == status_filter)

    source_ref_type = (request.args.get('source_ref_type') or '').strip()
    if source_ref_type:
        q = q.filter(TestReport.source_ref_type == source_ref_type)
    source_ref_id = _parse_int(request.args.get('source_ref_id'))
    if source_ref_id is not None:
        q = q.filter(TestReport.source_ref_id == source_ref_id)

    search = (request.args.get('search') or '').strip()
    if search:
        like = f'%{search}%'
        q = q.filter(or_(
            TestReport.title.like(like),
            TestReport.remark.like(like),
            TestReport.version_name.like(like),
        ))

    page = max(_parse_int(request.args.get('page'), 1), 1)
    page_size = min(max(_parse_int(request.args.get('page_size'), 20), 1), 100)
    total = q.count()
    items = (q.order_by(TestReport.created_at.desc())
             .offset((page - 1) * page_size)
             .limit(page_size)
             .all())
    # 项目过滤（普通用户场景看不到的资源直接 filter 掉，避免计数与可见性不一致）
    visible = [r for r in items if _can_view(caller, r)]
    return jsonify({
        'items': [r.to_dict(include_content=False) for r in visible],
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/test-reports', methods=['POST'])
def create_test_report():
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401

    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'title 不能为空'}), 400
    if len(title) > 200:
        return jsonify({'error': 'title 长度不能超过 200'}), 400

    project_id = _parse_int(data.get('project_id'))
    if not project_id:
        return jsonify({'error': 'project_id 必填'}), 400
    project = Project.query.get(project_id)
    if not project:
        return jsonify({'error': '项目不存在'}), 404

    report_type = (data.get('report_type') or 'other_specialized').strip()
    if report_type not in TEST_REPORT_TYPES:
        return jsonify({
            'error': f'report_type 非法，必须是 {list(TEST_REPORT_TYPES.keys())}',
        }), 400

    risk_level = (data.get('risk_level') or 'tbd').strip()
    if risk_level not in TEST_REPORT_RISK_LEVELS:
        risk_level = 'tbd'

    fmt = (data.get('format') or 'markdown').lower()
    if fmt not in ('markdown', 'html'):
        return jsonify({'error': 'format 仅支持 markdown/html'}), 400

    iteration_id = _parse_int(data.get('iteration_id'))
    iteration = None
    if iteration_id:
        iteration = TestIteration.query.get(iteration_id)
        if not iteration:
            return jsonify({'error': '迭代不存在'}), 404

    source_ref_type = (data.get('source_ref_type') or 'manual').strip()
    if source_ref_type not in TEST_REPORT_SOURCE_REF_TYPES:
        source_ref_type = 'manual'
    source_ref_id = _parse_int(data.get('source_ref_id'))

    # 状态：默认 draft；允许在创建时直接给到 published（admin 一步到位）
    status = (data.get('status') or 'draft').strip()
    if status not in TEST_REPORT_STATUSES:
        status = 'draft'

    report = TestReport(
        title=title,
        report_type=report_type,
        remark=(data.get('remark') or '').strip()[:500],
        project_id=project_id,
        iteration_id=iteration_id,
        version_name=(data.get('version_name')
                      or (iteration.version_name if iteration else '')
                      or '').strip()[:100],
        content=(data.get('content') or ''),
        format=fmt,
        risk_level=risk_level,
        status=status,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        submitter_type=caller['type'],
        submitter_user_id=caller['user_id'] if caller['type'] == 'user' else None,
        submitter_claw_id=caller['claw_id'] if caller['type'] == 'openclaw' else None,
        submitter_name=caller['name'],
        is_hidden=bool(data.get('is_hidden', False)),
    )
    db.session.add(report)
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify(report.to_dict(include_content=True)), 201


# ============================================================
# 详情 / 更新 / 软删
# ============================================================
@api_bp.route('/test-reports/<int:report_id>', methods=['GET'])
def get_test_report(report_id):
    caller = _get_caller()
    report = TestReport.query.get_or_404(report_id)
    if report.is_deleted:
        return jsonify({'error': '报告已删除'}), 404
    if not _can_view(caller, report):
        return jsonify({'error': '无权查看'}), 403
    data = report.to_dict(include_content=True)
    data['can_edit'] = _can_edit(caller, report)
    return jsonify(data)


@api_bp.route('/test-reports/<int:report_id>', methods=['PUT'])
def update_test_report(report_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    report = TestReport.query.get_or_404(report_id)
    if report.is_deleted:
        return jsonify({'error': '报告已删除'}), 404
    if not _can_edit(caller, report):
        return jsonify({'error': '无权编辑'}), 403

    data = request.get_json(silent=True) or {}
    if 'title' in data:
        t = (data['title'] or '').strip()
        if not t:
            return jsonify({'error': 'title 不能为空'}), 400
        report.title = t[:200]
    if 'report_type' in data:
        rt = (data['report_type'] or '').strip()
        if rt and rt not in TEST_REPORT_TYPES:
            return jsonify({'error': 'report_type 非法'}), 400
        if rt:
            report.report_type = rt
    if 'remark' in data:
        report.remark = (data['remark'] or '').strip()[:500]
    if 'content' in data:
        report.content = data['content'] or ''
    if 'format' in data:
        f = (data['format'] or 'markdown').lower()
        if f not in ('markdown', 'html'):
            return jsonify({'error': 'format 仅支持 markdown/html'}), 400
        report.format = f
    if 'risk_level' in data:
        rl = (data['risk_level'] or 'tbd').strip()
        report.risk_level = rl if rl in TEST_REPORT_RISK_LEVELS else 'tbd'
    if 'status' in data:
        st = (data['status'] or '').strip()
        if st and st not in TEST_REPORT_STATUSES:
            return jsonify({
                'error': f'status 非法，必须是 {list(TEST_REPORT_STATUSES.keys())}',
            }), 400
        if st:
            report.status = st
    if 'iteration_id' in data:
        iid = _parse_int(data['iteration_id'])
        if iid:
            if not TestIteration.query.get(iid):
                return jsonify({'error': '迭代不存在'}), 404
            report.iteration_id = iid
        else:
            report.iteration_id = None
    if 'version_name' in data:
        report.version_name = (data['version_name'] or '').strip()[:100]
    if 'is_hidden' in data:
        report.is_hidden = bool(data['is_hidden'])
    if 'project_id' in data:
        pid = _parse_int(data['project_id'])
        if pid:
            if not Project.query.get(pid):
                return jsonify({'error': '项目不存在'}), 404
            report.project_id = pid
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify(report.to_dict(include_content=True))


@api_bp.route('/test-reports/<int:report_id>', methods=['DELETE'])
def delete_test_report(report_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    report = TestReport.query.get_or_404(report_id)
    if not _can_edit(caller, report):
        return jsonify({'error': '无权删除'}), 403
    report.is_deleted = True
    report.deleted_at = _now()
    # 软删时同步关闭分享，避免幽灵外链
    report.is_shared = False
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'删除失败: {e}'}), 500
    return jsonify({'ok': True})


# ============================================================
# 分享外链
# ============================================================
@api_bp.route('/test-reports/<int:report_id>/share', methods=['POST'])
def enable_share(report_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    report = TestReport.query.get_or_404(report_id)
    if report.is_deleted:
        return jsonify({'error': '报告已删除'}), 404
    if not _can_edit(caller, report):
        return jsonify({'error': '无权操作分享'}), 403

    # 支持 ?refresh=1 强制生成新 token（旧 token 失效）
    refresh = request.args.get('refresh') == '1'
    if refresh:
        report.share_token = None
    if not report.share_token:
        report.generate_share_token()
    report.is_shared = True
    report.shared_at = _now()
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500

    hub = (os.environ.get('HUB_PUBLIC_URL')
           or 'https://clawteam.woa.com').rstrip('/')
    return jsonify({
        'is_shared': True,
        'share_token': report.share_token,
        'share_url': f'{hub}/r/{report.share_token}',
        'shared_at': str(report.shared_at),
    })


@api_bp.route('/test-reports/<int:report_id>/share', methods=['DELETE'])
def revoke_share(report_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    report = TestReport.query.get_or_404(report_id)
    if not _can_edit(caller, report):
        return jsonify({'error': '无权操作分享'}), 403
    report.is_shared = False
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify({'ok': True, 'is_shared': False})


@api_bp.route('/test-reports/shared/<token>', methods=['GET'])
def get_shared_report(token):
    """公开匿名只读访问。已在 PUBLIC_PATHS 放行。"""
    if not token or len(token) < 8:
        return jsonify({'error': 'token 非法'}), 404
    report = TestReport.query.filter_by(
        share_token=token, is_shared=True, is_deleted=False,
    ).first()
    if not report:
        return jsonify({'error': '分享链接无效或已撤销'}), 404
    # for_public=True：脱敏（不返回内部 id、不返回附件下载 url）
    data = report.to_dict(include_content=True, for_public=True,
                          include_attachments=True)
    data['share_token'] = token
    return jsonify(data)


# ============================================================
# 附件
# ============================================================
def _safe_filename(name: str) -> str:
    """保留中文名 + 防御性清理。secure_filename 会把中文吃掉，自己实现。"""
    name = name or 'unnamed'
    # 仅替换路径分隔符与控制字符；其他字符（含中文）保留
    name = name.replace('\\', '_').replace('/', '_').replace('\x00', '_')
    name = re.sub(r'[\r\n\t]', '_', name).strip()
    if not name:
        name = 'unnamed'
    return name[:200]


@api_bp.route('/test-reports/<int:report_id>/attachments', methods=['POST'])
def upload_attachment(report_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    report = TestReport.query.get_or_404(report_id)
    if report.is_deleted:
        return jsonify({'error': '报告已删除'}), 404
    if not _can_edit(caller, report):
        return jsonify({'error': '无权上传附件'}), 403

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '未收到文件（form field 名应为 file）'}), 400

    # 大小检查（先看 Content-Length 头快速失败，最后再校验实际写入大小）
    cl = request.content_length or 0
    if cl > MAX_ATTACHMENT_SIZE + 8192:
        return jsonify({
            'error': f'附件超过 10MB 上限，请联系管理员协助上传',
            'limit_bytes': MAX_ATTACHMENT_SIZE,
        }), 413

    safe_orig = _safe_filename(f.filename)
    # 磁盘文件名：时间戳 + 随机 8 + 扩展名 —— 避免冲突
    ext = ''
    if '.' in safe_orig:
        ext = '.' + safe_orig.rsplit('.', 1)[-1][:20]
    stored_name = f'{int(datetime.now().timestamp())}_{secrets.token_hex(4)}{ext}'

    target_dir = _report_dir(report_id)
    target_path = os.path.join(target_dir, stored_name)

    # 流式落盘，写完检查实际大小（防止 Content-Length 撒谎）
    written = 0
    with open(target_path, 'wb') as out:
        while True:
            chunk = f.stream.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_ATTACHMENT_SIZE:
                out.close()
                try:
                    os.remove(target_path)
                except OSError:
                    pass
                return jsonify({
                    'error': '附件超过 10MB 上限，请联系管理员协助上传',
                    'limit_bytes': MAX_ATTACHMENT_SIZE,
                }), 413
            out.write(chunk)

    att = TestReportAttachment(
        report_id=report_id,
        filename=safe_orig,
        stored_name=stored_name,
        size_bytes=written,
        content_type=(f.mimetype or 'application/octet-stream'),
        uploaded_by=caller['name'],
        uploaded_by_user_id=caller.get('user_id'),
        uploaded_by_claw_id=caller.get('claw_id'),
    )
    db.session.add(att)
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        # 回滚也要清理磁盘文件
        try:
            os.remove(target_path)
        except OSError:
            pass
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify(att.to_dict()), 201


@api_bp.route('/test-reports/<int:report_id>/attachments/<int:att_id>/download',
              methods=['GET'])
def download_attachment(report_id, att_id):
    """附件下载需要登录态（用户选 anon_no_attach），由 before_request 兜底鉴权。"""
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    att = TestReportAttachment.query.filter_by(
        id=att_id, report_id=report_id, is_deleted=False,
    ).first()
    if not att:
        return jsonify({'error': '附件不存在'}), 404
    report = TestReport.query.get_or_404(report_id)
    if not _can_view(caller, report):
        return jsonify({'error': '无权下载'}), 403
    target_dir = _report_dir(report_id)
    full_path = os.path.join(target_dir, att.stored_name)
    if not os.path.exists(full_path):
        return jsonify({'error': '附件文件已丢失'}), 410
    # send_from_directory 支持原文件名下载头
    resp = send_from_directory(
        target_dir, att.stored_name,
        as_attachment=True,
        download_name=att.filename,
    )
    # 中文文件名兼容
    try:
        encoded = quote(att.filename)
        resp.headers['Content-Disposition'] = (
            f"attachment; filename*=UTF-8''{encoded}"
        )
    except Exception:
        pass
    return resp


@api_bp.route('/test-reports/<int:report_id>/attachments/<int:att_id>',
              methods=['DELETE'])
def delete_attachment(report_id, att_id):
    caller = _get_caller()
    if not caller:
        return jsonify({'error': '未认证'}), 401
    report = TestReport.query.get_or_404(report_id)
    if not _can_edit(caller, report):
        return jsonify({'error': '无权删除附件'}), 403
    att = TestReportAttachment.query.filter_by(
        id=att_id, report_id=report_id,
    ).first()
    if not att:
        return jsonify({'error': '附件不存在'}), 404
    att.is_deleted = True
    # 物理删除文件，DB 软删（保留审计）
    try:
        full_path = os.path.join(_report_dir(report_id), att.stored_name)
        if os.path.exists(full_path):
            os.remove(full_path)
    except OSError:
        pass
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'error': f'删除失败: {e}'}), 500
    return jsonify({'ok': True})
