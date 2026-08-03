"""5 分钟兜底守护进程（B+ 通信稳定化）

设计目标
    确保 OpenClaw ↔ Hub 的通信链路即使部分失败，owner 也能收到企微告警，
    永远不出现"消息发出去但 owner 一直不知道"的隐性故障。

兜底范围
    A. ClawTodoLog: status='submitted' AND notified_at IS NULL AND completed_at < now-5min
       → 走 sendRTXInfo 应用通知兜底，告知 owner: "你的待办已完成（agent 似乎没主动通知你）"
       → 写入 notified_at + notified_strategy='timeout_fallback'
    B. ClawMessage: status IN ('pending','processing') AND created_at < now-5min
       → 走 sendRTXInfo 应用通知兜底，告知 owner: "你的 claw 有消息卡了 5 分钟没处理"
       → 写入 status='failed' + failed_reason='timeout_5min'，避免重复告警

主进程选举（gunicorn -w 4 安全）
    用 system_config 表做分布式锁：
        config_key = 'timeout_watcher_owner'
        value      = 持锁 worker 的 pid
        updated_at = 最近一次心跳时间，超过 90 秒视为失效
    每个 worker 都跑 watcher 循环，但只有持锁的那个真干活，其他空转抢锁。

调用入口
    app/__init__.py:create_app() 末尾调用 start_timeout_watcher(app)
"""

import os
import random
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text


WATCHER_LOCK_KEY = 'timeout_watcher_owner'
WATCHER_LOOP_INTERVAL_SEC = 60       # 主进程每 60s 扫一次
WATCHER_IDLE_INTERVAL_SEC = 30       # 抢不到锁的 worker 30s 重试一次
LOCK_TTL_SEC = 90                    # 心跳超过 90s 视为失效，可被抢
TODO_TIMEOUT_MIN = 5                 # todo submitted 超过 N 分钟没通知就兜底
MESSAGE_TIMEOUT_MIN = 5              # claw_message pending/processing 超过 N 分钟就告警
WORKFLOW_TASK_TTL_MIN = 30           # F2：workflow AgentTask 超过 N 分钟仍未结束就失效（防陈旧任务重连即跑）

# 运行开关（存在 system_config 表里，不重启服务就能改）：
#   timeout_watcher_todo_fallback_enabled  '1'/'0'  默认 '0' 关闭
#       —— 控制 A 类扫描：ClawTodoLog 5 分钟没通知是否发应用通知兜底
#   timeout_watcher_message_alert_enabled  '1'/'0'  默认 '0' 关闭
#       —— 控制 B 类扫描：ClawMessage 卡死 5 分钟是否告警
# 两个都默认关闭：
#   - 数据库 schema / 状态机 / wecom_send_logs 等基础设施先上线观察
#   - 等数据稳定、用户确认 owner_wecom_userid 都配好了，再用 SQL 打开兜底
SWITCH_TODO_FALLBACK = 'timeout_watcher_todo_fallback_enabled'
SWITCH_MESSAGE_ALERT = 'timeout_watcher_message_alert_enabled'

_started = False
_started_lock = threading.Lock()


def _now():
    return datetime.now()


def _get_switch(db, key, default_on):
    """读 system_config 中的开关，缺失则按 default_on 处理。

    Args:
        key: 配置 key
        default_on: bool，缺失时返回的默认值
    """
    try:
        with db.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT value FROM system_config WHERE config_key=:k LIMIT 1"
            ), {'k': key}).fetchone()
            if not row:
                return default_on
            v = (row[0] or '').strip().lower()
            if v in ('1', 'true', 'on', 'yes'):
                return True
            if v in ('0', 'false', 'off', 'no'):
                return False
            return default_on
    except Exception:
        return default_on


def _try_acquire_lock(db, pid_str, logger):
    """原子化抢锁：当前 owner 是自己 / 过期，则更新为自己 + 心跳。

    返回 True = 抢到（或保持持有），False = 别人持有且未过期。
    """
    try:
        with db.engine.begin() as conn:
            # 第一次启动时确保 row 存在（INSERT IGNORE 后续 noop）
            conn.execute(text(
                "INSERT IGNORE INTO system_config (config_key, value, description, updated_at) "
                "VALUES (:k, :pid, '5分钟兜底守护进程持锁 worker PID（B+ 方案）', NOW())"
            ), {'k': WATCHER_LOCK_KEY, 'pid': pid_str})

            result = conn.execute(text(
                "UPDATE system_config "
                "SET value=:pid, updated_at=NOW() "
                "WHERE config_key=:k "
                "  AND (value=:pid OR updated_at < (NOW() - INTERVAL :ttl SECOND))"
            ), {'k': WATCHER_LOCK_KEY, 'pid': pid_str, 'ttl': LOCK_TTL_SEC})
            return result.rowcount > 0
    except Exception as e:
        logger.warning(f'[timeout_watcher] 抢锁异常 pid={pid_str}: {e}')
        return False


def _scan_overdue_todo_logs(db, app):
    """A. 兜底未通知的 submitted todo log。"""
    from app.models import ClawTodoLog, ClawTodo, OpenClawInstance
    from app.api.wecom import WecomDispatcher

    threshold = _now() - timedelta(minutes=TODO_TIMEOUT_MIN)
    rows = (ClawTodoLog.query
            .filter(ClawTodoLog.status == 'submitted')
            .filter(ClawTodoLog.notified_at.is_(None))
            .filter(ClawTodoLog.completed_at != None)  # noqa: E711
            .filter(ClawTodoLog.completed_at < threshold)
            .order_by(ClawTodoLog.id.asc())
            .limit(50)
            .all())

    sent = 0
    for log in rows:
        todo = ClawTodo.query.get(log.todo_id)
        claw = OpenClawInstance.query.get(log.openclaw_id)
        target = (claw and claw.owner_wecom_userid) or ''

        title = f"[Hub 兜底] {claw.name if claw else 'OpenClaw'} 待办已完成"
        body_lines = [
            f"任务: {todo.title if todo else f'todo#{log.todo_id}'}",
        ]
        summary = (log.result_summary or '').strip()
        if summary:
            body_lines.append(f"结果: {summary[:800]}")
        body_lines.append(f"提交时间: {log.completed_at}")
        body_lines.append(
            f"提示: agent 提交了 {TODO_TIMEOUT_MIN} 分钟仍未自发企微，Hub 应用通知兜底。"
        )

        try:
            ok, _ = WecomDispatcher.send(
                target_userid=target,
                title=title,
                content='\n'.join(body_lines),
                claw_id=log.openclaw_id,
                related_type='todo_timeout',
                related_id=log.id,
                commit=False,
            )
            log.notified_at = _now()
            log.notified_strategy = 'timeout_fallback'
            db.session.commit()
            sent += 1
            app.logger.info(
                f"[timeout_watcher] 兜底通知 todo_log={log.id} "
                f"claw={log.openclaw_id} target={target!r} ok={ok}")
        except Exception as e:
            db.session.rollback()
            app.logger.warning(
                f"[timeout_watcher] 兜底通知失败 todo_log={log.id}: {e}")
    return sent


def _scan_stuck_claw_messages(db, app):
    """B. 告警卡住的 claw_messages。"""
    from app.models import ClawMessage, OpenClawInstance
    from app.api.wecom import WecomDispatcher

    threshold = _now() - timedelta(minutes=MESSAGE_TIMEOUT_MIN)
    rows = (ClawMessage.query
            .filter(ClawMessage.direction == 'to_claw')
            .filter(ClawMessage.status.in_(['pending', 'processing']))
            .filter(ClawMessage.created_at < threshold)
            .order_by(ClawMessage.id.asc())
            .limit(50)
            .all())

    sent = 0
    for msg in rows:
        claw = OpenClawInstance.query.get(msg.claw_id)
        target = (claw and claw.owner_wecom_userid) or ''
        claw_name = claw.name if claw else f'claw#{msg.claw_id}'

        title = f"[Hub 告警] {claw_name} 消息卡住 {MESSAGE_TIMEOUT_MIN} 分钟"
        body_lines = [
            f"消息 ID: {msg.id}（{msg.msg_type}）",
            f"发送时间: {msg.created_at}",
            f"当前状态: {msg.status}",
            f"内容: {(msg.content or '')[:400]}",
            "",
            "提示: claw 可能离线或处理失败。请检查 sidecar 状态。",
        ]
        try:
            WecomDispatcher.send(
                target_userid=target,
                title=title,
                content='\n'.join(body_lines),
                claw_id=msg.claw_id,
                related_type='message_stuck',
                related_id=msg.id,
                commit=False,
            )
            msg.status = 'failed'
            msg.failed_reason = f'timeout_{MESSAGE_TIMEOUT_MIN}min'
            db.session.commit()
            sent += 1
            app.logger.info(
                f"[timeout_watcher] 卡死消息已告警并标记 msg={msg.id} claw={msg.claw_id}")
        except Exception as e:
            db.session.rollback()
            app.logger.warning(
                f"[timeout_watcher] 消息告警失败 msg={msg.id}: {e}")
    return sent


def _scan_stale_workflow_agent_tasks(db, app):
    """F2：清理陈旧/死 run 的 workflow AgentTask，防止离线 agent 重连后执行陈旧任务空转烧算力。

    失效条件（满足其一）：
      a) 归属的 run/step 已不活跃（run/step 非 running/retrying）—— 不论新旧，立即失效；
      b) 仍 pending 且 created_at 超过 WORKFLOW_TASK_TTL_MIN —— 从未投递的陈旧任务。
    注意：仍活跃 step 上正常 running 的任务（有心跳的长任务）不会被误杀，交给 F1 按心跳判定。
    """
    import json as _json
    from app.models import AgentTask, WorkflowRun, WorkflowRunStep

    threshold = _now() - timedelta(minutes=WORKFLOW_TASK_TTL_MIN)
    rows = (AgentTask.query
            .filter(AgentTask.task_type == 'workflow_agent_task')
            .filter(AgentTask.status.in_(['pending', 'running']))
            .order_by(AgentTask.id.asc())
            .limit(500)
            .all())
    n = 0
    for t in rows:
        run_id = step_id = None
        try:
            payload = t.payload
            if isinstance(payload, str):
                payload = _json.loads(payload) if payload.strip().startswith('{') else {}
            if isinstance(payload, dict):
                run_id = payload.get('run_id')
                step_id = payload.get('step_id')
        except Exception:
            run_id = step_id = None

        expire, reason = False, ''
        if run_id and step_id:
            step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first()
            run = WorkflowRun.query.get(run_id)
            if (not step or step.status not in ('running', 'retrying')
                    or not run or run.status not in ('running', 'retrying')):
                expire, reason = True, 'workflow_run_or_step_inactive'
        if not expire and t.status == 'pending' and t.created_at and t.created_at < threshold:
            expire, reason = True, 'stale_pending_%dmin' % WORKFLOW_TASK_TTL_MIN
        if expire:
            t.status = 'failed'
            t.error = reason
            t.completed_at = _now()
            n += 1

    if n:
        try:
            db.session.commit()
            app.logger.info(f'[timeout_watcher] 失效陈旧 workflow AgentTask {n} 条')
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f'[timeout_watcher] 失效 workflow 任务提交失败: {e}')
            return 0
    return n


def _watcher_loop(app):
    """主循环：抢锁→扫描→sleep。"""
    from app import db

    pid_str = str(os.getpid())
    logger = app.logger

    # 启动错峰：4 个 worker 同时启动时，避免完全同步抢锁
    time.sleep(random.uniform(0, 5))

    logger.info(f'[timeout_watcher] 启动 pid={pid_str}')

    while True:
        try:
            with app.app_context():
                got_lock = _try_acquire_lock(db, pid_str, logger)
                if got_lock:
                    # F1：周期性刷新 workflow step 健康 —— 让无响应硬超时的 step 无需人工查看
                    # 也能自动重试/阻断（否则健康检查只在访问 workflow API 时触发）。
                    try:
                        from app.api.workflows import _refresh_workflow_step_health
                        _refresh_workflow_step_health(commit=True)
                    except Exception as e:
                        logger.warning(f'[timeout_watcher] workflow step 健康刷新异常: {e}')
                    # F2：过期 workflow 任务清理 —— 始终开启（算力/稳定性兜底，不受下面两个告警开关控制）
                    try:
                        wf_n = _scan_stale_workflow_agent_tasks(db, app)
                    except Exception as e:
                        wf_n = 0
                        logger.warning(f'[timeout_watcher] 过期 workflow 任务扫描异常: {e}')

                    # 读运行开关（存 system_config，DB 改完立即生效，不用重启）
                    todo_on = _get_switch(db, SWITCH_TODO_FALLBACK, default_on=False)
                    msg_on = _get_switch(db, SWITCH_MESSAGE_ALERT, default_on=False)

                    todo_n = _scan_overdue_todo_logs(db, app) if todo_on else 0
                    msg_n = _scan_stuck_claw_messages(db, app) if msg_on else 0
                    if todo_n or msg_n or wf_n:
                        logger.info(
                            f'[timeout_watcher] 一轮完成 pid={pid_str} '
                            f'兜底todo={todo_n} 告警msg={msg_n} 失效wf任务={wf_n} '
                            f'(switches: todo={todo_on}, msg={msg_on})')
                    sleep_sec = WATCHER_LOOP_INTERVAL_SEC
                else:
                    sleep_sec = WATCHER_IDLE_INTERVAL_SEC
        except Exception as e:
            logger.exception(f'[timeout_watcher] 循环异常 pid={pid_str}: {e}')
            sleep_sec = WATCHER_IDLE_INTERVAL_SEC

        time.sleep(sleep_sec)


def start_timeout_watcher(app):
    """在 create_app() 末尾调用一次。线程内部自抢锁，4 worker 安全。

    通过 ENV 开关 TIMEOUT_WATCHER_ENABLED=0 可关闭（用于本地调试）。
    """
    global _started
    if os.getenv('TIMEOUT_WATCHER_ENABLED', '1').strip() in ('0', 'false', 'False'):
        app.logger.info('[timeout_watcher] 已通过环境变量关闭')
        return

    with _started_lock:
        if _started:
            return
        _started = True

    t = threading.Thread(
        target=_watcher_loop, args=(app,),
        name='timeout-watcher', daemon=True,
    )
    t.start()
