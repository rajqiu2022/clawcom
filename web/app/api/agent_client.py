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
from flask import request, jsonify, Response, stream_with_context, Blueprint
from app import db
from app.models import OpenClawInstance, ClawMessage, ClawTodo, ClawTodoLog, _now
from functools import wraps
import json
import time
import hashlib
import logging


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
    threshold = datetime.now() - timedelta(seconds=OFFLINE_TIMEOUT)
    stale_claws = OpenClawInstance.query.filter(
        OpenClawInstance.status.in_(['工作', '学习', '摸鱼', 'online']),
        OpenClawInstance.last_activity < threshold
    ).all()
    for c in stale_claws:
        logger.warning(f"检测到超时连接: {c.name} (ID={c.id}), last_activity={c.last_activity}, 标记为 offline")
        c.status = 'offline'
    if stale_claws:
        db.session.commit()


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器

    优先验证 URL 中 claw_id 对应的 Token；
    如果不匹配，回退遍历所有 OpenClawInstance 验证（支持 Token 与 claw_id 不一致的场景，
    如客户端 Token 更新后 claw_id 仍为旧值但 Token 有效的情况）。
    """
    @wraps(f)
    def decorated(claw_id, *args, **kwargs):
        claw = OpenClawInstance.query.get_or_404(claw_id)
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401
        token = auth_header[7:]
        # 优先匹配 claw_id 对应的 Token
        if claw.verify_token(token):
            return f(claw_id, claw=claw, *args, **kwargs)
        # 回退：遍历所有非删除的 OpenClawInstance 验证 Token
        for c in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if c.verify_token(token):
                return f(claw_id, claw=c, *args, **kwargs)
        return jsonify({'error': 'Token 无效'}), 403
    return decorated


class AgentTask(db.Model):
    """子agent任务队列"""
    __tablename__ = 'agent_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    task_id = db.Column(db.String(64), unique=True, nullable=False, comment='全局唯一任务ID')
    task_type = db.Column(db.String(50), nullable=False, comment='任务类型')
    command = db.Column(db.String(255), comment='操作命令')
    target_path = db.Column(db.String(500), comment='目标文件路径')
    payload = db.Column(db.Text, comment='操作内容(JSON)')  # JSON字符串
    status = db.Column(db.String(20), default='pending', comment='pending/running/completed/failed')
    result = db.Column(db.Text, comment='执行结果')
    error = db.Column(db.Text, comment='错误信息')
    created_at = db.Column(db.DateTime, default=_now)
    assigned_at = db.Column(db.DateTime, comment='分配时间')
    completed_at = db.Column(db.DateTime, comment='完成时间')

    claw = db.relationship('OpenClawInstance', backref='tasks')

    def to_dict(self):
        return {
            'id': self.id,
            'task_id': self.task_id,
            'claw_id': self.claw_id,
            'task_type': self.task_type,
            'command': self.command,
            'target_path': self.target_path,
            'payload': json.loads(self.payload) if self.payload else None,
            'status': self.status,
            'result': self.result,
            'error': self.error,
            'created_at': str(self.created_at) if self.created_at else None,
            'assigned_at': str(self.assigned_at) if self.assigned_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
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
    - event: todos_pending\ndata: {count, todos: [{id, title, urgency, ...}]}\n\n
    - event: ping\ndata: \n\n
    """
    # 清理超时的 SSE 连接（防止挂断后状态不更新）
    _cleanup_stale_connections()

    # SSE 连接建立时设为在线
    # 重要：在 gevent 环境下，SQLAlchemy 的 db.session.commit() 可能不真正提交到 MariaDB，
    # 必须使用独立的 pymysql 连接来确保状态更新
    import pymysql as _pymysql
    _raw_conn = _pymysql.connect(
        host=os.getenv('MYSQL_HOST', 'localhost'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'booster'),
        password=os.getenv('MYSQL_PASSWORD', 'booster'),
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

    # 刷新 claw 对象以获取最新状态
    db.session.expire(claw)
    claw_name = claw.name
    logger.info(f"SSE连接建立，OpenClaw {claw_id} ({claw_name}) 状态: {claw.status}")

    def generate():
        # 发送连接成功事件
        yield f"event: connected\ndata: {json.dumps({'claw_id': claw_id, 'name': claw_name, 'server_time': datetime.now().isoformat()})}\n\n"

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
            for msg in unread_msgs:
                if msg.status == 'pending':
                    msg.status = 'delivered'
                    msg.delivered_at = datetime.now()
                yield f"event: message\ndata: {json.dumps(msg.to_dict())}\n\n"
            if unread_msgs:
                db.session.commit()
                logger.info(
                    f"SSE连接建立，推送 {len(unread_msgs)} 条未读消息给 OpenClaw {claw_id}"
                )

            # 推送当前待办列表
            today = date.today()
            all_todos = ClawTodo.query.filter_by(openclaw_id=claw_id, enabled=True).all()
            today_logs = {l.todo_id: l for l in ClawTodoLog.query.filter_by(
                openclaw_id=claw_id, log_date=today).all()}
            pending_todos = []
            for t in all_todos:
                log = today_logs.get(t.id)
                is_done = log and log.status in ('completed', 'approved')
                need_today = False
                if t.schedule_type == 'once':
                    need_today = not is_done
                elif t.schedule_type == 'daily':
                    need_today = True
                elif t.schedule_type == 'weekly' and t.schedule_day:
                    need_today = today.isoweekday() == t.schedule_day
                elif t.schedule_type == 'monthly' and t.schedule_day:
                    need_today = today.day == t.schedule_day
                if need_today and not is_done:
                    pending_todos.append({
                        'id': t.id,
                        'title': t.title,
                        'urgency': t.urgency_level or 'flexible',
                        'schedule_time': t.schedule_time,
                        'schedule_type': t.schedule_type,
                        'today_status': log.status if log else None,
                    })
            yield f"event: todos_pending\ndata: {json.dumps({'count': len(pending_todos), 'todos': pending_todos})}\n\n"
            logger.info(f"SSE连接建立，推送 {len(pending_todos)} 个待办给 OpenClaw {claw_id}")
        except Exception as e:
            logger.error(f"SSE连接建立时推送初始数据失败: {e}")

        last_task_check = time.time()
        last_todo_check = 0  # 待办推送节流：上次推送待办的时间
        # 用初始推送的待办数初始化（避免重复推送）
        try:
            last_todo_count = len(pending_todos)
        except NameError:
            last_todo_count = -1

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

                # 每10秒检查新任务和新消息
                tasks = AgentTask.query.filter(
                    AgentTask.claw_id == claw_id,
                    AgentTask.status == 'pending'
                ).order_by(AgentTask.created_at.asc()).limit(10).all()

                # 使用独立 pymysql 连接查询消息，绕过 gevent session 缓存问题
                # （db.session 在 gevent 长连接中会缓存旧数据，导致新消息不可见）
                import pymysql as _pymysql
                _mc = _pymysql.connect(
                    host=os.getenv('MYSQL_HOST', 'localhost'),
                    port=int(os.getenv('MYSQL_PORT', '3306')),
                    user=os.getenv('MYSQL_USER', 'booster'),
                    password=os.getenv('MYSQL_PASSWORD', 'booster'),
                    database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
                    charset='utf8mb4'
                )
                pending_msg_ids = []
                try:
                    with _mc.cursor(_pymysql.cursors.DictCursor) as _mcur:
                        # 主循环只推 status='pending' 的新消息（首屏未读消息已在
                        # connected 阶段一次性推完）。这里如果用 read_at IS NULL
                        # 会把所有"已推但未读"的也再次拉出来反复推，反而退化。
                        _mcur.execute(
                            "SELECT id FROM claw_messages WHERE claw_id=%s "
                            "AND direction='to_claw' AND status='pending' "
                            "ORDER BY created_at ASC LIMIT 10",
                            (claw_id,),
                        )
                        pending_msg_ids = [row['id'] for row in _mcur.fetchall()]
                finally:
                    _mc.close()

                # 用 SQLAlchemy 加载消息对象（session 已 expire_all，会从 DB 读最新）
                messages = []
                if pending_msg_ids:
                    messages = ClawMessage.query.filter(
                        ClawMessage.id.in_(pending_msg_ids)
                    ).order_by(ClawMessage.created_at.asc()).all()

                now = time.time()
                
                # === 每次循环都推送消息（不再等 10 秒间隔）===
                # 消息查询已用独立 pymysql，结果可靠，直接推送
                delivered_ids = []
                for msg in messages:
                    msg.status = 'delivered'
                    msg.delivered_at = datetime.now()
                    yield f"event: message\ndata: {json.dumps(msg.to_dict())}\n\n"
                    delivered_ids.append(msg.id)

                # 用独立 pymysql 把 status 写回 DB
                # （和上面 status='工作' 同样的原因：db.session.commit() 在 gevent
                # 长连接里可能不真正落库，会导致同一条消息被反复推送 / 状态卡 pending）
                if delivered_ids:
                    import pymysql as _pymysql
                    _uc = _pymysql.connect(
                        host=os.getenv('MYSQL_HOST', 'localhost'),
                        port=int(os.getenv('MYSQL_PORT', '3306')),
                        user=os.getenv('MYSQL_USER', 'booster'),
                        password=os.getenv('MYSQL_PASSWORD', 'booster'),
                        database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
                        charset='utf8mb4',
                    )
                    try:
                        with _uc.cursor() as _ucur:
                            _placeholders = ','.join(['%s'] * len(delivered_ids))
                            _ucur.execute(
                                f"UPDATE claw_messages SET status='delivered', "
                                f"delivered_at=NOW() WHERE id IN ({_placeholders}) "
                                f"AND status='pending'",
                                tuple(delivered_ids),
                            )
                        _uc.commit()
                        print(
                            f"[SSE_PUSH] claw_id={claw_id} delivered "
                            f"{len(delivered_ids)} msgs ids={delivered_ids}",
                            flush=True,
                        )
                    finally:
                        _uc.close()

                if now - last_task_check > 10:
                    # 更新活跃时间和在线状态（使用独立 pymysql 连接，绕过 gevent session 问题）
                    import pymysql as _pymysql
                    _rc = _pymysql.connect(
                        host=os.getenv('MYSQL_HOST', 'localhost'),
                        port=int(os.getenv('MYSQL_PORT', '3306')),
                        user=os.getenv('MYSQL_USER', 'booster'),
                        password=os.getenv('MYSQL_PASSWORD', 'booster'),
                        database=os.getenv('MYSQL_DATABASE', 'openclaw_manager'),
                        charset='utf8mb4'
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

                    # 发送待处理任务
                    for task in tasks:
                        task.status = 'running'
                        task.assigned_at = datetime.now()
                        db.session.commit()

                        yield f"event: task\ndata: {json.dumps(task.to_dict())}\n\n"

                    # 检查待办任务变化（每 30 秒，或收到待办变更通知时立即推送）
                    has_todo_change = _consume_todo_change(claw_id)
                    if has_todo_change or now - last_todo_check > 30:
                        today = date.today()
                        all_todos = ClawTodo.query.filter_by(
                            openclaw_id=claw_id, enabled=True).all()
                        today_logs = {l.todo_id: l for l in ClawTodoLog.query.filter_by(
                            openclaw_id=claw_id, log_date=today).all()}

                        pending_todos = []
                        for t in all_todos:
                            log = today_logs.get(t.id)
                            is_done = log and log.status in ('completed', 'approved')

                            # 判断今天是否需要执行
                            need_today = False
                            if t.schedule_type == 'once':
                                need_today = not is_done
                            elif t.schedule_type == 'daily':
                                need_today = True
                            elif t.schedule_type == 'weekly' and t.schedule_day:
                                need_today = today.isoweekday() == t.schedule_day
                            elif t.schedule_type == 'monthly' and t.schedule_day:
                                need_today = today.day == t.schedule_day

                            if need_today and not is_done:
                                pending_todos.append({
                                    'id': t.id,
                                    'title': t.title,
                                    'urgency': t.urgency_level or 'flexible',
                                    'schedule_time': t.schedule_time,
                                    'schedule_type': t.schedule_type,
                                    'today_status': log.status if log else None,
                                })

                        current_count = len(pending_todos)
                        # 待办数变化时推送，或首次连接时推送，或收到待办变更通知时立即推送
                        if current_count != last_todo_count or last_todo_count == -1 or has_todo_change:
                            yield f"event: todos_pending\ndata: {json.dumps({'count': current_count, 'todos': pending_todos})}\n\n"
                            last_todo_count = current_count

                        last_todo_check = now

                    last_task_check = now

                # ping 保持连接（防止 nginx/proxy 超时断开）
                # 有消息推送时跳过 ping（消息本身就是保活信号）
                if not messages:
                    yield f"event: ping\ndata: \n\n"




                # 轮询间隔 2 秒（gevent 多 worker 下 Event 不跨进程，用短轮询更可靠）
                time.sleep(2)




            except GeneratorExit:
                # 客户端断开连接，更新状态为 offline
                try:
                    import pymysql as _pymysql
                    _rc = _pymysql.connect(
                        host=os.getenv('MYSQL_HOST', 'localhost'),
                        port=int(os.getenv('MYSQL_PORT', '3306')),
                        user=os.getenv('MYSQL_USER', 'booster'),
                        password=os.getenv('MYSQL_PASSWORD', 'booster'),
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
    """OpenClaw 给其他 OpenClaw 发消息（admin claw 可发给任意 claw）

    请求体：
    {
        "target_claw_ids": [1, 2, 3],
        "content": "消息内容",
        "msg_type": "text|chat|task_delegate|knowledge_share"
    }
    """
    if claw.role != 'admin':
        return jsonify({'error': '只有管理员 claw 可以给其他 claw 发消息'}), 403

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

    # 防止 admin claw 把消息发给自己导致"自发自收"回路（direction=to_claw 且 sender=自己）
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

    # 标记为 running
    for task in tasks:
        task.status = 'running'
        task.assigned_at = datetime.now()
    db.session.commit()

    return jsonify({
        'has_tasks': len(tasks) > 0,
        'tasks': [t.to_dict() for t in tasks],
        'server_time': datetime.now().isoformat(),
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
