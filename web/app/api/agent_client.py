"""
OpenClaw 子agent插件 - Manager端
部署在 openclaw-manager 服务器上

功能：
1. 接收子agent的SSE长连接
2. 下发任务给子agent（修改配置、执行命令、更新文件等）
3. 接收子agent的任务执行结果

API设计：
- GET  /api/openclaws/<id>/events     - SSE端点，子agent连接接收任务
- POST /api/openclaws/<id>/report    - 子agent上报任务结果
- POST /api/openclaws/<id>/dispatch  - 派发任务给指定claw (Manager管理接口)
"""

import os
from datetime import datetime, date, timedelta
from flask import request, jsonify, Response, stream_with_context, Blueprint, current_app
from app import db
from app.models import (AgentPostAssignment, AgentTask, OpenClawInstance,
                        ClawMessage, ClawTodo, ClawTodoLog,
                        WorkflowDefinition, _now)
from app.services.todo_schedule import (
    TODO_TIMEZONE,
    cst_iso,
    cst_now_naive,
    todo_schedule_state,
)
from functools import wraps
import json
import time
import hashlib
import logging


def _workflow_task_deliverable(task):
    """F3：workflow_agent_task 仅在其 run/step 仍活跃(running/retrying)时才投递。

    否则(run 已 cancelled/blocked/succeeded、step 已结束) 返回 False —— 避免离线 agent
    重连后被塞入死 run 的陈旧任务、再跑一遍 LLM 空转。非 workflow 任务一律放行。
    """
    if getattr(task, 'task_type', '') != 'workflow_agent_task':
        return True
    try:
        payload = task.payload
        if isinstance(payload, str):
            payload = json.loads(payload) if payload.strip().startswith('{') else {}
        if not isinstance(payload, dict):
            return True
        run_id = payload.get('run_id')
        step_id = payload.get('step_id')
        if not run_id or not step_id:
            return True
        from app.models import WorkflowRun, WorkflowRunStep
        step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first()
        if not step or step.status not in ('running', 'retrying'):
            return False
        run = WorkflowRun.query.get(run_id)
        if not run or run.status not in ('running', 'retrying'):
            return False
        return True
    except Exception:
        # 判定异常时保守放行，不因为守护逻辑阻断正常任务
        return True


def _is_missing(v):
    """统一的"必填"判定，避免 `if not x` 把合法的整数 0 当作缺失。"""
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False
import threading

logger = logging.getLogger(__name__)

# 创建蓝图（注意：这里不再使用 api_bp 前缀，因为已在 __init__.py 中单独注册）
agent_bp = Blueprint('agent_client', __name__, url_prefix='/api/openclaws')

# 离线超时阈值（秒）：超过此时间无 last_activity 更新，标记为 offline
OFFLINE_TIMEOUT = 300  # 5 分钟

# ==================== SSE 即时通知机制 ====================
# 每个 claw_id 对应一个 threading.Event，SSE generator 等待它，
# 写入 ClawMessage 时触发它，实现即时推送而非轮询。

_claw_events: dict = {}   # claw_id -> threading.Event
_claw_events_lock = threading.Lock()

# 待办变更通知：记录有待办变更需要立即推送的 claw_id
_todo_changed_claws: set = set()
_todo_changed_lock = threading.Lock()

# SSE 建连时的离线清理是全局维护动作，不能每条长连接都批量扫库。
_stale_cleanup_lock = threading.Lock()
_last_stale_cleanup_at = 0.0

# Hermes sidecar 健康巡检（#116 经验之后的兜底）：周期性巡检 Hub 代建
# Hermes Agent 是否还有有效的 sidecar 心跳，命中告警给 admin OpenClaw。
_sidecar_audit_lock = threading.Lock()
_last_sidecar_audit_at = 0.0
_recent_sidecar_audit_alerts: dict = {}
SIDECAR_AUDIT_INTERVAL = 300         # 每 5 分钟最多巡检一次
SIDECAR_AUDIT_OFFLINE_GRACE = 600    # 心跳静默 10 分钟以上才告警
SIDECAR_AUDIT_REPEAT_COOLDOWN = 3600  # 同一 claw 1 小时内不重复告警


def _get_claw_event(claw_id: int) -> threading.Event:
    """获取指定 claw 的通知事件（不存在则创建）"""
    with _claw_events_lock:
        if claw_id not in _claw_events:
            _claw_events[claw_id] = threading.Event()
        return _claw_events[claw_id]


def notify_claw(claw_id: int):
    """通知指定 claw 的 SSE 长连接有新数据，立即唤醒"""
    evt = _get_claw_event(claw_id)
    evt.set()
    print(f"[SSE_NOTIFY] notify_claw called for claw_id={claw_id}, event.is_set={evt.is_set()}", flush=True)


def notify_claw_todo(claw_id: int):
    """通知指定 claw 的待办列表有变更，SSE 立即推送待办更新"""
    with _todo_changed_lock:
        _todo_changed_claws.add(claw_id)
    notify_claw(claw_id)


def _consume_todo_change(claw_id: int) -> bool:
    """检查并消费 claw 的待办变更标记"""
    with _todo_changed_lock:
        if claw_id in _todo_changed_claws:
            _todo_changed_claws.discard(claw_id)
            return True
        return False


def _clear_claw_event(claw_id: int):
    """SSE generator 读取完数据后重置事件"""
    with _claw_events_lock:
        evt = _claw_events.get(claw_id)
        if evt:
            evt.clear()


def _cleanup_stale_connections():
    """将 last_activity 超时的 claw 标记为 offline（防止 SSE 挂断后状态不更新）"""
    global _last_stale_cleanup_at
    now_ts = time.time()
    if now_ts - _last_stale_cleanup_at < 120:
        return
    if not _stale_cleanup_lock.acquire(blocking=False):
        return
    _last_stale_cleanup_at = now_ts
    try:
        # 使用独立 pymysql 连接且单条 UPDATE，避免多个 SSE 建连并发 ORM 查询 +
        # 批量 flush 同一批 openclaw_instances 时触发 MySQL deadlock。
        import pymysql as _pymysql
        threshold = datetime.now() - timedelta(seconds=OFFLINE_TIMEOUT)
        conn = _pymysql.connect(
            host=os.getenv('MYSQL_HOST', 'localhost'),
            port=int(os.getenv('MYSQL_PORT', '3306')),
            user=os.getenv('MYSQL_USER', 'your_mysql_user'),
            password=os.getenv('MYSQL_PASSWORD', 'your_mysql_user'),
            database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
            charset='utf8mb4',
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE openclaw_instances "
                    "SET status='offline', updated_at=NOW() "
                    "WHERE status IN ('工作','学习','摸鱼','online') "
                    "AND last_activity < %s",
                    (threshold,),
                )
                affected = cur.rowcount
            conn.commit()
            if affected:
                logger.warning("检测到 %s 个超时 OpenClaw，已标记为 offline", affected)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("清理超时 SSE 连接失败（忽略，不影响当前请求）: %s", e)
    finally:
        _stale_cleanup_lock.release()


def _release_orm_session():
    """SSE 是长连接，请求结束前不会自动 teardown，必须主动归还连接池连接。"""
    try:
        db.session.remove()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _audit_sidecar_health():
    """巡检 Hub 代建 Hermes Agent 的 sidecar 是否还有有效心跳。

    判定逻辑：claw 曾经有过 ``AgentDeployment.status='success'``（说明 Hub 代建
    生成过 sidecar 单元），但当前 ``last_activity`` 距今超过
    :data:`SIDECAR_AUDIT_OFFLINE_GRACE` 秒——视为 sidecar 异常。命中后给
    所有 ``role='admin'`` 的 OpenClaw 写一条 ``ClawMessage`` 提示巡查；
    同一 claw 在 :data:`SIDECAR_AUDIT_REPEAT_COOLDOWN` 秒内只告警一次，
    避免短期重复打扰。

    设计取舍：
      * 走 SSE 建连旁路触发（节流 5 分钟一次），不额外起后台线程，
        避免引入 gunicorn 多 worker 下重复执行；只要有任何 sidecar 还在连
        Hub，建连频率就足以覆盖巡检周期。
      * 用一条 ORM 查询定位异常 claw，再发若干 ``ClawMessage``；ORM 处
        理后立刻 ``_release_orm_session()`` 归还连接池连接，防止 SSE 长
        连接里残留 transaction。
    """
    global _last_sidecar_audit_at
    now_ts = time.time()
    if now_ts - _last_sidecar_audit_at < SIDECAR_AUDIT_INTERVAL:
        return
    if not _sidecar_audit_lock.acquire(blocking=False):
        return
    _last_sidecar_audit_at = now_ts
    try:
        from app.models import (
            OpenClawInstance,
            ClawMessage,
            AgentDeployment,
        )
        from sqlalchemy import or_ as _or

        threshold = datetime.now() - timedelta(seconds=SIDECAR_AUDIT_OFFLINE_GRACE)
        managed_claws = (
            db.session.query(AgentDeployment.openclaw_id)
            .filter(AgentDeployment.agent_type.in_(('hermes', 'codex')),
                    AgentDeployment.deploy_method == 'systemd',
                    AgentDeployment.status == 'success')
            .group_by(AgentDeployment.openclaw_id)
            .subquery()
        )
        offline_claws = (
            OpenClawInstance.query
            .filter(OpenClawInstance.id.in_(managed_claws))
            .filter(_or(OpenClawInstance.last_activity.is_(None),
                        OpenClawInstance.last_activity < threshold))
            .all()
        )
        if not offline_claws:
            return

        admin_claws = (OpenClawInstance.query
                       .filter_by(role='admin')
                       .filter(OpenClawInstance.status != 'deleted')
                       .all())
        if not admin_claws:
            logger.warning("Sidecar audit: 发现 %s 个异常 claw 但没有 admin 角色 OpenClaw 可告警",
                           len(offline_claws))
            return

        alerted_ids: list = []
        for c in offline_claws:
            last_alert = _recent_sidecar_audit_alerts.get(c.id, 0)
            if now_ts - last_alert < SIDECAR_AUDIT_REPEAT_COOLDOWN:
                continue
            _recent_sidecar_audit_alerts[c.id] = now_ts
            last_act = (c.last_activity.strftime('%Y-%m-%d %H:%M:%S')
                        if c.last_activity else 'never')
            content = (
                "[Hermes Sidecar 巡检告警] "
                f"{c.name} (claw #{c.id}) 超过 "
                f"{SIDECAR_AUDIT_OFFLINE_GRACE // 60} 分钟未上报 SSE 心跳"
                f"（last_activity={last_act}）。"
                "请在目标机检查 "
                f"openclaw-sidecar-v2-claw-{c.id}.service 状态；"
                "如缺失，可在 Hub 卡片点击「重启 Agent」自动重新生成 "
                "wrapper / sidecar.env / unit 并启动。"
            )
            for a in admin_claws:
                db.session.add(ClawMessage(
                    claw_id=a.id,
                    sender_name='Hub',
                    content=content,
                    msg_type='audit',
                    direction='to_claw',
                    status='pending',
                ))
            alerted_ids.append(c.id)

        if alerted_ids:
            db.session.commit()
            for a in admin_claws:
                notify_claw(a.id)
            logger.warning("Sidecar audit: 异常 %s 个 claw，已通知 admin（claw ids=%s）",
                           len(alerted_ids), alerted_ids)
    except Exception as e:
        logger.warning("Sidecar audit 异常（忽略，不影响请求）: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass
    finally:
        _sidecar_audit_lock.release()
        _release_orm_session()


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器 — 统一版本（防越权）"""
    from app.api.auth_utils import require_claw_token as _unified
    return _unified(f)


def _runnable_todo_payload(todo, today_log=None):
    state = todo_schedule_state(todo, today_log=today_log)
    if not state['is_due']:
        return None
    return {
        'id': todo.id,
        'title': todo.title,
        'description': todo.description or '',
        'urgency': todo.urgency_level or 'flexible',
        'schedule_time': todo.schedule_time,
        'schedule_type': todo.schedule_type,
        'task_category': todo.task_category or 'routine',
        **state,
    }


@agent_bp.route('/<int:claw_id>/events', methods=['GET'])
@require_claw_token
def claw_sse_events(claw_id, claw=None):
    """
    SSE 端点 - 子agent 长连接接收任务

    事件格式：
    - event: connected\ndata: {claw_id, name}\n\n
    - event: heartbeat\ndata: {server_time}\n\n
    - event: task\ndata: {task_id, task_type, command, target_path, payload}\n\n
    - event: message\ndata: {msg_id, content, msg_type, ...}\n\n
    - event: room_invited\ndata: {room, member}\n\n
    - event: room_message\ndata: {delivery_id, room, message}\n\n
    - event: todos_pending\ndata: {count, todos: [{id, title, urgency, ...}]}\n\n
    - event: ping\ndata: \n\n
    """
    # 清理超时的 SSE 连接（防止挂断后状态不更新）
    _cleanup_stale_connections()
    # 顺手巡检一下 Hub 代建 Hermes 的 sidecar 是否还活着；两者都有节流，
    # 不会因为多 SSE 重连而被反复触发。
    _audit_sidecar_health()

    # SSE 连接建立时设为在线
    # 重要：在 gevent 环境下，SQLAlchemy 的 db.session.commit() 可能不真正提交到 MariaDB，
    # 必须使用独立的 pymysql 连接来确保状态更新
    import pymysql as _pymysql
    _raw_conn = _pymysql.connect(
        host=os.getenv('MYSQL_HOST', 'localhost'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'your_mysql_user'),
        password=os.getenv('MYSQL_PASSWORD', 'your_mysql_user'),
        database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
        charset='utf8mb4'
    )
    try:
        with _raw_conn.cursor() as _cur:
            _cur.execute(
                "UPDATE openclaw_instances SET status='工作', last_activity=NOW() WHERE id=%s",
                (claw_id,)
            )
        _raw_conn.commit()
    finally:
        _raw_conn.close()

    # _cleanup_stale_connections / _audit_sidecar_health may remove the scoped
    # session, so the decorator-provided object can already be detached here.
    # Re-query in the current session instead of expiring a detached instance.
    claw = db.session.get(OpenClawInstance, claw_id)
    if claw is None:
        return jsonify({'error': 'OpenClaw 不存在'}), 404
    claw_name = claw.name
    logger.info(f"SSE连接建立，OpenClaw {claw_id} ({claw_name}) 状态: {claw.status}")
    _release_orm_session()

    def generate():
        # 发送连接成功事件
        yield f"event: connected\ndata: {json.dumps({'claw_id': claw_id, 'name': claw_name, 'server_time': cst_iso(), 'timezone': TODO_TIMEZONE})}\n\n"

        # === 连接建立时推送未读消息和当前待办（解决断线期间消息丢失问题）===
        try:
            # 只推送真正"未读"的消息：read_at IS NULL
            # （以前推 status in [pending, delivered]，导致每次重连重推
            #  几十条历史消息，逼客户端用 seen_msg_ids 永久去重，进而误伤新消息。
            #  现在改成读取语义：客户端真正消费完一条 message 后，必须 POST
            #  /api/openclaws/<id>/messages/<msg_id>/read，否则下次重连还会重推
            #  这一条——这样客户端不再需要本地永久去重。）
            unread_msgs = ClawMessage.query.filter(
                ClawMessage.claw_id == claw_id,
                ClawMessage.direction == 'to_claw',
                ClawMessage.read_at.is_(None),
            ).order_by(ClawMessage.created_at.asc()).limit(50).all()
            unread_payloads = []
            for msg in unread_msgs:
                if msg.status == 'pending':
                    msg.status = 'delivered'
                    msg.delivered_at = datetime.now()
                unread_payloads.append(msg.to_dict())
            if unread_msgs:
                db.session.commit()
                logger.info(
                    f"SSE连接建立，推送 {len(unread_msgs)} 条未读消息给 OpenClaw {claw_id}"
                )

            # 推送当前待办列表
            today = cst_now_naive().date()
            all_todos = ClawTodo.query.filter_by(openclaw_id=claw_id, enabled=True).all()
            today_logs = {l.todo_id: l for l in ClawTodoLog.query.filter_by(
                openclaw_id=claw_id, log_date=today).all()}
            pending_todos = []
            for t in all_todos:
                log = today_logs.get(t.id)
                item = _runnable_todo_payload(t, log)
                if item:
                    pending_todos.append(item)
            room_events = []
            if current_app.config.get('CHAT_ROOM_ENABLED', False):
                from app.services.chat_rooms import pending_agent_events
                room_events = pending_agent_events(claw_id, limit=50)
            _release_orm_session()
            for payload in unread_payloads:
                yield f"event: message\ndata: {json.dumps(payload)}\n\n"
            yield f"event: todos_pending\ndata: {json.dumps({'count': len(pending_todos), 'todos': pending_todos})}\n\n"
            for event_name, payload in room_events:
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            logger.info(f"SSE连接建立，推送 {len(pending_todos)} 个待办给 OpenClaw {claw_id}")
        except Exception as e:
            logger.error(f"SSE连接建立时推送初始数据失败: {e}")
            _release_orm_session()

        last_task_check = time.time()
        last_message_check = 0
        last_status_update = 0
        last_todo_check = 0  # 待办推送节流：上次推送待办的时间
        # 用初始推送的待办 ID 签名初始化。不能只比 count：
        # 新增一个待办同时完成/禁用另一个待办时，数量不变但内容已变。
        try:
            last_todo_count = len(pending_todos)
            last_todo_signature = tuple(t.get('id') for t in pending_todos)
        except NameError:
            last_todo_count = -1
            last_todo_signature = ()

        # 获取当前 claw 的通知事件，用于 notify_claw() 即时唤醒
        _my_event = _get_claw_event(claw_id)

        loop_count = 0
        while True:
            try:
                loop_count += 1
                if loop_count % 30 == 1:  # 每60秒打一次
                    print(f"[SSE_LOOP] claw_id={claw_id} loop={loop_count}", flush=True)

                # 强制结束当前事务并刷新 session，确保后续 ORM 查询走新事务
                # （SSE 长连接里 db.session 处于一个长事务，MySQL REPEATABLE READ
                # 隔离级别下只能看到事务开始时的 snapshot，新插入的 pending 消息
                # 在 ORM .query.filter().all() 里永远查不到 → messages 永远为空
                # → 永远不会 PUSH。必须 rollback 结束事务再 expire_all。）
                try:
                    db.session.rollback()
                except Exception:
                    pass
                db.session.expire_all()

                now = time.time()
                events_to_yield = []
                pending_msg_ids = []
                message_payloads = []

                # 检查 notify_claw() 是否触发了即时通知
                notified = _my_event.is_set()
                if notified:
                    _my_event.clear()

                # 消息轮询降频：不能每 2 秒为每条 SSE 长连接开 DB 连接，
                # 否则多个 sidecar 重连时会迅速打满 MySQL / SQLAlchemy pool。
                # 但如果 notify_claw() 触发了，立即跳过节流检查消息。
                if notified or now - last_message_check > 10:
                    # 使用独立 pymysql 连接查询消息，绕过 gevent session 缓存问题
                    # （db.session 在 gevent 长连接中会缓存旧数据，导致新消息不可见）
                    import pymysql as _pymysql
                    _mc = _pymysql.connect(
                        host=os.getenv('MYSQL_HOST', 'localhost'),
                        port=int(os.getenv('MYSQL_PORT', '3306')),
                        user=os.getenv('MYSQL_USER', 'your_mysql_user'),
                        password=os.getenv('MYSQL_PASSWORD', 'your_mysql_user'),
                        database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
                        charset='utf8mb4',
                        connect_timeout=3,
                        read_timeout=5,
                        write_timeout=5,
                    )
                    try:
                        with _mc.cursor(_pymysql.cursors.DictCursor) as _mcur:
                            # 主循环只推 status='pending' 的新消息（首屏未读消息已在
                            # connected 阶段一次性推完）。这里如果用 read_at IS NULL
                            # 会把所有"已推但未读"的也再次拉出来反复推，反而退化。
                            _mcur.execute(
                                "SELECT id FROM claw_messages WHERE claw_id=%s "
                                "AND direction='to_claw' AND status='pending' "
                                "ORDER BY created_at ASC LIMIT 10 FOR UPDATE",
                                (claw_id,),
                            )
                            pending_msg_ids = [row['id'] for row in _mcur.fetchall()]
                            if pending_msg_ids:
                                _placeholders = ','.join(['%s'] * len(pending_msg_ids))
                                # 先原子认领再 yield，避免同一 claw 多个 SSE 连接同时查到
                                # pending 后重复推送同一条消息。
                                _mcur.execute(
                                    f"UPDATE claw_messages SET status='delivered', "
                                    f"delivered_at=NOW() WHERE id IN ({_placeholders}) "
                                    f"AND status='pending'",
                                    tuple(pending_msg_ids),
                                )
                                _mc.commit()
                                if _mcur.rowcount != len(pending_msg_ids):
                                    pending_msg_ids = []
                    finally:
                        _mc.close()
                    last_message_check = now

                    # 用 SQLAlchemy 加载消息对象（session 已 expire_all，会从 DB 读最新）
                    if pending_msg_ids:
                        messages = ClawMessage.query.filter(
                            ClawMessage.id.in_(pending_msg_ids)
                        ).order_by(ClawMessage.created_at.asc()).all()
                        message_payloads = [msg.to_dict() for msg in messages]

                # 消息已在 pymysql 连接中先标记 delivered，避免多 SSE 连接重复认领。
                for payload in message_payloads:
                    events_to_yield.append(f"event: message\ndata: {json.dumps(payload)}\n\n")
                if pending_msg_ids:
                    print(
                        f"[SSE_PUSH] claw_id={claw_id} delivered "
                        f"{len(pending_msg_ids)} msgs ids={pending_msg_ids}",
                        flush=True,
                    )

                if now - last_task_check > 10:
                    # 更新活跃时间降频到 60s，避免 SSE 长连接每 10s 写库。
                    if now - last_status_update > 60:
                        import pymysql as _pymysql
                        _rc = _pymysql.connect(
                            host=os.getenv('MYSQL_HOST', 'localhost'),
                            port=int(os.getenv('MYSQL_PORT', '3306')),
                            user=os.getenv('MYSQL_USER', 'your_mysql_user'),
                            password=os.getenv('MYSQL_PASSWORD', 'your_mysql_user'),
                            database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
                            charset='utf8mb4',
                            connect_timeout=3,
                            read_timeout=5,
                            write_timeout=5,
                        )
                        try:
                            with _rc.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE openclaw_instances SET last_activity=NOW(), status='工作' WHERE id=%s AND status!='deleted'",
                                    (claw_id,)
                                )
                            _rc.commit()
                        finally:
                            _rc.close()
                        last_status_update = now

                    tasks = AgentTask.query.filter(
                        AgentTask.claw_id == claw_id,
                        AgentTask.status == 'pending'
                    ).order_by(AgentTask.created_at.asc()).limit(10).all()

                    # 发送待处理任务
                    task_events = []
                    for task in tasks:
                        # F3：workflow 任务先校验 run/step 仍活跃，否则失效、不投递
                        if not _workflow_task_deliverable(task):
                            task.status = 'failed'
                            task.error = 'workflow_run_or_step_inactive'
                            task.completed_at = datetime.now()
                            db.session.commit()
                            continue
                        task.status = 'running'
                        task.assigned_at = datetime.now()
                        db.session.commit()
                        task_events.append(f"event: task\ndata: {json.dumps(task.to_dict())}\n\n")
                    events_to_yield.extend(task_events)

                    # 聊天室事件使用独立事件名。普通聊天室消息不会创建 notify_agent
                    # 投递，因此不会无意唤醒 Agent；仅 @提及/@所有人（或房间显式
                    # all_messages 策略）会出现在这里。
                    if current_app.config.get('CHAT_ROOM_ENABLED', False):
                        from app.services.chat_rooms import pending_agent_events
                        for event_name, payload in pending_agent_events(claw_id, limit=20):
                            events_to_yield.append(
                                f"event: {event_name}\ndata: "
                                f"{json.dumps(payload, ensure_ascii=False)}\n\n")

                    # 检查待办任务变化（每 30 秒，或收到待办变更通知时立即推送）
                    has_todo_change = _consume_todo_change(claw_id)
                    if has_todo_change or now - last_todo_check > 30:
                        today = cst_now_naive().date()
                        all_todos = ClawTodo.query.filter_by(
                            openclaw_id=claw_id, enabled=True).all()
                        today_logs = {l.todo_id: l for l in ClawTodoLog.query.filter_by(
                            openclaw_id=claw_id, log_date=today).all()}

                        pending_todos = []
                        for t in all_todos:
                            log = today_logs.get(t.id)
                            item = _runnable_todo_payload(t, log)
                            if item:
                                pending_todos.append(item)

                        current_count = len(pending_todos)
                        current_signature = tuple(t.get('id') for t in pending_todos)
                        # 待办列表变化时推送，或首次连接时推送，或收到待办变更通知时立即推送。
                        # 多 worker 下 notify 事件是进程内存态，30s 轮询的 ID 签名比较是兜底。
                        if (current_signature != last_todo_signature
                                or last_todo_count == -1
                                or has_todo_change):
                            events_to_yield.append(
                                f"event: todos_pending\ndata: {json.dumps({'count': current_count, 'todos': pending_todos})}\n\n"
                            )
                            last_todo_count = current_count
                            last_todo_signature = current_signature

                        last_todo_check = now

                    last_task_check = now

                # ping 保持连接（防止 nginx/proxy 超时断开）
                # 有消息推送时跳过 ping（消息本身就是保活信号）
                _release_orm_session()
                if events_to_yield:
                    for event_text in events_to_yield:
                        yield event_text
                else:
                    yield f"event: ping\ndata: \n\n"




                # 等 2 秒，但 notify_claw() 设置 Event 时可立即唤醒
                # （gevent 多 worker 下 Event 不跨进程，2s 短轮询是兜底）
                _my_event.wait(2)




            except GeneratorExit:
                # 客户端断开连接，更新状态为 offline
                try:
                    import pymysql as _pymysql
                    _rc = _pymysql.connect(
                        host=os.getenv('MYSQL_HOST', 'localhost'),
                        port=int(os.getenv('MYSQL_PORT', '3306')),
                        user=os.getenv('MYSQL_USER', 'your_mysql_user'),
                        password=os.getenv('MYSQL_PASSWORD', 'your_mysql_user'),
                        database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
                        charset='utf8mb4'
                    )
                    try:
                        with _rc.cursor() as _cur:
                            _cur.execute(
                                "UPDATE openclaw_instances SET status='offline' WHERE id=%s",
                                (claw_id,)
                            )
                        _rc.commit()
                    finally:
                        _rc.close()
                    logger.info(f"SSE断开，OpenClaw {claw_id} ({claw_name}) 状态已设为 offline")
                except Exception as e:
                    logger.error(f"更新 claw 状态失败: {e}")
                    db.session.rollback()
                break
            except Exception as e:
                _release_orm_session()
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                break

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@agent_bp.route('/<int:claw_id>/report', methods=['POST'])
@require_claw_token
def claw_task_report(claw_id, claw=None):
    """
    子agent上报任务执行结果

    请求体：
    {
        "task_id": "xxx",
        "status": "completed/failed",
        "result": "执行结果",
        "error": "错误信息（可选）"
    }
    """
    data = request.get_json()
    if not data or _is_missing(data.get('task_id')):
        return jsonify({'error': 'task_id 为必填项'}), 400

    task = AgentTask.query.filter_by(task_id=data['task_id'], claw_id=claw_id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    task.status = data.get('status', 'completed')
    task.result = data.get('result')
    task.error = data.get('error')
    task.completed_at = datetime.now()
    db.session.commit()

    return jsonify({'status': 'ok', 'task': task.to_dict()})


# ==================== OpenClaw 消息 API ====================

@agent_bp.route('/<int:claw_id>/messages', methods=['POST'])
@require_claw_token
def claw_send_message(claw_id, claw=None):
    """
    OpenClaw 发送消息给 Web 管理端

    请求体：
    {
        "content": "消息内容",
        "msg_type": "text|task_delegate|knowledge_share|request_help",
        "reply_to": 123  // 可选，回复某条消息的ID
    }
    """
    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    msg = ClawMessage(
        claw_id=claw_id,
        from_claw_id=claw_id,
        sender_name=claw.name,
        content=data['content'],
        msg_type=data.get('msg_type', 'text'),
        direction='from_claw',
        reply_to=data.get('reply_to'),
        status='delivered',
        delivered_at=datetime.now(),
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({'status': 'ok', 'message': msg.to_dict()}), 201


@agent_bp.route('/<int:claw_id>/send-to-claw', methods=['POST'])
@require_claw_token
def claw_send_to_claw(claw_id, claw=None):
    """OpenClaw 给其他 OpenClaw 发消息（任意 claw 可互发，不能发给自己）

    请求体：
    {
        "target_claw_ids": [1, 2, 3],
        "content": "消息内容",
        "msg_type": "text|chat|task_delegate|knowledge_share"
    }
    """
    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    target_ids = data.get('target_claw_ids', [])
    if not target_ids:
        return jsonify({'error': 'target_claw_ids 为必填项'}), 400

    try:
        normalized_target_ids = [int(x) for x in target_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'target_claw_ids 必须是整数数组'}), 400

    # 禁止自发自收回路
    if claw_id in normalized_target_ids:
        return jsonify({'error': '禁止给自己发送消息，请改用 /messages/<msg_id>/read 闭环回复'}), 400

    content = data['content']
    msg_type = data.get('msg_type', 'text')
    sent = 0

    targets = OpenClawInstance.query.filter(
        OpenClawInstance.id.in_(normalized_target_ids),
        OpenClawInstance.status != 'deleted',
    ).all()

    for target in targets:
        msg = ClawMessage(
            claw_id=target.id,
            from_claw_id=claw_id,
            sender_name=claw.name,
            content=content,
            msg_type=msg_type,
            direction='to_claw',
            status='pending',  # 先设pending，SSE推送到客户端后再改delivered
        )
        db.session.add(msg)
        sent += 1

    db.session.commit()

    # 通知目标 claw 的 SSE 长连接立即推送
    for target_id in normalized_target_ids:
        notify_claw(target_id)

    return jsonify({'status': 'ok', 'sent_count': sent}), 201


@agent_bp.route('/<int:claw_id>/messages', methods=['GET'])
@require_claw_token
def claw_get_messages(claw_id, claw=None):
    """
    OpenClaw 获取消息历史（双向）

    参数：
    - limit: 返回条数（默认50）
    - unread: true 只返回未读消息
    - direction: to_claw/from_claw 过滤方向
    """
    limit = request.args.get('limit', 50, type=int)
    unread = request.args.get('unread', 'false').lower() == 'true'
    direction = request.args.get('direction')

    query = ClawMessage.query.filter_by(claw_id=claw_id)

    if unread:
        query = query.filter(
            ClawMessage.direction == 'to_claw',
            ClawMessage.status.in_(['pending', 'delivered'])
        )
    if direction:
        query = query.filter_by(direction=direction)

    messages = query.order_by(ClawMessage.created_at.desc()).limit(limit).all()

    return jsonify({
        'messages': [m.to_dict() for m in messages],
        'count': len(messages),
    })


@agent_bp.route('/<int:claw_id>/messages/<int:msg_id>/read', methods=['PUT'])
@require_claw_token
def claw_mark_read(claw_id, msg_id, claw=None):
    """OpenClaw 标记消息已读，支持附带回复"""
    msg = ClawMessage.query.filter_by(id=msg_id, claw_id=claw_id).first()
    if not msg:
        return jsonify({'error': '消息不存在'}), 404

    msg.status = 'read'
    msg.read_at = datetime.now()

    # 如果附带了 reply，自动创建一条回复消息
    reply_msg = None
    data = request.get_json(silent=True) or {}
    reply_content = data.get('reply', '').strip()
    if reply_content:
        reply_msg = ClawMessage(
            claw_id=claw_id,
            sender_name=claw.name,
            content=reply_content,
            msg_type='text',
            direction='from_claw',
            reply_to=msg_id,
            status='delivered',
            delivered_at=datetime.now(),
        )
        db.session.add(reply_msg)

    db.session.commit()

    result = {'status': 'ok'}
    if reply_msg:
        result['reply'] = reply_msg.to_dict()
    return jsonify(result)


# ==================== B+ 状态机：sidecar v2 显式上报 ====================
# 这三个接口取代老 sidecar 的"1.5 秒自动 mark read 兜底"——
# 让 sidecar 在 LLM 执行前/后/失败时显式调用，Hub 才能区分"agent 真的在干"和"agent 卡死"。

@agent_bp.route('/<int:claw_id>/messages/<int:msg_id>/processing', methods=['PUT'])
@require_claw_token
def claw_mark_processing(claw_id, msg_id, claw=None):
    """sidecar v2：开始调用 LLM 处理这条消息。

    幂等：重复调用只更新时间戳。从 pending/delivered → processing。
    """
    msg = ClawMessage.query.filter_by(id=msg_id, claw_id=claw_id).first()
    if not msg:
        return jsonify({'error': '消息不存在'}), 404
    msg.status = 'processing'
    msg.processing_at = datetime.now()
    if not msg.delivered_at:
        msg.delivered_at = msg.processing_at
    db.session.commit()
    return jsonify({'status': 'ok', 'msg_id': msg_id, 'state': 'processing'})


@agent_bp.route('/<int:claw_id>/messages/<int:msg_id>/done', methods=['PUT'])
@require_claw_token
def claw_mark_done(claw_id, msg_id, claw=None):
    """sidecar v2：LLM 处理完成，可附带 llm_response（Hub 仅留痕，不二次转发）。

    Body (可选): { "llm_response": "..." }
    自动同时更新 read_at（兼容老链路把 done 视作"已读"）。
    """
    msg = ClawMessage.query.filter_by(id=msg_id, claw_id=claw_id).first()
    if not msg:
        return jsonify({'error': '消息不存在'}), 404
    data = request.get_json(silent=True) or {}
    msg.status = 'done'
    msg.done_at = datetime.now()
    if not msg.read_at:
        msg.read_at = msg.done_at
    resp = (data.get('llm_response') or '').strip()
    if resp:
        msg.llm_response = resp[:5000]
        # 自动创建 from_claw 回复消息，通信中心前端才能显示 claw 的回复
        reply_msg = ClawMessage(
            claw_id=claw_id,
            sender_name=claw.name if claw else (msg.sender_name or 'OpenClaw'),
            content=resp[:5000],
            msg_type='text',
            direction='from_claw',
            reply_to=msg_id,
            status='delivered',
            delivered_at=datetime.now(),
        )
        db.session.add(reply_msg)
    db.session.commit()
    return jsonify({'status': 'ok', 'msg_id': msg_id, 'state': 'done'})


@agent_bp.route('/<int:claw_id>/messages/<int:msg_id>/failed', methods=['PUT'])
@require_claw_token
def claw_mark_failed(claw_id, msg_id, claw=None):
    """sidecar v2：LLM 处理失败，必须带 failed_reason。

    Body: { "failed_reason": "..." }
    """
    msg = ClawMessage.query.filter_by(id=msg_id, claw_id=claw_id).first()
    if not msg:
        return jsonify({'error': '消息不存在'}), 404
    data = request.get_json(silent=True) or {}
    reason = (data.get('failed_reason') or '').strip()
    if not reason:
        return jsonify({'error': 'failed_reason 必填'}), 400
    msg.status = 'failed'
    msg.failed_reason = reason[:2000]
    if not msg.read_at:
        # 标记 read_at 让 SSE 不再重推这条 failed 消息
        msg.read_at = datetime.now()
    db.session.commit()
    return jsonify({'status': 'ok', 'msg_id': msg_id, 'state': 'failed'})


# ==================== sidecar 配置中心：sidecar v2 拉配置 + 心跳 ====================

def _build_llm_apply(claw):
    """解析出可直接落到 agent config.yaml / .env 的完整大模型配置，供 sidecar 应用。

    返回 dict（provider/model/config_model/api_mode/base_url/api_key_env/api_key/
    context_length/timiai_project/gateway_service）；解析失败返回 None（sidecar 跳过）。
    """
    try:
        from app.hermes_models import (
            normalize_hermes_provider, normalize_hermes_model,
            hermes_config_model, hermes_context_length,
            hermes_provider_api_mode, HERMES_LLM_PROVIDERS,
            DEFAULT_HERMES_LLM_MODEL, DEFAULT_TIMIAI_LLM_MODEL,
        )
        from app.services.hermes_timiai_projects import normalize_timiai_project
        provider = normalize_hermes_provider(claw.llm_provider or 'venus')
        default_model = DEFAULT_TIMIAI_LLM_MODEL if provider == 'timiai' else DEFAULT_HERMES_LLM_MODEL
        model_key = normalize_hermes_model(claw.llm_model or default_model, provider)
        config_model = hermes_config_model(model_key, provider)
        context_length = hermes_context_length(model_key, provider)
        api_mode = hermes_provider_api_mode(provider)
        meta = HERMES_LLM_PROVIDERS[provider]
        project = ''
        api_key = ''
        try:
            from app.api.agent_deployments import (
                _configured_timiai_api_key, _configured_venus_api_key,
            )
            if provider == 'timiai':
                project = normalize_timiai_project(claw.timiai_project or 'gbt')
                api_key = _configured_timiai_api_key(project) or ''
            else:
                api_key = _configured_venus_api_key() or ''
        except Exception:
            logger.exception('resolve llm api_key failed for claw %s', getattr(claw, 'id', '?'))
        gateway_service = 'hermes-gateway-claw-%s.service' % claw.id
        try:
            from app.models import AgentDeployment
            dep = (AgentDeployment.query
                   .filter_by(openclaw_id=claw.id, agent_type='hermes')
                   .order_by(AgentDeployment.created_at.desc()).first())
            if dep and dep.container_name:
                gateway_service = dep.container_name
        except Exception:
            pass
        return {
            'provider': provider,
            'model': model_key,
            'config_model': config_model,
            'api_mode': api_mode,
            'base_url': meta['base_url'],
            'api_key_env': meta['api_key_env'],
            'api_key': api_key,
            'context_length': int(context_length),
            'timiai_project': project,
            'gateway_service': gateway_service,
        }
    except Exception:
        logger.exception('build llm_apply failed for claw %s', getattr(claw, 'id', '?'))
        return None


@agent_bp.route('/<int:claw_id>/sidecar-config', methods=['GET'])
@require_claw_token
def claw_sidecar_config(claw_id, claw=None):
    """sidecar v2 启动 / 每 60s 拉配置，顺便上报心跳。

    Query 参数（可选）:
        sidecar_version=2.0.0  - sidecar 自身版本
        agent_type=openclaw    - 旧版 sidecar 自动探测到的 provider
        runtime_kind=claw_worker - 新版 Worker 运行时类型
        runtime_provider=codex - 新版 Worker provider（hermes/codex）
        runtime_mode=legacy_split - Worker 进程拓扑
        runtime_platform=linux - Worker 平台（linux/windows）
        openclaw_bin=/path/...  - 自动探测到的 bin 路径

    返回:
        {
          agent_type, openclaw_bin, hermes_home, agent_name, agent_timeout,
          wecom_enabled, enabled, config_version,
          owner_wecom_userid,    # 让 sidecar 拼 prompt 时能带上
          server_time
        }
    """
    from app.models import ClawSidecarConfig
    from app.services.worker_runtime import (
        runtime_from_query,
        runtime_summary,
    )

    cfg = ClawSidecarConfig.query.get(claw_id)
    cfg_was_new = cfg is None
    runtime_report_warning = ''
    if cfg is None:
        # 首次启动：用 sidecar 上报的 agent_type / bin 初始化（保守默认）
        cfg = ClawSidecarConfig(
            claw_id=claw_id,
            agent_type=request.args.get('agent_type', 'openclaw') or 'openclaw',
            openclaw_bin=request.args.get('openclaw_bin', '') or '',
            hermes_home=request.args.get('hermes_home', '') or '',
            agent_name=request.args.get('agent_name', 'main') or 'main',
            agent_timeout=int(request.args.get('agent_timeout', 300) or 300),
            llm_provider=claw.llm_provider or 'venus',
            llm_model=claw.llm_model or 'venus',
            wecom_enabled=bool(claw.wecom_bot_id and claw.wecom_bot_secret),
            enabled=True,
            config_version=1,
            config_owner='hub',
            sidecar_version=request.args.get('sidecar_version', '') or '',
            last_heartbeat_at=datetime.now(),
            updated_by=f'sidecar_first_start',
        )
        db.session.add(cfg)
    else:
        cfg.last_heartbeat_at = datetime.now()
        sver = request.args.get('sidecar_version', '')
        if sver and cfg.sidecar_version != sver:
            cfg.sidecar_version = sver
        reported_type = (request.args.get('agent_type') or '').strip().lower()
        stored_kind = (
            (cfg.runtime_config_json or {}).get('kind')
            if isinstance(cfg.runtime_config_json, dict) else '')
        if stored_kind != 'claw_worker' \
                and reported_type in ('openclaw', 'hermes', 'codex', 'custom') \
                and cfg.agent_type != reported_type:
            cfg.agent_type = reported_type
            cfg.config_version = int(cfg.config_version or 0) + 1
            cfg.updated_by = 'sidecar_provider_activation'

    # New Workers can report their secret-free runtime identity.  Old Workers
    # remain compatible and can be backfilled through the operator endpoint.
    # config_owner is deliberately never accepted from the Worker query.
    try:
        reported_runtime = runtime_from_query(
            request.args, cfg.runtime_config_json)
        if reported_runtime:
            changed = reported_runtime != (cfg.runtime_config_json or {})
            cfg.runtime_config_json = reported_runtime
            cfg.runtime_reported_at = datetime.now()
            if reported_runtime.get('kind') == 'claw_worker':
                cfg.agent_type = reported_runtime['provider']
            if changed and not cfg_was_new:
                cfg.config_version = int(cfg.config_version or 0) + 1
                cfg.updated_by = 'worker_runtime_report'
    except ValueError as exc:
        runtime_report_warning = 'WORKER_RUNTIME_REPORT_INVALID'
        logger.warning('ignore invalid worker runtime report for claw %s: %s',
                       claw_id, exc)

    db.session.commit()

    # 把 owner_wecom_userid 一并返回给 sidecar；未单独配置时使用 owner 用户名。
    owner_wecom_userid = (claw.owner_wecom_userid or claw.owner or '').strip()

    payload = cfg.to_dict()
    runtime_info = runtime_summary(
        cfg.runtime_config_json, cfg.config_owner or 'hub')
    payload.update(runtime_info)
    if runtime_info['has_worker_runtime']:
        worker_runtime = runtime_info['worker_runtime'] or {}
        payload['llm_provider'] = worker_runtime.get('llm_provider') or ''
        payload['llm_model'] = worker_runtime.get('llm_model') or ''
        payload['timiai_project'] = worker_runtime.get('timiai_project') or ''
    else:
        payload['llm_provider'] = (
            claw.llm_provider or payload.get('llm_provider') or 'venus')
        payload['llm_model'] = (
            claw.llm_model or payload.get('llm_model') or 'venus')
    runtime_agent_type = (request.args.get('runtime_agent_type') or '').strip().lower()
    effective_runtime_agent_type = (
        runtime_info['runtime_provider']
        if runtime_info['has_worker_runtime']
        else (runtime_agent_type
              if runtime_agent_type in ('openclaw', 'hermes', 'codex', 'custom')
              else (payload.get('agent_type') or 'openclaw'))
    )
    # agent_type remains the compatibility field consumed by existing Worker
    # releases; runtime_kind identifies the host process separately.
    payload['agent_type'] = effective_runtime_agent_type
    payload['runtime_agent_type'] = effective_runtime_agent_type
    # Codex receives only sanitized conversational context. Its separately
    # supervised WeCom bridge reads the ACL-protected local credential file.
    payload['wecom_bot_secret'] = (
        '' if effective_runtime_agent_type == 'codex'
        else claw.get_wecom_bot_secret_plain() or ''
    )
    payload['wecom_bot_id'] = (
        '' if effective_runtime_agent_type == 'codex' else claw.wecom_bot_id or ''
    )
    payload['wecom_enabled'] = bool(claw.wecom_bot_id and claw.wecom_bot_secret)
    payload['owner_wecom_userid'] = owner_wecom_userid
    # A worker-owned runtime manages its own model credentials/config.  Omitting
    # llm_apply prevents stale Hub card values from overwriting that local state.
    if ((cfg.config_owner or 'hub') == 'hub'
            and effective_runtime_agent_type == 'hermes'):
        _la = _build_llm_apply(claw)
        if _la:
            payload['llm_apply'] = _la
    payload['claw_name'] = claw.name
    assignments = []
    rule_links = []
    workflow_create_definition_ids = None
    active_workflow_missions = []
    context_warnings = []
    if runtime_report_warning:
        context_warnings.append(runtime_report_warning)
    try:
        assignments = AgentPostAssignment.query.filter_by(
            claw_id=claw_id, status='active').all()
    except Exception:
        logger.exception('build agent profile config failed for claw %s', claw_id)
        context_warnings.append('PROFILE_CONTEXT_UNAVAILABLE')
    try:
        from app.models import OpenClawRule
        rule_links = OpenClawRule.query.filter_by(
            openclaw_id=claw_id, enabled=True).all()
    except Exception:
        logger.exception('build agent rule config failed for claw %s', claw_id)
        context_warnings.append('RULE_CONTEXT_UNAVAILABLE')
    try:
        from app.services.agent_system_context import (
            allowed_workflow_create_definition_ids,
        )
        active_definitions = WorkflowDefinition.query.filter_by(
            status='active').all()
        workflow_create_definition_ids = allowed_workflow_create_definition_ids(
            claw, active_definitions)
    except Exception:
        logger.exception('build workflow create grants failed for claw %s', claw_id)
        context_warnings.append('WORKFLOW_CREATE_GRANTS_UNAVAILABLE')
    try:
        from app.models import WorkflowMission
        active_workflow_missions = [
            {
                'id': item.id,
                'mission_key': item.mission_key,
                'project_id': item.project_id,
                'definitions_api': (
                    f'/api/v1/workflow-missions/{item.id}/definitions'),
                'dispatch_api': (
                    f'/api/v1/workflow-missions/{item.id}/dispatch'),
            }
            for item in WorkflowMission.query.filter(
                WorkflowMission.main_claw_id == claw_id,
                WorkflowMission.status == 'active',
                WorkflowMission.expires_at > _now(),
            ).order_by(WorkflowMission.updated_at.desc()).limit(10).all()
        ]
    except Exception:
        db.session.rollback()
        logger.exception('build workflow Mission grants failed for claw %s', claw_id)
        context_warnings.append('WORKFLOW_MISSION_GRANTS_UNAVAILABLE')
    try:
        from app.services.agent_system_context import build_agent_system_context
        payload.update(build_agent_system_context(
            claw,
            effective_runtime_agent_type,
            assignments,
            rule_links,
            warnings=context_warnings,
            workflow_create_definition_ids=workflow_create_definition_ids,
            configured_policy=cfg.system_context_policy_json,
            workflow_missions=active_workflow_missions,
        ))
    except Exception:
        logger.exception('build agent system context failed for claw %s', claw_id)
        payload['agent_profiles'] = []
        payload['active_agent_profile'] = None
        payload['rules'] = []
        fallback_policy = {
            'profile_required_for_execution': False,
            'missing_profile_behavior': 'continue_with_identity_and_rules',
        }
        fallback_warnings = ['SYSTEM_CONTEXT_UNAVAILABLE']
        if (effective_runtime_agent_type == 'codex' and
                cfg.system_context_policy_json is not None):
            fallback_policy['allowed_workflow_create_definition_ids'] = []
            fallback_warnings.append('CODEX_ORCHESTRATOR_POLICY_INVALID')
        payload['system_context'] = {
            'schema_version': 2,
            'identity': {
                'claw_id': claw_id,
                'claw_name': claw.name or '',
                'provider': effective_runtime_agent_type,
            },
            'profile': None,
            'rules': [],
            'warnings': fallback_warnings,
            'policy': fallback_policy,
        }
        payload['system_context_version'] = 2
        payload['system_context_digest'] = ''
        payload['context_warnings'] = fallback_warnings
    # Hub 能力索引：sidecar 每次构 prompt 前注入，等价于"回复前先查 Rule #19"。
    try:
        from app.services.hub_capability import (
            build_hub_capability_digest, HUB_CAPABILITY_VERSION,
        )
        payload['hub_capability_digest'] = build_hub_capability_digest()
        payload['hub_capability_version'] = HUB_CAPABILITY_VERSION
    except Exception:
        logger.exception('build hub capability digest failed for claw %s', claw_id)
    # 笔记索引（三层记忆·记全层）：当前任务上下文(taskctx) + 常驻决策(decision)。
    # sidecar 每次构 prompt 前注入，让 agent 知道自己记过什么；取全文用 /api/v1/memos/memo/{id}。
    try:
        from app.services.memo_index import build_memo_index
        payload['memo_index'] = build_memo_index(claw, limit=20)
    except Exception:
        logger.exception('build memo index failed for claw %s', claw_id)
        payload['memo_index'] = []
    try:
        from app.services.memory_sync_config import (
            build_memory_sync_config,
            shared_memory_capability,
        )
        memory_sync = build_memory_sync_config(claw)
        if memory_sync:
            payload['memory_sync'] = memory_sync
            payload.setdefault('capabilities', {})['shared_memory'] = (
                shared_memory_capability()
            )
    except Exception:
        logger.exception('build shared memory config failed for claw %s', claw_id)
    payload['server_time'] = cst_iso()
    payload['timezone'] = TODO_TIMEZONE
    return jsonify(payload)


@agent_bp.route('/<int:claw_id>/sidecar-deployment-verify', methods=['GET'])
@require_claw_token
def sidecar_deployment_verify(claw_id, claw=None):
    """供 install_v2.sh / claw 单命令部署后自检：Hub 汇总 DB 状态，不对就列出原因。

    Query:
        expected_sidecar_version   默认读环境变量 EXPECTED_SIDECAR_VERSION，缺省 2.0.1
        heartbeat_max_age_sec     默认 180，超过则认为 sidecar 未正常心跳
        notify=1                  若校验未通过，向该 claw 发一条 system 消息（便于 agent 看到）

    返回 JSON（HTTP 200，body 里 ok=false 表示未通过）：
        ok, errors[], warnings[], hints{}
    """
    from app.models import OpenClawSkill, ClawSidecarConfig as _Cfg

    expected = (
        request.args.get('expected_sidecar_version')
        or os.getenv('EXPECTED_SIDECAR_VERSION', '2.0.1')
    ).strip()
    try:
        hb_max = int(request.args.get('heartbeat_max_age_sec', '180'))
    except ValueError:
        hb_max = 180
    do_notify = request.args.get('notify', '').lower() in ('1', 'true', 'yes')

    errors = []
    warnings = []
    hints = {}

    inst = OpenClawInstance.query.get(claw_id)
    if not inst:
        errors.append({'code': 'no_instance', 'message': f'claw_id={claw_id} 不存在'})
    else:
        mode = (inst.connection_mode or '').strip().lower()
        if mode != 'sse':
            errors.append({
                'code': 'connection_mode',
                'message': f'当前 connection_mode={mode!r}，#143 v2 需要 sse',
                'fix': f"UPDATE openclaw_instances SET connection_mode='sse' WHERE id={claw_id};",
            })
        hints['connection_mode'] = mode

        o143 = OpenClawSkill.query.filter_by(
            openclaw_id=claw_id, skill_id=143, enabled=True).first()
        if not o143:
            errors.append({
                'code': 'skill_143',
                'message': '未在 Hub 启用 skill #143（hub-sse-sidecar-v2）',
                'fix': 'Hub Web → 给小 claw 安装 #143 并启用',
            })

        o135 = OpenClawSkill.query.filter_by(
            openclaw_id=claw_id, skill_id=135, enabled=True).first()
        if o135:
            warnings.append({
                'code': 'skill_135_enabled',
                'message': '仍启用旧 #135，可能与 v2 冲突',
                'fix': 'Hub Web → 禁用 #135',
            })

        cfg = _Cfg.query.get(claw_id)
        if not cfg:
            errors.append({
                'code': 'no_sidecar_config',
                'message': 'Hub 尚无 claw_sidecar_configs 记录（sidecar 未成功拉过 /sidecar-config）',
                'fix': '确认本机 sidecar 已启动且 CLAW_TOKEN 正确',
            })
        else:
            hints['sidecar_version'] = cfg.sidecar_version or ''
            hints['last_heartbeat_at'] = (
                str(cfg.last_heartbeat_at) if cfg.last_heartbeat_at else None)
            if cfg.sidecar_version and expected and cfg.sidecar_version != expected:
                warnings.append({
                    'code': 'sidecar_version_mismatch',
                    'message': f'sidecar 上报 {cfg.sidecar_version!r}，期望 {expected!r}',
                    'fix': f'curl 拉取静态脚本后重启 sidecar: {inst.name}',
                })
            if cfg.last_heartbeat_at:
                age = (datetime.now() - cfg.last_heartbeat_at).total_seconds()
                if age > hb_max:
                    errors.append({
                        'code': 'heartbeat_stale',
                        'message': f'心跳过旧 {int(age)}s > {hb_max}s',
                        'fix': '本机 systemctl restart hub-sse-sidecar-v2 或检查 sidecar 日志',
                    })
            else:
                errors.append({
                    'code': 'no_heartbeat',
                    'message': 'sidecar 从未写入 last_heartbeat_at',
                    'fix': '检查 sidecar 进程与网络',
                })

    ok = len(errors) == 0
    body = {
        'ok': ok,
        'claw_id': claw_id,
        'expected_sidecar_version': expected,
        'errors': errors,
        'warnings': warnings,
        'hints': hints,
    }

    if do_notify and not ok:
        lines = ['[Hub 部署校验未通过]']
        for e in errors:
            lines.append(f"- {e.get('code')}: {e.get('message')}")
            if e.get('fix'):
                lines.append(f"  建议: {e['fix']}")
        for w in warnings:
            lines.append(f"- (警告) {w.get('code')}: {w.get('message')}")
        msg = ClawMessage(
            claw_id=claw_id,
            sender_name='Hub',
            content='\n'.join(lines),
            msg_type='system',
            direction='to_claw',
            status='pending',
        )
        db.session.add(msg)
        db.session.commit()
        notify_claw(claw_id)
        body['notified_message_id'] = msg.id

    return jsonify(body)


@agent_bp.route('/<int:claw_id>/dispatch', methods=['POST'])
def dispatch_task_to_claw(claw_id):
    """
    派发任务给指定子agent（Manager管理接口）

    请求体：
    {
        "task_type": "modify_config|execute_command|update_file|read_file",
        "command": "具体命令",
        "target_path": "/path/to/file",
        "payload": {...}  # JSON格式的操作内容
    }
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    if not data or not data.get('task_type'):
        return jsonify({'error': 'task_type 为必填项'}), 400

    # 生成唯一任务ID
    import secrets
    task_id = f"task_{int(time.time())}_{secrets.token_hex(8)}"

    task = AgentTask(
        claw_id=claw_id,
        task_id=task_id,
        task_type=data['task_type'],
        command=data.get('command'),
        target_path=data.get('target_path'),
        payload=json.dumps(data.get('payload', {})),
        status='pending',
    )
    db.session.add(task)
    db.session.commit()

    # 通知 SSE 长连接立即推送新任务
    notify_claw(claw_id)

    return jsonify({
        'status': 'dispatched',
        'task': task.to_dict()
    }), 201


@agent_bp.route('/<int:claw_id>/tasks', methods=['GET'])
def list_claw_tasks(claw_id):
    """查询指定claw的任务列表"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    status = request.args.get('status')  # pending/running/completed/failed
    limit = request.args.get('limit', 50, type=int)

    query = AgentTask.query.filter_by(claw_id=claw_id)
    if status:
        query = query.filter_by(status=status)

    tasks = query.order_by(AgentTask.created_at.desc()).limit(limit).all()

    return jsonify([t.to_dict() for t in tasks])


@agent_bp.route('/<int:claw_id>/tasks/<task_id>', methods=['GET'])
def get_claw_task(claw_id, task_id):
    """查询指定claw的指定任务"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    task = AgentTask.query.filter_by(claw_id=claw_id, task_id=task_id).first_or_404()

    return jsonify(task.to_dict())


# ==================== 轮询模式接口 ====================

@agent_bp.route('/<int:claw_id>/pending-tasks', methods=['GET'])
@require_claw_token
def get_pending_tasks_poll(claw_id, claw=None):
    """
    轮询模式专用接口 - OpenClaw 客户端定期轮询获取待处理任务

    请求方式：GET /api/openclaws/<claw_id>/pending-tasks
    认证：Authorization: Bearer <token>

    返回：
    {
        "has_tasks": true/false,
        "tasks": [...],  // 最多10条
        "server_time": "..."
    }
    """
    # 获取待处理任务
    tasks = AgentTask.query.filter(
        AgentTask.claw_id == claw_id,
        AgentTask.status == 'pending'
    ).order_by(AgentTask.created_at.asc()).limit(10).all()

    # 标记为 running（F3：workflow 任务先校验 run/step 仍活跃，否则失效不投递）
    deliver = []
    for task in tasks:
        if not _workflow_task_deliverable(task):
            task.status = 'failed'
            task.error = 'workflow_run_or_step_inactive'
            task.completed_at = datetime.now()
            continue
        task.status = 'running'
        task.assigned_at = datetime.now()
        deliver.append(task)
    db.session.commit()

    return jsonify({
        'has_tasks': len(deliver) > 0,
        'tasks': [t.to_dict() for t in deliver],
        'server_time': cst_iso(),
        'timezone': TODO_TIMEZONE,
    })


@agent_bp.route('/<int:claw_id>/poll', methods=['POST'])
@require_claw_token
def poll_task_result(claw_id, claw=None):
    """
    轮询模式 - 上报任务执行结果（兼容 MCP 模式）

    请求体：
    {
        "task_id": "xxx",
        "status": "completed/failed",
        "result": "执行结果",
        "error": "错误信息"
    }
    """
    data = request.get_json()
    if not data or _is_missing(data.get('task_id')):
        return jsonify({'error': 'task_id 为必填项'}), 400

    task = AgentTask.query.filter_by(task_id=data['task_id'], claw_id=claw_id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    task.status = data.get('status', 'completed')
    task.result = data.get('result')
    task.error = data.get('error')
    task.completed_at = datetime.now()
    db.session.commit()

    return jsonify({'status': 'ok', 'task': task.to_dict()})
