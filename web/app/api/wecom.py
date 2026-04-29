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
import requests

from app.api import api_bp
from app import db
from app.models import OpenClawInstance, SystemConfig, WecomSendLog


# ====== micro-cloud (龙虾王) goApi 入口 ======
GOAPI_RTX_URL = 'http://9.134.11.169:8080/goApi/sendRTXInfo'
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
            return False, f"HTTP {r.status_code}: {r.text[:300]}"
        body = r.json()
        if body.get('code') != 20000:
            return False, f"goApi code={body.get('code')}: {body.get('message', '')}"
        return True, ''
    except Exception as e:
        return False, f"sendRTXInfo 异常: {type(e).__name__}: {e}"


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
            return False, f"group_robot: {body}"
        return True, ''
    except Exception as e:
        return False, f"group_robot 异常: {type(e).__name__}: {e}"


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
        )
        db.session.add(log)
        db.session.flush()  # 拿 log.id

        ok = False
        errors = []

        # 策略 1: 私聊（如果有 target_userid）
        if target_userid:
            ok, err = _send_via_rtx_info(target_userid, title, content)
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
                ok2, err2 = _send_via_group_robot(webhook, title, content, target_userid)
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

        if commit:
            db.session.commit()
        return ok, log


# ===================== HTTP 接口 =====================

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

    # 没传目标，用 claw_id 反查 owner 的企微 ID
    if not target_userid and claw_id:
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
    )
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
