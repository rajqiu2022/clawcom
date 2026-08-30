"""企微 Hub 兜底通道（B+ 方案）

定位（重要）：
    本模块**不是**企微"真私聊"——真私聊由 OpenClaw agent 自己持有的 WeComWebhookClient
    通过 message(channel='wecom', to=...) 工具发起，那是真正的会话气泡。
    本模块是 Hub 服务端在 agent 主路径失败/未发时的"应用通知兜底"，
    走 micro-cloud sendRTXInfo（企微"应用通知"小红点，非会话气泡）。

设计要点：
1. 由 timeout_watcher (5 分钟兜底守护进程) 在以下场景调用：
     a) ClawTodoLog 提交 5 分钟仍未通知 owner（agent 没主动调 message 工具）
     b) ClawMessage 卡 5 分钟没处理（claw 离线/sidecar 异常）
2. 所有发送先落 wecom_send_logs 再投递，方便排障 / 重试 / 审计。
3. 策略：sendRTXInfo 私推优先，失败兜底群机器人（registration_pass_webhook）。
4. 对外暴露 POST /api/v1/wecom/send，sidecar 紧急排障也可用，
   但**不应**用作 todo 完成通知主路径——那是 agent 自己的责任。

历史教训（写给以后的我）：
- 不要在 todos.complete_todo 里直接调本模块发企微 —— 会和 agent 自己发的私聊重复。
  正确做法：agent 主发 + Hub 5 分钟兜底（看 services/timeout_watcher.py）。
- 不要把企微 corpid/secret 散落在每个 claw 机器上 —— 之前一改就要 8 台机器同步。
- 不要在 sidecar 的 hub_worker 里 silent-fail —— 必须落 log，让 Hub watcher 扫得到。
"""

from flask import request, jsonify, current_app
from datetime import datetime
import hashlib
import requests

from app.api import api_bp
from app import db
from app.models import (
    OpenClawInstance, SystemConfig, TestReport, WecomSendLog,
    WorkflowRun, WorkflowRunStep,
)
from app.services.workflows import evaluate_notification_authorization


# ====== micro-cloud (龙虾王) goApi 入口 ======
GOAPI_RTX_URL = 'http://your-hub-host:8080/goApi/sendRTXInfo'
GOAPI_TIMEOUT_SEC = 10


def _send_via_rtx_info(rec, title, msg):
    """走 micro-cloud sendRTXInfo 直接私聊 RTX 用户。

    Args:
        rec: 接收人英文名，多个用 ';' 分隔（micro-cloud 原生支持）
        title: 消息标题
        msg: 消息正文（纯文本，超长会被龙虾王截断）

    Returns:
        (ok: bool, error: str)
    """
    try:
        r = requests.post(
            GOAPI_RTX_URL,
            data={'rec': rec, 'title': title, 'msg': msg},
            timeout=GOAPI_TIMEOUT_SEC,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:300]}", {
                'http_status': r.status_code}
        body = r.json()
        if body.get('code') != 20000:
            return False, f"goApi code={body.get('code')}: {body.get('message', '')}", body
        return True, '', body
    except Exception as e:
        return False, f"sendRTXInfo 异常: {type(e).__name__}: {e}", {}


def _send_via_group_robot(webhook, title, content, target_userid=None):
    """兜底：私聊失败时退到群机器人，文案带 @userid 提醒"""
    try:
        text = f"【{title}】\n{content}"
        if target_userid:
            text += f"\n\n@{target_userid}"
        r = requests.post(
            webhook,
            json={'msgtype': 'markdown', 'markdown': {'content': text}},
            timeout=GOAPI_TIMEOUT_SEC,
        )
        body = r.json()
        if body.get('errcode') != 0:
            return False, f"group_robot: {body}", body
        return True, '', body
    except Exception as e:
        return False, f"group_robot 异常: {type(e).__name__}: {e}", {}


def _get_group_webhook():
    """获取兜底群机器人 webhook（可能未配置，返回空字符串）"""
    cfg = SystemConfig.query.filter_by(config_key='registration_pass_webhook').first()
    if cfg and cfg.value:
        v = cfg.value.strip()
        # SystemConfig.value 历史上既存过 JSON 字符串也存过裸 URL，做下兼容
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        return v
    return ''


class WecomDispatcher:
    """企微集中发送入口 —— 单一函数 send() 封装所有策略和留痕。"""

    @staticmethod
    def send(target_userid, title, content, *,
             claw_id=None, related_type='', related_id=None,
             workflow_run_id=None, workflow_step_id='', request_summary=None,
             template_version='', message_hash='', gate_result=None,
             commit=True):
        """发送一条企微消息，先落 wecom_send_logs 再投递。

        Args:
            target_userid: 企微/RTX 英文名，多个用 ';' 分隔；空字符串表示直接走群机器人
            title: 标题
            content: 正文
            claw_id: 来源 claw（仅用于日志关联，不参与发送）
            related_type/related_id: 关联资源（todo_complete/message_failed 等）
            commit: 调用方在 transaction 中时设 False，由调用方 commit

        Returns:
            (ok: bool, log: WecomSendLog)
        """
        title = (title or '消息')[:200]
        content = content or ''

        log = WecomSendLog(
            claw_id=claw_id,
            related_type=related_type or '',
            related_id=related_id,
            target_userid=target_userid or '',
            content=content,
            strategy='',
            status='pending',
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id or '',
            request_summary_json=request_summary or {},
            template_version=template_version or '',
            message_hash=message_hash or '',
            gate_result_json=gate_result or {},
        )
        db.session.add(log)
        db.session.flush()  # 拿 log.id

        ok = False
        errors = []
        receipt = {}

        # 策略 1: 私聊（如果有 target_userid）
        if target_userid:
            ok, err, receipt = _send_via_rtx_info(target_userid, title, content)
            log.strategy = 'direct_api'
            if ok:
                log.status = 'sent'
                log.sent_at = datetime.now()
            else:
                errors.append(f"direct_api: {err}")

        # 策略 2: 群机器人兜底（私聊失败 或 没有 target_userid）
        if not ok:
            webhook = _get_group_webhook()
            if webhook:
                ok2, err2, group_receipt = _send_via_group_robot(
                    webhook, title, content, target_userid)
                receipt = group_receipt or receipt
                log.strategy = 'group_robot' if not log.strategy else f"{log.strategy}_then_group_robot"
                if ok2:
                    ok = True
                    log.status = 'sent'
                    log.sent_at = datetime.now()
                else:
                    errors.append(f"group_robot: {err2}")
            else:
                errors.append('group_robot: SystemConfig.registration_pass_webhook 未配置')

        if not ok:
            log.status = 'failed'
            log.error = ' | '.join(errors)[:2000]
            current_app.logger.warning(
                f"[wecom] 发送失败 to={target_userid} title={title} errors={log.error}")
        log.receipt_json = receipt or {
            'status': log.status,
            'strategy': log.strategy,
            'error': log.error or '',
        }

        if commit:
            db.session.commit()
        return ok, log


# ===================== HTTP 接口 =====================


def _workflow_outputs(run_id):
    return {
        row.step_id: row.outputs_json or {}
        for row in WorkflowRunStep.query.filter_by(run_id=run_id).order_by(
            WorkflowRunStep.position.asc()).all()
        if row.outputs_json
    }


def _nested_value(value, key):
    found = None

    def visit(item):
        nonlocal found
        if not isinstance(item, dict):
            return
        if key in item and item.get(key) not in (None, ''):
            found = item.get(key)
        for child in item.values():
            if isinstance(child, dict):
                visit(child)

    visit(value)
    return found


def _workflow_wecom_gate(run_id, step_id):
    run = WorkflowRun.query.get(run_id)
    step = WorkflowRunStep.query.filter_by(
        run_id=run_id, step_id=step_id).first() if run else None
    outputs = _workflow_outputs(run_id) if run else {}
    report = None
    try:
        report_id = int(_nested_value(outputs, 'hub_report_id'))
    except (TypeError, ValueError):
        report_id = None
    if report_id:
        candidate = TestReport.query.get(report_id)
        share_url = str(_nested_value(outputs, 'share_url') or '')
        expected = '/r/%s' % candidate.share_token if candidate and candidate.share_token else ''
        if (candidate and not candidate.is_deleted and candidate.is_shared
                and candidate.project_id == run.project_id
                and expected and (share_url == expected or share_url.endswith(expected))):
            report = candidate
    gate = evaluate_notification_authorization(outputs, bool(report))
    gate.update({
        'workflow_run_found': bool(run),
        'notification_step_found': bool(step and step.step_type == 'notification'),
        'hub_report_id': report.id if report else None,
        'share_url': '/r/%s' % report.share_token if report else '',
    })
    gate['allowed'] = bool(
        gate['allowed'] and run and step and step.step_type == 'notification'
        and step.status in ('running', 'retrying'))
    if not gate['allowed']:
        gate.update({
            'notification_skipped': True,
            'wecom_sent': False,
            'skip_reason': 'business_pass_or_automation_only',
        })
    return run, step, gate

@api_bp.route('/wecom/send', methods=['POST'])
def wecom_send():
    """sidecar / 内部服务调用：代发企微消息

    请求体：
    {
      "claw_id": 5,                  // 可空，sidecar 上报自身 claw
      "target_userid": "rajqiu",     // 可空；为空时按 claw_id 查 owner_wecom_userid
      "title": "待办完成",
      "content": "...",              // 必填
      "related_type": "todo_complete",
      "related_id": 1234
    }

    返回：{ ok, log_id, strategy, status, error }
    """
    data = request.get_json(silent=True) or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content 必填'}), 400

    title = data.get('title') or '消息'
    target_userid = (data.get('target_userid') or '').strip()
    claw_id = data.get('claw_id')
    workflow_run_id = data.get('workflow_run_id')
    workflow_step_id = str(data.get('workflow_step_id') or '').strip()
    is_workflow_notification = bool(
        workflow_run_id or workflow_step_id
        or data.get('related_type') == 'workflow_notification')
    gate = {}
    if is_workflow_notification:
        try:
            workflow_run_id = int(workflow_run_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'workflow_run_id 必须是整数'}), 400
        _, gate_step, gate = _workflow_wecom_gate(
            workflow_run_id, workflow_step_id)
        if not gate.get('allowed'):
            digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
            log = WecomSendLog(
                claw_id=claw_id,
                related_type='workflow_notification',
                related_id=workflow_run_id,
                target_userid='',
                content='',
                strategy='hub_gate',
                status='skipped',
                error='business_pass_or_automation_only',
                workflow_run_id=workflow_run_id,
                workflow_step_id=workflow_step_id,
                request_summary_json={
                    'title': str(title)[:200],
                    'content_length': len(content),
                    'target': '大群2',
                },
                template_version=str(
                    data.get('template_version') or 'workflow-wecom-v1')[:64],
                message_hash=digest,
                receipt_json={},
                gate_result_json=gate,
            )
            db.session.add(log)
            if gate_step:
                gate_step.outputs_json = dict(gate_step.outputs_json or {}, **{
                    'notification_skipped': True,
                    'wecom_sent': False,
                    'skip_reason': 'business_pass_or_automation_only',
                })
            db.session.commit()
            return jsonify({
                'ok': False,
                'log_id': log.id,
                'notification_skipped': True,
                'wecom_sent': False,
                'skip_reason': 'business_pass_or_automation_only',
                'gate_result': gate,
            }), 403
        # Workflow notifications are brokered to the configured release group
        # (大群2); never downgrade them into a personal RTX notification.
        target_userid = ''

    # 没传目标，用 claw_id 反查 owner 的企微 ID
    if not is_workflow_notification and not target_userid and claw_id:
        claw = OpenClawInstance.query.get(claw_id)
        if claw and claw.owner_wecom_userid:
            target_userid = claw.owner_wecom_userid

    ok, log = WecomDispatcher.send(
        target_userid=target_userid,
        title=title,
        content=content,
        claw_id=claw_id,
        related_type=data.get('related_type', ''),
        related_id=data.get('related_id'),
        workflow_run_id=workflow_run_id if is_workflow_notification else None,
        workflow_step_id=workflow_step_id,
        request_summary={
            'title': str(title)[:200],
            'content_length': len(content),
            'target': '大群2' if is_workflow_notification else target_userid,
        },
        template_version=str(data.get('template_version') or (
            'workflow-wecom-v1' if is_workflow_notification else ''))[:64],
        message_hash=hashlib.sha256(content.encode('utf-8')).hexdigest(),
        gate_result=gate,
    )
    if is_workflow_notification:
        _, sent_step, _ = _workflow_wecom_gate(
            workflow_run_id, workflow_step_id)
        if sent_step:
            sent_step.outputs_json = dict(sent_step.outputs_json or {}, **{
                'notification_skipped': False,
                'wecom_sent': bool(ok),
                'skip_reason': '',
                'wecom_log_id': log.id,
            })
            db.session.commit()
    return jsonify({
        'ok': ok,
        'log_id': log.id,
        'strategy': log.strategy,
        'status': log.status,
        'error': log.error,
    }), (200 if ok else 502)


@api_bp.route('/wecom/send-logs', methods=['GET'])
def list_wecom_send_logs():
    """查询发送记录（管理排障用）

    Query: claw_id, status, limit (默认 50)
    """
    q = WecomSendLog.query
    claw_id = request.args.get('claw_id', type=int)
    if claw_id:
        q = q.filter_by(claw_id=claw_id)
    status = request.args.get('status')
    if status:
        q = q.filter_by(status=status)
    limit = min(request.args.get('limit', 50, type=int), 200)
    rows = q.order_by(WecomSendLog.id.desc()).limit(limit).all()
    return jsonify([r.to_dict() for r in rows])
