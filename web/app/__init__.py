from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_cors import CORS
from config import config
import logging
import json

logger = logging.getLogger(__name__)

db = SQLAlchemy()
migrate = Migrate()

# SocketIO 实例（延迟初始化）
socketio = None


def create_app(config_name=None):
    if config_name is None:
        import os
        config_name = os.getenv('FLASK_ENV', 'default')

    app = Flask(__name__,
                static_folder='../static',
                template_folder='../templates')
    app.config.from_object(config[config_name])

    # 初始化扩展
    db.init_app(app)
    migrate.init_app(app, db)
    CORS(app)

    # 注册蓝图 - API
    from app.api import api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')

    # 注册蓝图 - 页面
    from app.views import views_bp
    app.register_blueprint(views_bp)

    # 注册 MCP 蓝图
    from app.api.mcp_protocol import mcp_bp
    app.register_blueprint(mcp_bp)

    # 注册 Agent Client 蓝图（SSE 任务推送）
    from app.api.agent_client import agent_bp
    app.register_blueprint(agent_bp, url_prefix='/api/openclaws')

    # 初始化 SocketIO 并注册 WebSocket 处理器
    from app.api.gateway_ws import init_socketio, socketio as _socketio
    globals()['socketio'] = init_socketio(app)

    # SSE 连接管理在线状态：连接建立=online，断开=offline（agent_client.py）
    # 不再需要后台心跳超时检测

    # 自动迁移：添加新字段
    with app.app_context():
        try:
            from sqlalchemy import text
            with db.engine.begin() as conn:
                # test_cases 表添加 module_path 和 created_by
                for col, coltype in [('module_path', 'VARCHAR(500) DEFAULT ""'), ('created_by', 'VARCHAR(100) DEFAULT ""')]:
                    try:
                        conn.execute(text(f'ALTER TABLE test_cases ADD COLUMN {col} {coltype}'))
                        logger.info(f'已添加 test_cases.{col} 列')
                    except Exception:
                        pass  # 列已存在

                # openclaw_instances.status: ENUM → VARCHAR(20) 支持中文状态
                try:
                    db_url = str(db.engine.url)
                    if 'mysql' in db_url:
                        conn.execute(text(
                            "ALTER TABLE openclaw_instances MODIFY COLUMN status VARCHAR(20) DEFAULT 'offline'"
                        ))
                        logger.info('openclaw_instances.status 已迁移为 VARCHAR(20) (MySQL)')
                    elif 'sqlite' in db_url:
                        logger.info('SQLite: status 列无需修改类型，将更新旧数据值')
                except Exception as e:
                    logger.info(f'status 列迁移跳过: {e}')

                # 旧状态值迁移: online→工作, busy→摸鱼
                try:
                    conn.execute(text("UPDATE openclaw_instances SET status='工作' WHERE status='online'"))
                    conn.execute(text("UPDATE openclaw_instances SET status='摸鱼' WHERE status='busy'"))
                    logger.info('旧状态值已迁移为中文状态')
                except Exception as e:
                    logger.info(f'旧状态值迁移跳过: {e}')

                # 列重命名: last_heartbeat → last_activity
                try:
                    if 'mysql' in db_url:
                        conn.execute(text(
                            "ALTER TABLE openclaw_instances CHANGE COLUMN last_heartbeat last_activity DATETIME"
                        ))
                        conn.execute(text(
                            "ALTER TABLE agents CHANGE COLUMN last_heartbeat last_activity DATETIME"
                        ))
                        logger.info('last_heartbeat → last_activity 重命名完成 (MySQL)')
                    elif 'sqlite' in db_url:
                        # SQLite 3.25+ 支持 ALTER TABLE RENAME COLUMN
                        try:
                            conn.execute(text(
                                "ALTER TABLE openclaw_instances RENAME COLUMN last_heartbeat TO last_activity"
                            ))
                            logger.info('openclaw_instances.last_heartbeat → last_activity 重命名完成 (SQLite)')
                        except Exception:
                            logger.info('openclaw_instances 列可能已重命名')
                        try:
                            conn.execute(text(
                                "ALTER TABLE agents RENAME COLUMN last_heartbeat TO last_activity"
                            ))
                            logger.info('agents.last_heartbeat → last_activity 重命名完成 (SQLite)')
                        except Exception:
                            logger.info('agents 列可能已重命名')
                except Exception as e:
                    logger.info(f'列重命名跳过: {e}')

                # skills 表添加软删除字段
                for col, coltype in [('is_deleted', 'BOOLEAN DEFAULT 0'), ('deleted_at', 'DATETIME DEFAULT NULL')]:
                    try:
                        conn.execute(text(f'ALTER TABLE skills ADD COLUMN {col} {coltype}'))
                        logger.info(f'已添加 skills.{col} 列')
                    except Exception:
                        pass

                # skills 表添加 rating 字段
                try:
                    conn.execute(text('ALTER TABLE skills ADD COLUMN rating FLOAT DEFAULT 3.0'))
                    logger.info('已添加 skills.rating 列')
                except Exception:
                    pass

                # rules 表添加软删除字段
                for col, coltype in [('is_deleted', 'BOOLEAN DEFAULT 0'), ('deleted_at', 'DATETIME DEFAULT NULL')]:
                    try:
                        conn.execute(text(f'ALTER TABLE rules ADD COLUMN {col} {coltype}'))
                        logger.info(f'已添加 rules.{col} 列')
                    except Exception:
                        pass

                # test_cases 表添加 is_placeholder 字段
                try:
                    conn.execute(text('ALTER TABLE test_cases ADD COLUMN is_placeholder BOOLEAN DEFAULT 0'))
                    logger.info('已添加 test_cases.is_placeholder 列')
                except Exception:
                    pass

                # topics 表添加 visibility 和用例评审关联字段
                for col, coltype in [
                    ('visibility', "VARCHAR(20) DEFAULT 'public'"),
                    ('review_library_id', 'INTEGER DEFAULT NULL'),
                    ('review_module_paths', 'LONGTEXT DEFAULT NULL'),
                    ('review_knowledge_id', 'INTEGER DEFAULT NULL'),
                ]:
                    try:
                        conn.execute(text(f'ALTER TABLE topics ADD COLUMN {col} {coltype}'))
                        logger.info(f'已添加 topics.{col} 列')
                    except Exception:
                        pass

                # 添加外键（如果列已添加成功）
                try:
                    conn.execute(text('ALTER TABLE topics ADD CONSTRAINT fk_topics_review_library FOREIGN KEY (review_library_id) REFERENCES test_case_libraries(id)'))
                except Exception:
                    pass
                try:
                    conn.execute(text('ALTER TABLE topics ADD CONSTRAINT fk_topics_review_knowledge FOREIGN KEY (review_knowledge_id) REFERENCES knowledge_entries(id)'))
                except Exception:
                    pass

                # 创建测试计划相关表（如果不存在）
                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS test_plans (
                            id INTEGER PRIMARY KEY AUTO_INCREMENT,
                            name VARCHAR(200) NOT NULL,
                            description TEXT,
                            version_type VARCHAR(20) DEFAULT 'regular',
                            version_name VARCHAR(100),
                            start_date DATE NOT NULL,
                            end_date DATE NOT NULL,
                            project_id INTEGER,
                            tapd_iteration_ids LONGTEXT,
                            tapd_iteration_names LONGTEXT,
                            tapd_workspace_id VARCHAR(50),
                            status VARCHAR(20) DEFAULT 'draft',
                            total_tasks INTEGER DEFAULT 0,
                            completed_tasks INTEGER DEFAULT 0,
                            total_bugs INTEGER DEFAULT 0,
                            resolved_bugs INTEGER DEFAULT 0,
                            created_by VARCHAR(100) DEFAULT '',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id)
                        )
                    """))
                    logger.info('test_plans 表已创建')
                except Exception as e:
                    logger.info(f'test_plans 表创建跳过: {e}')

                # test_plans 表迁移：旧字段 → 新字段
                try:
                    conn.execute(text(
                        "ALTER TABLE test_plans ADD COLUMN tapd_iteration_ids LONGTEXT"
                    ))
                    logger.info('已添加 test_plans.tapd_iteration_ids 列')
                except Exception:
                    pass
                try:
                    conn.execute(text(
                        "ALTER TABLE test_plans ADD COLUMN tapd_iteration_names LONGTEXT"
                    ))
                    logger.info('已添加 test_plans.tapd_iteration_names 列')
                except Exception:
                    pass
                # 迁移旧数据：tapd_iteration_id → tapd_iteration_ids
                try:
                    old_rows = conn.execute(text(
                        "SELECT id, tapd_iteration_id FROM test_plans WHERE tapd_iteration_id IS NOT NULL AND tapd_iteration_id != ''"
                    )).fetchall()
                    for row in old_rows:
                        old_id = row[1]
                        conn.execute(text(
                            "UPDATE test_plans SET tapd_iteration_ids = :ids WHERE id = :pid"
                        ), {'ids': json.dumps([old_id]), 'pid': row[0]})
                    if old_rows:
                        logger.info(f'已迁移 {len(old_rows)} 条 test_plans 迭代数据')
                except Exception as e:
                    logger.info(f'test_plans 迭代数据迁移跳过: {e}')

                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS test_tasks (
                            id INTEGER PRIMARY KEY AUTO_INCREMENT,
                            plan_id INTEGER NOT NULL,
                            name VARCHAR(200) NOT NULL,
                            description TEXT,
                            task_type VARCHAR(20) DEFAULT 'functional',
                            assignee_claw_id INTEGER,
                            start_date DATE,
                            end_date DATE,
                            priority VARCHAR(5) DEFAULT 'P2',
                            library_id INTEGER,
                            status VARCHAR(20) DEFAULT 'pending',
                            progress INTEGER DEFAULT 0,
                            result_summary TEXT,
                            total_cases INTEGER DEFAULT 0,
                            passed_cases INTEGER DEFAULT 0,
                            failed_cases INTEGER DEFAULT 0,
                            blocked_cases INTEGER DEFAULT 0,
                            skipped_cases INTEGER DEFAULT 0,
                            tapd_bug_ids LONGTEXT,
                            bug_count INTEGER DEFAULT 0,
                            created_by VARCHAR(100) DEFAULT '',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (plan_id) REFERENCES test_plans(id),
                            FOREIGN KEY (assignee_claw_id) REFERENCES openclaw_instances(id),
                            FOREIGN KEY (library_id) REFERENCES test_case_libraries(id)
                        )
                    """))
                    logger.info('test_tasks 表已创建')
                except Exception as e:
                    logger.info(f'test_tasks 表创建跳过: {e}')

                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS test_task_cases (
                            id INTEGER PRIMARY KEY AUTO_INCREMENT,
                            task_id INTEGER NOT NULL,
                            case_id INTEGER NOT NULL,
                            status VARCHAR(20) DEFAULT 'pending',
                            executed_at DATETIME,
                            executed_by VARCHAR(100),
                            note TEXT,
                            tapd_bug_id VARCHAR(50),
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (task_id) REFERENCES test_tasks(id),
                            FOREIGN KEY (case_id) REFERENCES test_cases(id),
                            UNIQUE KEY uq_task_case (task_id, case_id)
                        )
                    """))
                    logger.info('test_task_cases 表已创建')
                except Exception as e:
                    logger.info(f'test_task_cases 表创建跳过: {e}')

                # test_tasks 表迁移：添加 case_filter 列
                try:
                    conn.execute(text(
                        "ALTER TABLE test_tasks ADD COLUMN case_filter LONGTEXT"
                    ))
                    logger.info('已添加 test_tasks.case_filter 列')
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f'自动迁移检查异常: {e}')

    return app
