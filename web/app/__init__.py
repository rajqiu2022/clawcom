from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_cors import CORS
from config import config
import logging
import json
import os

logger = logging.getLogger(__name__)

db = SQLAlchemy()
migrate = Migrate()

# SocketIO 实例（延迟初始化）
socketio = None


def create_app(config_name=None):
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'default')

    app = Flask(__name__,
                static_folder='../static',
                template_folder='../templates')
    app.config.from_object(config[config_name])

    # 关闭严格尾斜杠（修复 MEMORY #155）
    # 默认 Flask 对路由 `/openclaws` 严格匹配，访问 `/openclaws/` 直接 404。
    # 龙虾王 / OpenClaw SDK 调用 `GET /api/v1/openclaws/` 就吃这个 404，
    # 误以为"权限/路径不对"。改为 False 后 `/foo` 和 `/foo/` 都能匹配，
    # 也不会触发 301 重定向丢 Bearer header（避免 POST/PUT 因重定向变 GET）。
    app.url_map.strict_slashes = False

    # 反向代理识别（nginx → gunicorn）
    # 外网域名 https://clawteam.woa.com 反代到内部 http://...:18800。
    # 必须信任 X-Forwarded-Proto，否则 Flask 把请求当 http：
    #   1) session cookie 在带 Secure 标记的环境下不会回写
    #   2) url_for(_external=True) 会生成 http:// 链接
    #   3) WOA SSO 回调 URL 也会被算成 http
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
        )
    except Exception as _pf_err:
        logger.warning('ProxyFix 初始化失败（继续运行）: %s', _pf_err)

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

    # 全局注入 hub_public_url 给所有模板使用。
    # 历史教训 #126b/#127：前端不能信 location.origin（浏览器可能从 https://clawteam.woa.com
    # 进，撞 lampp Apache 必 404；唯一对 claw 可达的真身入口是 http://your-hub-host:18800）。
    # 模板里用 {{ hub_public_url }} 拼"发给 claw / Agent 用的"URL（域名+端口）。
    # {{ hub_web_url }} 拼"给用户浏览器访问的"URL（https 域名）。
    import os as _os_ctx
    import time as _time_ctx
    _app_start_ts = str(int(_time_ctx.time()))
    @app.context_processor
    def _inject_hub_public_url():
        return {
            'hub_public_url': (
                _os_ctx.environ.get('HUB_PUBLIC_URL')
                or 'http://your-hub-host:18800'
            ).rstrip('/'),
            'hub_web_url': (
                _os_ctx.environ.get('HUB_WEB_URL')
                or 'https://clawteam.woa.com'
            ).rstrip('/'),
            'cache_bust': _app_start_ts,
        }

    # HTML 页面强制不缓存（避免 base.html / 子模板缓存导致 inline JS 与版本号不一致）。
    # 静态资源 (.js/.css/.png 等) 不受影响，仍走带 ?v=xxx 的强缓存。
    @app.after_request
    def _no_cache_html(resp):
        try:
            ct = (resp.headers.get('Content-Type') or '').lower()
            if ct.startswith('text/html'):
                resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                resp.headers['Pragma'] = 'no-cache'
                resp.headers['Expires'] = '0'
        except Exception:
            pass
        return resp

    # 兜底 session rollback：如果请求处理过程中 DB 操作异常导致 session 脏状态，
    # 这里确保清理，避免脏 session 泄漏到下一个请求（修复 #3 裸 commit 问题）。
    # 注意：不调用 db.session.remove()，因为 SSE 长连接场景中 teardown 触发时
    # streaming response 可能仍在使用 session 对象，remove 会导致
    # InvalidRequestError: Instance is not persistent within this Session。
    # scoped_session 本身在请求结束后（非 SSE）由 POOL_RECYCLE 和连接归还机制管理。
    @app.teardown_appcontext
    def _shutdown_session(exception=None):
        if exception:
            try:
                db.session.rollback()
            except Exception:
                pass

    # 全局 500 错误处理器：捕获未处理的 SQLAlchemy 异常，确保 rollback + 友好响应
    @app.errorhandler(500)
    def _handle_500(e):
        db.session.rollback()
        logger.exception('Unhandled 500 error: %s', e)
        from flask import jsonify as _jf
        return _jf({'error': '服务器内部错误，请稍后重试'}), 500

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
    if not os.getenv("SKIP_AUTO_MIGRATE"):
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

                    # skills 表添加私有 Skill 字段
                    for col, coltype in [
                        ('visibility', "VARCHAR(20) DEFAULT 'public'"),
                        ('owner_claw_id', 'INT DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE skills ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 skills.{col} 列')
                        except Exception:
                            pass

                    # skills 表添加镜像内容和历史记录字段
                    for col, coltype in [
                        ('mirror_content', 'LONGTEXT DEFAULT NULL'),
                        ('mirror_updated_by', 'VARCHAR(100) DEFAULT NULL'),
                        ('mirror_updated_at', 'DATETIME DEFAULT NULL'),
                        ('content_history', 'LONGTEXT DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE skills ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 skills.{col} 列')
                        except Exception:
                            pass

                    # skills 表添加最后修改人字段（不论镜像/直改/审核通过都会写入）
                    for col, coltype in [
                        ('last_modified_by', 'VARCHAR(100) DEFAULT NULL'),
                        ('last_modified_at', 'DATETIME DEFAULT NULL'),
                        ('last_modified_source', "VARCHAR(20) DEFAULT 'web'"),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE skills ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 skills.{col} 列')
                        except Exception:
                            pass

                    # rules 表添加软删除字段
                    for col, coltype in [('is_deleted', 'BOOLEAN DEFAULT 0'), ('deleted_at', 'DATETIME DEFAULT NULL')]:
                        try:
                            conn.execute(text(f'ALTER TABLE rules ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 rules.{col} 列')
                        except Exception:
                            pass

                    # rules 表添加镜像内容和历史记录字段
                    for col, coltype in [
                        ('mirror_content', 'LONGTEXT DEFAULT NULL'),
                        ('mirror_updated_by', 'VARCHAR(100) DEFAULT NULL'),
                        ('mirror_updated_at', 'DATETIME DEFAULT NULL'),
                        ('content_history', 'LONGTEXT DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE rules ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 rules.{col} 列')
                        except Exception:
                            pass

                    # rules 表添加最后修改人字段
                    for col, coltype in [
                        ('last_modified_by', 'VARCHAR(100) DEFAULT NULL'),
                        ('last_modified_at', 'DATETIME DEFAULT NULL'),
                        ('last_modified_source', "VARCHAR(20) DEFAULT 'web'"),
                    ]:
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

                    # 知识库收藏与匿名分享：建收藏表，并补分享状态和 token。
                    try:
                        from app.models import KnowledgeFavorite
                        KnowledgeFavorite.__table__.create(
                            bind=conn, checkfirst=True)
                        logger.info('knowledge_favorites 表已就绪')
                    except Exception as e:
                        logger.info(f'knowledge_favorites 表创建跳过: {e}')
                    for col, coltype in [
                        ('is_shared', 'BOOLEAN NOT NULL DEFAULT 0'),
                        ('share_token', 'VARCHAR(64) DEFAULT NULL'),
                        ('shared_at', 'DATETIME DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE knowledge_entries ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 knowledge_entries.{col} 列')
                        except Exception:
                            pass
                    try:
                        conn.execute(text(
                            'CREATE UNIQUE INDEX uq_knowledge_share_token '
                            'ON knowledge_entries (share_token)'))
                    except Exception:
                        pass

                    # topics 表添加 visibility 和用例评审关联字段
                    for col, coltype in [
                        ('visibility', "VARCHAR(20) DEFAULT 'public'"),
                        ('review_library_id', 'INTEGER DEFAULT NULL'),
                        ('review_module_paths', 'LONGTEXT DEFAULT NULL'),
                        ('review_knowledge_id', 'INTEGER DEFAULT NULL'),
                        ('review_case_ids', 'LONGTEXT DEFAULT NULL'),
                        ('review_status', "VARCHAR(20) DEFAULT 'reviewing'"),
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

                    # 创建用例评审轮次表
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS case_review_rounds (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                topic_id INTEGER NOT NULL,
                                round_number INTEGER DEFAULT 1,
                                description TEXT,
                                case_content LONGTEXT,
                                status VARCHAR(20) DEFAULT 'pending',
                                submitted_by VARCHAR(100) DEFAULT '',
                                submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (topic_id) REFERENCES topics(id),
                                INDEX ix_crr_topic (topic_id),
                                INDEX ix_crr_topic_round (topic_id, round_number)
                            )
                        """))
                        logger.info('case_review_rounds 表已创建')
                    except Exception as e:
                        logger.info(f'case_review_rounds 表创建跳过: {e}')

                    # 创建用例评审意见表
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS case_review_comments (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                round_id INTEGER NOT NULL,
                                author_name VARCHAR(100) NOT NULL,
                                author_claw_id INTEGER DEFAULT NULL,
                                author_user_id INTEGER DEFAULT NULL,
                                content TEXT NOT NULL,
                                verdict VARCHAR(20) DEFAULT 'comment',
                                score INTEGER DEFAULT NULL,
                                is_edited TINYINT(1) DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (round_id) REFERENCES case_review_rounds(id),
                                INDEX ix_crc_round (round_id)
                            )
                        """))
                        logger.info('case_review_comments 表已创建')
                    except Exception as e:
                        logger.info(f'case_review_comments 表创建跳过: {e}')

                    # 增量迁移：case_review_comments 新增 score/is_edited 列
                    for col_def in [
                        ("score", "INTEGER DEFAULT NULL"),
                        ("is_edited", "TINYINT(1) DEFAULT 0"),
                    ]:
                        try:
                            conn.execute(text(
                                f"ALTER TABLE case_review_comments ADD COLUMN {col_def[0]} {col_def[1]}"
                            ))
                            logger.info(f'case_review_comments 新增列 {col_def[0]}')
                        except Exception:
                            pass  # 已存在则跳过

                    # 增量迁移：topics 新增 review_summary 相关列
                    for col_def in [
                        ("review_summary", "TEXT DEFAULT NULL"),
                        ("review_summary_by", "VARCHAR(100) DEFAULT NULL"),
                        ("review_summary_at", "DATETIME DEFAULT NULL"),
                    ]:
                        try:
                            conn.execute(text(
                                f"ALTER TABLE topics ADD COLUMN {col_def[0]} {col_def[1]}"
                            ))
                            logger.info(f'topics 新增列 {col_def[0]}')
                        except Exception:
                            pass  # 已存在则跳过

                    # 增量迁移：case_review_rounds 新增 is_deleted/deleted_at 列
                    for col_def in [
                        ("is_deleted", "TINYINT(1) DEFAULT 0"),
                        ("deleted_at", "DATETIME DEFAULT NULL"),
                    ]:
                        try:
                            conn.execute(text(
                                f"ALTER TABLE case_review_rounds ADD COLUMN {col_def[0]} {col_def[1]}"
                            ))
                            logger.info(f'case_review_rounds 新增列 {col_def[0]}')
                        except Exception:
                            pass  # 已存在则跳过

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
                                tapd_bug_url VARCHAR(500),
                                case_info_snapshot JSON,
                                bug_sync_status VARCHAR(20) DEFAULT 'none',
                                bug_sync_error TEXT,
                                bug_synced_at DATETIME,
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

                    for col, coltype in [
                        ('tapd_bug_url', 'VARCHAR(500)'),
                        ('case_info_snapshot', 'LONGTEXT'),
                        ('bug_sync_status', "VARCHAR(20) DEFAULT 'none'"),
                        ('bug_sync_error', 'TEXT'),
                        ('bug_synced_at', 'DATETIME'),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE test_task_cases ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 test_task_cases.{col} 列')
                        except Exception:
                            pass

                    # 测试计划报告表（支持多份报告）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_plan_reports (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                plan_id INTEGER NOT NULL,
                                title VARCHAR(200) NOT NULL,
                                content LONGTEXT,
                                format VARCHAR(20) DEFAULT 'markdown',
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (plan_id) REFERENCES test_plans(id)
                            )
                        """))
                        logger.info('test_plan_reports 表已创建')
                    except Exception as e:
                        logger.info(f'test_plan_reports 表创建跳过: {e}')

                    # test_tasks 表迁移：添加 case_filter 列
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_tasks ADD COLUMN case_filter LONGTEXT"
                        ))
                        logger.info('已添加 test_tasks.case_filter 列')
                    except Exception:
                        pass

                    # test_tasks 表迁移：添加 assignee_username 列（直接分配给真人）
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_tasks ADD COLUMN assignee_username VARCHAR(100) DEFAULT ''"
                        ))
                        logger.info('已添加 test_tasks.assignee_username 列')
                    except Exception:
                        pass

                    # test_task_chains 表迁移：任务链执行结论字段（支持富文本）
                    for col, coltype in [
                        ('execution_conclusion', 'LONGTEXT DEFAULT NULL'),
                        ('conclusion_format', "VARCHAR(20) DEFAULT 'markdown'"),
                        ('conclusion_updated_by', 'VARCHAR(100) DEFAULT NULL'),
                        ('conclusion_updated_at', 'DATETIME DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE test_task_chains ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 test_task_chains.{col} 列')
                        except Exception:
                            pass

                    # 测试任务报告表（与测试计划报告类似，每个任务可有多份报告）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_task_reports (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                task_id INTEGER NOT NULL,
                                title VARCHAR(200) NOT NULL,
                                content LONGTEXT,
                                format VARCHAR(20) DEFAULT 'markdown',
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (task_id) REFERENCES test_tasks(id)
                            )
                        """))
                        logger.info('test_task_reports 表已创建')
                    except Exception as e:
                        logger.info(f'test_task_reports 表创建跳过: {e}')

                    # 测试任务Bug上报表
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_task_bug_reports (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                task_id INTEGER NOT NULL,
                                total_bugs INTEGER DEFAULT 0,
                                content LONGTEXT,
                                format VARCHAR(20) DEFAULT 'markdown',
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (task_id) REFERENCES test_tasks(id)
                            )
                        """))
                        logger.info('test_task_bug_reports 表已创建')
                    except Exception as e:
                        logger.info(f'test_task_bug_reports 表创建跳过: {e}')

                    # ===== 三级架构迁移：test_iterations 表 + test_plans.iteration_id =====
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_iterations (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                name VARCHAR(200) NOT NULL,
                                description TEXT,
                                version_name VARCHAR(100),
                                version_type VARCHAR(20) DEFAULT 'regular',
                                start_date DATE,
                                end_date DATE,
                                project_id INTEGER,
                                tapd_iteration_ids LONGTEXT,
                                tapd_iteration_names LONGTEXT,
                                tapd_workspace_id VARCHAR(50),
                                status VARCHAR(20) DEFAULT 'draft',
                                total_plans INTEGER DEFAULT 0,
                                total_tasks INTEGER DEFAULT 0,
                                completed_tasks INTEGER DEFAULT 0,
                                total_bugs INTEGER DEFAULT 0,
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (project_id) REFERENCES projects(id)
                            )
                        """))
                        logger.info('test_iterations 表已创建')
                    except Exception as e:
                        logger.info(f'test_iterations 表创建跳过: {e}')

                    # test_plans 添加 iteration_id 外键列
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_plans ADD COLUMN iteration_id INTEGER DEFAULT NULL"
                        ))
                        logger.info('已添加 test_plans.iteration_id 列')
                    except Exception:
                        pass
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_plans ADD CONSTRAINT fk_test_plans_iteration "
                            "FOREIGN KEY (iteration_id) REFERENCES test_iterations(id)"
                        ))
                    except Exception:
                        pass

                    # test_cases 添加 TAPD 需求绑定字段
                    for col, coltype in [
                        ('tapd_story_url', 'VARCHAR(500) DEFAULT NULL'),
                        ('tapd_story_title', 'VARCHAR(255) DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE test_cases ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 test_cases.{col} 列')
                        except Exception:
                            pass

                    # ===== 工程分析中心（Engineering Analysis Center）相关表 =====
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS engineering_baselines (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                project_id INTEGER NOT NULL,
                                name VARCHAR(200) NOT NULL,
                                repo_url VARCHAR(500) NOT NULL,
                                branch VARCHAR(100) NOT NULL DEFAULT 'main',
                                baseline_commit VARCHAR(64),
                                architecture_doc_url VARCHAR(500),
                                module_mapping LONGTEXT,
                                risk_rules LONGTEXT,
                                status VARCHAR(20) DEFAULT 'active',
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (project_id) REFERENCES projects(id)
                            )
                        """))
                        logger.info('engineering_baselines 表已创建')
                    except Exception as e:
                        logger.info(f'engineering_baselines 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS analysis_refresh_batches (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                project_id INTEGER NOT NULL,
                                baseline_id INTEGER NOT NULL,
                                iteration_id INTEGER,
                                refresh_type VARCHAR(20) DEFAULT 'manual',
                                from_commit VARCHAR(64),
                                to_commit VARCHAR(64),
                                commit_count INTEGER DEFAULT 0,
                                changed_file_count INTEGER DEFAULT 0,
                                summary TEXT,
                                risk_level VARCHAR(20),
                                status VARCHAR(20) DEFAULT 'queued',
                                assignee_claw_id INTEGER,
                                result_payload LONGTEXT,
                                error_message TEXT,
                                started_at DATETIME,
                                finished_at DATETIME,
                                triggered_by VARCHAR(100) DEFAULT '',
                                approved_by VARCHAR(100),
                                approved_at DATETIME,
                                reject_reason TEXT,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (project_id) REFERENCES projects(id),
                                FOREIGN KEY (baseline_id) REFERENCES engineering_baselines(id),
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                FOREIGN KEY (assignee_claw_id) REFERENCES openclaw_instances(id)
                            )
                        """))
                        logger.info('analysis_refresh_batches 表已创建')
                    except Exception as e:
                        logger.info(f'analysis_refresh_batches 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS engineering_change_items (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                batch_id INTEGER NOT NULL,
                                file_path VARCHAR(500) NOT NULL,
                                change_type VARCHAR(20) DEFAULT 'modify',
                                module_id INTEGER,
                                module_name VARCHAR(100) DEFAULT '',
                                symbol_names LONGTEXT,
                                impact_tags LONGTEXT,
                                risk_score INTEGER DEFAULT 0,
                                reason TEXT,
                                tapd_story_ids LONGTEXT,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (batch_id) REFERENCES analysis_refresh_batches(id),
                                FOREIGN KEY (module_id) REFERENCES modules(id)
                            )
                        """))
                        logger.info('engineering_change_items 表已创建')
                    except Exception as e:
                        logger.info(f'engineering_change_items 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS engineering_test_impact_items (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                batch_id INTEGER NOT NULL,
                                library_id INTEGER,
                                module_id INTEGER,
                                module_name VARCHAR(100) DEFAULT '',
                                feature_chain VARCHAR(500),
                                action_type VARCHAR(20) DEFAULT 'update_case',
                                priority VARCHAR(5) DEFAULT 'P2',
                                suggestion TEXT,
                                acceptance_criteria TEXT,
                                owner VARCHAR(100) DEFAULT '',
                                status VARCHAR(20) DEFAULT 'todo',
                                linked_test_task_id INTEGER,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (batch_id) REFERENCES analysis_refresh_batches(id),
                                FOREIGN KEY (library_id) REFERENCES test_case_libraries(id),
                                FOREIGN KEY (module_id) REFERENCES modules(id),
                                FOREIGN KEY (linked_test_task_id) REFERENCES test_tasks(id)
                            )
                        """))
                        logger.info('engineering_test_impact_items 表已创建')
                    except Exception as e:
                        logger.info(f'engineering_test_impact_items 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS engineering_test_case_links (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                impact_item_id INTEGER NOT NULL,
                                test_case_id INTEGER NOT NULL,
                                link_type VARCHAR(20) DEFAULT 'affected',
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (impact_item_id) REFERENCES engineering_test_impact_items(id),
                                FOREIGN KEY (test_case_id) REFERENCES test_cases(id),
                                UNIQUE KEY uq_impact_case_link (impact_item_id, test_case_id)
                            )
                        """))
                        logger.info('engineering_test_case_links 表已创建')
                    except Exception as e:
                        logger.info(f'engineering_test_case_links 表创建跳过: {e}')

                    # 工程架构分析快照（双子模块改版：full=全工程 / module=单模块深度）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS engineering_architecture_snapshots (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                baseline_id INTEGER NOT NULL,
                                scope VARCHAR(20) NOT NULL DEFAULT 'full',
                                target_module VARCHAR(100) DEFAULT '',
                                title VARCHAR(200),
                                summary TEXT,
                                content_md LONGTEXT,
                                structured LONGTEXT,
                                analyzed_commit VARCHAR(64),
                                source_type VARCHAR(20) DEFAULT 'agent',
                                triggered_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (baseline_id) REFERENCES engineering_baselines(id),
                                INDEX ix_arch_snap_baseline (baseline_id),
                                INDEX ix_arch_snap_created (baseline_id, created_at)
                            )
                        """))
                        logger.info('engineering_architecture_snapshots 表已创建')
                    except Exception as e:
                        logger.info(f'engineering_architecture_snapshots 表创建跳过: {e}')

                    # ===== 需求分析中心（Requirement Analysis Center）相关表 =====
                    # JSON 字段统一使用 LONGTEXT 兼容 MariaDB 10.1
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_items (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                iteration_id INTEGER NOT NULL,
                                tapd_story_id VARCHAR(64) NOT NULL,
                                tapd_workspace_id VARCHAR(50),
                                tapd_iteration_id VARCHAR(64),
                                title VARCHAR(500) DEFAULT '',
                                description TEXT,
                                description_text TEXT,
                                status VARCHAR(40),
                                priority_label VARCHAR(40),
                                priority_num INTEGER DEFAULT 0,
                                owner VARCHAR(500) DEFAULT '',
                                creator VARCHAR(100) DEFAULT '',
                                developer VARCHAR(500) DEFAULT '',
                                category_id VARCHAR(64),
                                workitem_type_id VARCHAR(64),
                                tapd_module VARCHAR(200) DEFAULT '',
                                feature VARCHAR(200) DEFAULT '',
                                local_module_name VARCHAR(200) DEFAULT '',
                                tapd_version VARCHAR(100),
                                tapd_release_id VARCHAR(64),
                                tapd_baseline_id VARCHAR(64),
                                acceptance_criteria TEXT,
                                test_focus TEXT,
                                test_suggestions LONGTEXT,
                                test_result TEXT,
                                need_test VARCHAR(40),
                                review_progress VARCHAR(40),
                                parent_id VARCHAR(64),
                                children_id VARCHAR(500),
                                tree_path VARCHAR(500),
                                progress INTEGER DEFAULT 0,
                                effort FLOAT DEFAULT 0,
                                effort_completed FLOAT DEFAULT 0,
                                remain FLOAT DEFAULT 0,
                                tech_risk VARCHAR(200),
                                tapd_created_at DATETIME,
                                tapd_modified_at DATETIME,
                                tapd_completed_at DATETIME,
                                tapd_begin DATE,
                                tapd_due DATE,
                                risk_score INTEGER DEFAULT 0,
                                risk_level VARCHAR(20) DEFAULT 'low',
                                local_test_status VARCHAR(20) DEFAULT 'pending',
                                local_synced_at DATETIME,
                                raw_payload LONGTEXT,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                UNIQUE KEY uq_req_item_iter_story (iteration_id, tapd_story_id),
                                INDEX ix_req_item_status (status),
                                INDEX ix_req_item_version (tapd_version),
                                INDEX ix_req_item_baseline (tapd_baseline_id)
                            )
                        """))
                        logger.info('requirement_items 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_items 表创建跳过: {e}')

                    # requirement_items 增量字段迁移
                    for col, coltype in [
                        ('impl_status', "VARCHAR(20) DEFAULT 'unknown'"),
                        ('impl_remark', "VARCHAR(500) DEFAULT ''"),
                        ('test_suggestions', 'LONGTEXT'),
                        ('completeness', "VARCHAR(20) DEFAULT ''"),
                        ('completeness_desc', 'TEXT'),
                    ]:
                        try:
                            conn.execute(text(f'ALTER TABLE requirement_items ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 requirement_items.{col} 列')
                        except Exception:
                            pass

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_change_logs (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                requirement_item_id INTEGER NOT NULL,
                                iteration_id INTEGER NOT NULL,
                                change_date DATE,
                                change_type VARCHAR(20) NOT NULL,
                                field_name VARCHAR(80) DEFAULT '',
                                old_value TEXT,
                                new_value TEXT,
                                diff_summary TEXT,
                                impact_level VARCHAR(10) DEFAULT 'low',
                                processed BOOLEAN DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (requirement_item_id) REFERENCES requirement_items(id),
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                UNIQUE KEY uq_req_change_dedup (requirement_item_id, change_date, field_name, change_type),
                                INDEX ix_req_change_date (change_date),
                                INDEX ix_req_change_created (created_at)
                            )
                        """))
                        logger.info('requirement_change_logs 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_change_logs 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_engineering_links (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                requirement_item_id INTEGER NOT NULL,
                                change_item_id INTEGER NOT NULL,
                                link_source VARCHAR(20) DEFAULT 'auto_tapd_id',
                                confidence INTEGER DEFAULT 90,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (requirement_item_id) REFERENCES requirement_items(id),
                                FOREIGN KEY (change_item_id) REFERENCES engineering_change_items(id),
                                UNIQUE KEY uq_req_eng_link (requirement_item_id, change_item_id)
                            )
                        """))
                        logger.info('requirement_engineering_links 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_engineering_links 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_function_links (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                requirement_item_id INTEGER NOT NULL,
                                change_item_id INTEGER NOT NULL,
                                file_path VARCHAR(500) NOT NULL DEFAULT '',
                                symbol_name VARCHAR(120) NOT NULL DEFAULT '',
                                start_line INTEGER DEFAULT NULL,
                                end_line INTEGER DEFAULT NULL,
                                link_source VARCHAR(20) DEFAULT 'auto_symbol',
                                confidence INTEGER DEFAULT 90,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (requirement_item_id) REFERENCES requirement_items(id),
                                FOREIGN KEY (change_item_id) REFERENCES engineering_change_items(id),
                                UNIQUE KEY uq_req_func_link (requirement_item_id, change_item_id, symbol_name)
                            )
                        """))
                        logger.info('requirement_function_links 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_function_links 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_testcase_links (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                requirement_item_id INTEGER NOT NULL,
                                test_case_id INTEGER NOT NULL,
                                link_type VARCHAR(20) DEFAULT 'covers',
                                coverage_status VARCHAR(20) DEFAULT 'covered',
                                created_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (requirement_item_id) REFERENCES requirement_items(id),
                                FOREIGN KEY (test_case_id) REFERENCES test_cases(id),
                                UNIQUE KEY uq_req_case_link (requirement_item_id, test_case_id)
                            )
                        """))
                        logger.info('requirement_testcase_links 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_testcase_links 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS tapd_versions (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                project_id INTEGER,
                                tapd_workspace_id VARCHAR(50) NOT NULL,
                                tapd_version_id VARCHAR(64) NOT NULL,
                                name VARCHAR(200) DEFAULT '',
                                description TEXT,
                                status VARCHAR(40),
                                version_type VARCHAR(40) DEFAULT 'Normal version',
                                start DATE,
                                due DATE,
                                realbegin DATE,
                                realend DATE,
                                testtime DATE,
                                releasetime DATE,
                                creator VARCHAR(100) DEFAULT '',
                                owner VARCHAR(500) DEFAULT '',
                                tapd_created_at DATETIME,
                                tapd_modified_at DATETIME,
                                local_synced_at DATETIME,
                                FOREIGN KEY (project_id) REFERENCES projects(id),
                                UNIQUE KEY uq_tapd_version (tapd_workspace_id, tapd_version_id)
                            )
                        """))
                        logger.info('tapd_versions 表已创建')
                    except Exception as e:
                        logger.info(f'tapd_versions 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS tapd_baselines (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                tapd_workspace_id VARCHAR(50) NOT NULL,
                                tapd_baseline_id VARCHAR(64) NOT NULL,
                                tapd_version_id_str VARCHAR(64),
                                version_id INTEGER,
                                name VARCHAR(200) DEFAULT '',
                                creator VARCHAR(100) DEFAULT '',
                                tapd_created_at DATETIME,
                                story_count INTEGER DEFAULT 0,
                                stories_snapshot LONGTEXT,
                                local_synced_at DATETIME,
                                FOREIGN KEY (version_id) REFERENCES tapd_versions(id),
                                UNIQUE KEY uq_tapd_baseline (tapd_workspace_id, tapd_baseline_id)
                            )
                        """))
                        logger.info('tapd_baselines 表已创建')
                    except Exception as e:
                        logger.info(f'tapd_baselines 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS tapd_iterations_cache (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                tapd_workspace_id VARCHAR(50) NOT NULL,
                                tapd_iteration_id VARCHAR(64) NOT NULL,
                                name VARCHAR(200) DEFAULT '',
                                status VARCHAR(40),
                                startdate DATE,
                                enddate DATE,
                                creator VARCHAR(100) DEFAULT '',
                                description TEXT,
                                parent_id VARCHAR(64),
                                last_synced_at DATETIME,
                                cache_version INTEGER DEFAULT 1,
                                UNIQUE KEY uq_tapd_iter_cache (tapd_workspace_id, tapd_iteration_id),
                                INDEX ix_tapd_iter_ws_status (tapd_workspace_id, status),
                                INDEX ix_tapd_iter_synced (last_synced_at)
                            )
                        """))
                        logger.info('tapd_iterations_cache 表已创建')
                    except Exception as e:
                        logger.info(f'tapd_iterations_cache 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS tapd_field_map_cache (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                tapd_workspace_id VARCHAR(50) NOT NULL,
                                entity_type VARCHAR(20) NOT NULL DEFAULT 'story',
                                field_map LONGTEXT,
                                last_synced_at DATETIME,
                                UNIQUE KEY uq_tapd_field_map (tapd_workspace_id, entity_type)
                            )
                        """))
                        logger.info('tapd_field_map_cache 表已创建')
                    except Exception as e:
                        logger.info(f'tapd_field_map_cache 表创建跳过: {e}')

                    # ===== 用例库共享授权 / 评审记录（参考 EngineeringShare 设计） =====
                    # 1) test_case_libraries 加 review_status / current_review_id / review_status_at
                    for col, coltype in [
                        ('review_status', "VARCHAR(20) DEFAULT 'draft'"),
                        ('current_review_id', 'INTEGER DEFAULT NULL'),
                        ('review_status_at', 'DATETIME DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE test_case_libraries ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 test_case_libraries.{col} 列')
                        except Exception:
                            pass
                    # 历史脏数据兜底：把空值 review_status 设为 'draft'
                    try:
                        conn.execute(text(
                            "UPDATE test_case_libraries SET review_status='draft' "
                            "WHERE review_status IS NULL OR review_status=''"))
                    except Exception as e:
                        logger.info(f'review_status 默认值回填跳过: {e}')

                    # 2) 共享授权表
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_case_library_shares (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                library_id INTEGER NOT NULL,
                                share_type VARCHAR(20) NOT NULL,
                                target_user_id INTEGER,
                                target_claw_id INTEGER,
                                permission VARCHAR(20) DEFAULT 'reviewer',
                                granted_by VARCHAR(100) DEFAULT '',
                                note VARCHAR(500) DEFAULT '',
                                expires_at DATETIME,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (library_id) REFERENCES test_case_libraries(id),
                                FOREIGN KEY (target_user_id) REFERENCES users(id),
                                FOREIGN KEY (target_claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_tcl_share_lib (library_id),
                                INDEX ix_tcl_share_user (share_type, target_user_id),
                                INDEX ix_tcl_share_claw (share_type, target_claw_id)
                            )
                        """))
                        logger.info('test_case_library_shares 表已创建')
                    except Exception as e:
                        logger.info(f'test_case_library_shares 表创建跳过: {e}')

                    # 旧表迁移：补 permission 列（针对早期未带 permission 的部署）
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_case_library_shares "
                            "ADD COLUMN permission VARCHAR(20) DEFAULT 'reviewer'"))
                        logger.info('已添加 test_case_library_shares.permission 列')
                    except Exception:
                        pass

                    # 3) 评审记录表
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_case_library_reviews (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                library_id INTEGER NOT NULL,
                                status VARCHAR(20) DEFAULT 'submitted',
                                submitted_by VARCHAR(100) DEFAULT '',
                                submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                submit_note TEXT,
                                scope_summary VARCHAR(500) DEFAULT '',
                                invited_reviewers LONGTEXT,
                                decided_by VARCHAR(100),
                                decided_at DATETIME,
                                decision_note TEXT,
                                related_topic_id INTEGER,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (library_id) REFERENCES test_case_libraries(id),
                                INDEX ix_tcl_review_lib (library_id),
                                INDEX ix_tcl_review_status (status),
                                INDEX ix_tcl_review_lib_status (library_id, status)
                            )
                        """))
                        logger.info('test_case_library_reviews 表已创建')
                    except Exception as e:
                        logger.info(f'test_case_library_reviews 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS tapd_refresh_requests (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                scope VARCHAR(20) NOT NULL DEFAULT 'iterations',
                                tapd_workspace_id VARCHAR(50),
                                iteration_id INTEGER,
                                tapd_iteration_id VARCHAR(64),
                                status VARCHAR(20) DEFAULT 'pending',
                                requested_by VARCHAR(100) DEFAULT '',
                                picked_by_claw_id INTEGER,
                                picked_at DATETIME,
                                finished_at DATETIME,
                                error_message TEXT,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                FOREIGN KEY (picked_by_claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_tapd_refresh_status (status),
                                INDEX ix_tapd_refresh_created (created_at)
                            )
                        """))
                        logger.info('tapd_refresh_requests 表已创建')
                    except Exception as e:
                        logger.info(f'tapd_refresh_requests 表创建跳过: {e}')

                    # ===== 需求分析图谱（Requirement Analysis Graph）=====
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_domain_clusters (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                iteration_id INTEGER NOT NULL,
                                domain_name VARCHAR(50) NOT NULL,
                                requirement_count INTEGER DEFAULT 0,
                                avg_score FLOAT DEFAULT 0.0,
                                top_issues LONGTEXT,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                UNIQUE KEY uq_req_domain_cluster (iteration_id, domain_name)
                            )
                        """))
                        logger.info('requirement_domain_clusters 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_domain_clusters 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS requirement_consistency_issues (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                iteration_id INTEGER NOT NULL,
                                issue_type VARCHAR(30) NOT NULL,
                                severity VARCHAR(10) DEFAULT '中',
                                domain VARCHAR(50) DEFAULT '',
                                requirement_ids LONGTEXT,
                                detail TEXT,
                                status VARCHAR(20) DEFAULT 'open',
                                resolved_at DATETIME,
                                resolved_by VARCHAR(100),
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                INDEX ix_req_ci_iteration (iteration_id),
                                INDEX ix_req_ci_status (status)
                            )
                        """))
                        logger.info('requirement_consistency_issues 表已创建')
                    except Exception as e:
                        logger.info(f'requirement_consistency_issues 表创建跳过: {e}')

                    # ===== B+ 方案：OpenClaw ↔ Hub 通信稳定化 =====
                    # 1) openclaw_instances 加 owner_wecom_userid（owner 的企微 ID）
                    try:
                        conn.execute(text(
                            "ALTER TABLE openclaw_instances "
                            "ADD COLUMN owner_wecom_userid VARCHAR(64) DEFAULT NULL "
                            "COMMENT 'owner 在企微的 userid/RTX 名（如 rajqiu），用于 Hub 集中代发企微消息'"
                        ))
                        logger.info('已添加 openclaw_instances.owner_wecom_userid 列')
                    except Exception:
                        pass

                    # 1b) openclaw_instances 加 safe_name（目录名安全版本，第一次创建时生成，后续不变）
                    try:
                        conn.execute(text(
                            "ALTER TABLE openclaw_instances "
                            "ADD COLUMN safe_name VARCHAR(50) DEFAULT '' "
                            "COMMENT '目录名安全版本（第一次创建时生成，后续不变）'"
                        ))
                        logger.info('已添加 openclaw_instances.safe_name 列')
                        # 为现有记录填充 safe_name（从 name 字段生成）
                        from app.services.agent_deployer import _safe_name
                        claws = conn.execute(text("SELECT id, name FROM openclaw_instances WHERE safe_name = '' OR safe_name IS NULL")).fetchall()
                        for row in claws:
                            claw_id, name = row[0], row[1]
                            safe = _safe_name(name or f'claw-{claw_id}')
                            if not safe or safe == 'unnamed':
                                safe = f'claw-{claw_id}'
                            conn.execute(text(
                                f"UPDATE openclaw_instances SET safe_name = '{safe}' WHERE id = {claw_id}"
                            ))
                        logger.info(f'已为 {len(claws)} 条记录填充 safe_name')
                    except Exception as e:
                        logger.info(f'safe_name 列迁移跳过: {e}')

                    # 2) claw_messages 状态机扩展：pending/delivered/processing/done/failed/read
                    try:
                        conn.execute(text(
                            "ALTER TABLE claw_messages "
                            "MODIFY COLUMN status VARCHAR(20) DEFAULT 'pending' "
                            "COMMENT '状态：pending/delivered/processing/done/failed/read'"
                        ))
                        logger.info('claw_messages.status 列已扩展')
                    except Exception as e:
                        logger.info(f'claw_messages.status 扩展跳过: {e}')

                    # 3) claw_messages 加上失败原因和处理时间戳
                    for col, coltype in [
                        ('processing_at', 'DATETIME DEFAULT NULL'),
                        ('done_at', 'DATETIME DEFAULT NULL'),
                        ('failed_reason', 'TEXT DEFAULT NULL'),
                        ('llm_response', 'LONGTEXT DEFAULT NULL'),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE claw_messages ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 claw_messages.{col} 列')
                        except Exception:
                            pass

                    # 3a) claw_messages 增加 from_claw_id（支持 claw→claw / claw→hub 追踪发送方）
                    try:
                        conn.execute(text(
                            'ALTER TABLE claw_messages ADD COLUMN from_claw_id INT DEFAULT NULL'
                        ))
                        logger.info('已添加 claw_messages.from_claw_id 列')
                    except Exception:
                        pass

                    # 4) sidecar 配置中心表（Web 后台改完自动下发到 sidecar）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS claw_sidecar_configs (
                                claw_id INTEGER PRIMARY KEY,
                                agent_type VARCHAR(20) DEFAULT 'openclaw',
                                openclaw_bin VARCHAR(500) DEFAULT '',
                                hermes_home VARCHAR(500) DEFAULT '',
                                agent_name VARCHAR(50) DEFAULT 'main',
                                agent_timeout INTEGER DEFAULT 300,
                                wecom_enabled BOOLEAN DEFAULT 0,
                                enabled BOOLEAN DEFAULT 1,
                                config_version INTEGER DEFAULT 1,
                                sidecar_version VARCHAR(30) DEFAULT '',
                                last_heartbeat_at DATETIME DEFAULT NULL,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                updated_by VARCHAR(100) DEFAULT '',
                                FOREIGN KEY (claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_sidecar_cfg_heartbeat (last_heartbeat_at)
                            )
                        """))
                        logger.info('claw_sidecar_configs 表已创建')
                    except Exception as e:
                        logger.info(f'claw_sidecar_configs 表创建跳过: {e}')

                    # 4b) 历史环境补齐 claw_sidecar_configs 新增列（safe_name / llm）
                    for col, coltype in [
                        ('safe_name', "VARCHAR(50) DEFAULT ''"),
                        ('llm_provider', "VARCHAR(50) DEFAULT 'venus'"),
                        ('llm_model', "VARCHAR(100) DEFAULT 'venus'"),
                        ('system_context_policy_json', "LONGTEXT DEFAULT NULL"),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE claw_sidecar_configs ADD COLUMN {col} {coltype}'
                            ))
                            logger.info(f'已添加 claw_sidecar_configs.{col} 列')
                        except Exception:
                            pass

                    # 4c) 执行后校验记录表（/ops/verify 从 DB 重读比对，堵"自以为成功"）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS claw_ops_verifications (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                claw_id INTEGER DEFAULT NULL,
                                token VARCHAR(100) DEFAULT NULL,
                                resource_type VARCHAR(50) NOT NULL,
                                resource_id INTEGER NOT NULL,
                                expected TEXT,
                                actual TEXT,
                                mismatches TEXT,
                                verified BOOLEAN DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                verified_at DATETIME DEFAULT NULL,
                                INDEX ix_ops_verify_claw (claw_id),
                                INDEX ix_ops_verify_token (token),
                                INDEX ix_ops_verify_res (resource_type, resource_id)
                            )
                        """))
                        logger.info('claw_ops_verifications 表已创建')
                    except Exception as e:
                        logger.info(f'claw_ops_verifications 表创建跳过: {e}')

                    # 5a) claw_todo_logs 加 notified_at / notified_strategy（B+ 5分钟兜底用）
                    for col, coltype in [
                        ('notified_at', 'DATETIME DEFAULT NULL'),
                        ('notified_strategy', "VARCHAR(30) DEFAULT ''"),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE claw_todo_logs ADD COLUMN {col} {coltype}'))
                            logger.info(f'已添加 claw_todo_logs.{col} 列')
                        except Exception:
                            pass

                    # 5c) Hub 代建 Hermes Agent 部署记录表
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS agent_deployments (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                openclaw_id INTEGER NOT NULL,
                                agent_type VARCHAR(20) DEFAULT 'hermes',
                                deploy_method VARCHAR(20) DEFAULT 'docker',
                                host VARCHAR(255) DEFAULT '',
                                ssh_user VARCHAR(64) DEFAULT '',
                                remote_base_dir VARCHAR(500) DEFAULT '',
                                container_name VARCHAR(100) DEFAULT '',
                                image VARCHAR(255) DEFAULT '',
                                status VARCHAR(20) DEFAULT 'pending',
                                log_tail TEXT,
                                error_message TEXT,
                                started_at DATETIME,
                                finished_at DATETIME,
                                triggered_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (openclaw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_agent_deploy_claw (openclaw_id),
                                INDEX ix_agent_deploy_status (status),
                                INDEX ix_agent_deploy_created (created_at)
                            )
                        """))
                        logger.info('agent_deployments 表已创建')
                    except Exception as e:
                        logger.info(f'agent_deployments 表创建跳过: {e}')

                    # 5c-1) Claw Worker 不可变发布版本与部署溯源
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS worker_releases (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                release_id VARCHAR(80) NOT NULL,
                                source_repository VARCHAR(500) DEFAULT '',
                                source_ref VARCHAR(120) DEFAULT '',
                                source_commit VARCHAR(40) NOT NULL,
                                committed_at VARCHAR(40) DEFAULT '',
                                channel VARCHAR(30) DEFAULT 'candidate',
                                signature_status VARCHAR(30) DEFAULT 'unsigned',
                                release_manifest_sha256 VARCHAR(64) NOT NULL,
                                platform VARCHAR(40) NOT NULL DEFAULT 'linux-x86_64',
                                platform_manifest_sha256 VARCHAR(64) NOT NULL,
                                package_manifest_sha256 VARCHAR(64) NOT NULL,
                                artifact_filename VARCHAR(255) NOT NULL,
                                artifact_sha256 VARCHAR(64) NOT NULL,
                                artifact_size BIGINT NOT NULL,
                                artifact_path VARCHAR(1000) NOT NULL,
                                approval_status VARCHAR(20) DEFAULT 'candidate',
                                is_default BOOLEAN NOT NULL DEFAULT 0,
                                imported_by VARCHAR(100) DEFAULT 'system',
                                imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                approved_by VARCHAR(100) DEFAULT '',
                                approved_at DATETIME DEFAULT NULL,
                                rejected_by VARCHAR(100) DEFAULT '',
                                rejected_at DATETIME DEFAULT NULL,
                                UNIQUE KEY uq_worker_release_platform (release_id, platform),
                                INDEX ix_worker_release_approval
                                    (platform, approval_status, is_default)
                            )
                        """))
                    except Exception as e:
                        logger.info(f'worker_releases 表创建跳过: {e}')
                    for col, coltype in [
                        ('worker_release_record_id', 'INTEGER DEFAULT NULL'),
                        ('worker_release_id', "VARCHAR(80) DEFAULT ''"),
                        ('worker_source_commit', "VARCHAR(40) DEFAULT ''"),
                        ('worker_artifact_sha256', "VARCHAR(64) DEFAULT ''"),
                    ]:
                        try:
                            conn.execute(text(
                                f'ALTER TABLE agent_deployments ADD COLUMN {col} {coltype}'))
                        except Exception:
                            pass

                    # 5d) Agent 模板库（超管管理，支持文件隔离存储）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS agent_role_templates (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                template_key VARCHAR(80) NOT NULL UNIQUE,
                                name VARCHAR(120) NOT NULL,
                                profile_name VARCHAR(120) DEFAULT '',
                                role_name VARCHAR(80) DEFAULT '',
                                main_responsibility TEXT,
                                skills_summary TEXT,
                                rules_summary TEXT,
                                schedules_summary TEXT,
                                installed_skill_ids LONGTEXT,
                                rule_ids LONGTEXT,
                                schedule_items LONGTEXT,
                                owner_claw_id INTEGER DEFAULT NULL,
                                status VARCHAR(20) DEFAULT 'draft',
                                current_version INTEGER DEFAULT 1,
                                review_comment TEXT,
                                created_by VARCHAR(100) DEFAULT 'system',
                                reviewed_by VARCHAR(100) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                INDEX ix_art_status (status),
                                INDEX ix_art_owner_claw (owner_claw_id)
                            )
                        """))
                        logger.info('agent_role_templates 表已创建')
                    except Exception as e:
                        logger.info(f'agent_role_templates 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS agent_role_template_files (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                template_id INTEGER NOT NULL,
                                relative_path VARCHAR(500) NOT NULL,
                                file_name VARCHAR(255) NOT NULL,
                                mime_type VARCHAR(120) DEFAULT '',
                                file_size INTEGER DEFAULT 0,
                                sha256 VARCHAR(64) DEFAULT '',
                                uploaded_by VARCHAR(100) DEFAULT 'system',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                UNIQUE KEY uq_artf_tpl_path (template_id, relative_path(191)),
                                INDEX ix_artf_template (template_id)
                            )
                        """))
                        logger.info('agent_role_template_files 表已创建')
                    except Exception as e:
                        logger.info(f'agent_role_template_files 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS agent_role_template_versions (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                template_id INTEGER NOT NULL,
                                version_no INTEGER NOT NULL,
                                action VARCHAR(30) DEFAULT 'save',
                                change_note VARCHAR(255) DEFAULT '',
                                snapshot_payload LONGTEXT NOT NULL,
                                created_by VARCHAR(100) DEFAULT 'system',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                UNIQUE KEY uq_artv_template_version (template_id, version_no),
                                INDEX ix_artv_template (template_id),
                                INDEX ix_artv_created (created_at)
                            )
                        """))
                        logger.info('agent_role_template_versions 表已创建')
                    except Exception as e:
                        logger.info(f'agent_role_template_versions 表创建跳过: {e}')

                    # 5b) 企微发送日志表（所有 wecom send 尝试都留痕，方便排障）
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS wecom_send_logs (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                claw_id INTEGER,
                                related_type VARCHAR(30) DEFAULT '',
                                related_id INTEGER DEFAULT NULL,
                                target_userid VARCHAR(64) DEFAULT '',
                                content TEXT,
                                strategy VARCHAR(30) DEFAULT '',
                                status VARCHAR(20) DEFAULT 'pending',
                                error TEXT,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                sent_at DATETIME DEFAULT NULL,
                                FOREIGN KEY (claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_wecom_log_claw (claw_id),
                                INDEX ix_wecom_log_status (status),
                                INDEX ix_wecom_log_created (created_at)
                            )
                        """))
                        logger.info('wecom_send_logs 表已创建')
                    except Exception as e:
                        logger.info(f'wecom_send_logs 表创建跳过: {e}')

                    # ===== 见闻分享（KM/文章/行业洞察的轻量分享 + 评论） =====
                    # 区别于知识库（结构化沉淀）/ 课题讨论（聚焦议题）：
                    # 这里是「转发 + 个人见解 + 全员评论」，不强制评审、不强结构。
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS shared_articles (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                title VARCHAR(200) NOT NULL,
                                summary VARCHAR(500) DEFAULT '',
                                content LONGTEXT,
                                source_url VARCHAR(500) DEFAULT '',
                                source_name VARCHAR(120) DEFAULT '',
                                category VARCHAR(30) DEFAULT 'other',
                                tags LONGTEXT,
                                sharer_type VARCHAR(10) DEFAULT 'user',
                                sharer_user_id INTEGER DEFAULT NULL,
                                sharer_claw_id INTEGER DEFAULT NULL,
                                sharer_name VARCHAR(80) NOT NULL,
                                view_count INTEGER DEFAULT 0,
                                comment_count INTEGER DEFAULT 0,
                                like_count INTEGER DEFAULT 0,
                                is_deleted BOOLEAN DEFAULT 0,
                                deleted_at DATETIME DEFAULT NULL,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                FOREIGN KEY (sharer_user_id) REFERENCES users(id),
                                FOREIGN KEY (sharer_claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_shared_articles_category (category),
                                INDEX ix_shared_articles_created (created_at),
                                INDEX ix_shared_articles_sharer (sharer_type, sharer_user_id, sharer_claw_id)
                            )
                        """))
                        logger.info('shared_articles 表已创建')
                    except Exception as e:
                        logger.info(f'shared_articles 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS shared_article_comments (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                article_id INTEGER NOT NULL,
                                parent_id INTEGER DEFAULT NULL,
                                content TEXT NOT NULL,
                                commenter_type VARCHAR(10) DEFAULT 'user',
                                commenter_user_id INTEGER DEFAULT NULL,
                                commenter_claw_id INTEGER DEFAULT NULL,
                                commenter_name VARCHAR(80) NOT NULL,
                                status VARCHAR(20) DEFAULT 'active',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (article_id) REFERENCES shared_articles(id),
                                FOREIGN KEY (parent_id) REFERENCES shared_article_comments(id),
                                FOREIGN KEY (commenter_user_id) REFERENCES users(id),
                                FOREIGN KEY (commenter_claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_shared_article_comments_article (article_id),
                                INDEX ix_shared_article_comments_created (created_at)
                            )
                        """))
                        logger.info('shared_article_comments 表已创建')
                    except Exception as e:
                        logger.info(f'shared_article_comments 表创建跳过: {e}')

                    # ---------------- 全局测试报告中心（MEMORY #134）----------------
                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_reports (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                title VARCHAR(200) NOT NULL,
                                report_type VARCHAR(40) NOT NULL DEFAULT 'other_specialized',
                                custom_category_key VARCHAR(120) DEFAULT '',
                                remark VARCHAR(500) DEFAULT '',
                                project_id INTEGER NOT NULL,
                                iteration_id INTEGER DEFAULT NULL,
                                version_name VARCHAR(100) DEFAULT '',
                                content LONGTEXT,
                                format VARCHAR(20) DEFAULT 'markdown',
                                risk_level VARCHAR(20) DEFAULT 'tbd',
                                source_ref_type VARCHAR(40) DEFAULT 'manual',
                                source_ref_id INTEGER DEFAULT NULL,
                                status VARCHAR(20) DEFAULT 'draft',
                                submitter_type VARCHAR(20) DEFAULT 'user',
                                submitter_user_id INTEGER DEFAULT NULL,
                                submitter_claw_id INTEGER DEFAULT NULL,
                                submitter_name VARCHAR(120) DEFAULT '',
                                is_shared TINYINT(1) DEFAULT 0,
                                share_token VARCHAR(64) DEFAULT NULL UNIQUE,
                                shared_at DATETIME DEFAULT NULL,
                                is_deleted TINYINT(1) DEFAULT 0,
                                deleted_at DATETIME DEFAULT NULL,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (project_id) REFERENCES projects(id),
                                FOREIGN KEY (iteration_id) REFERENCES test_iterations(id),
                                FOREIGN KEY (submitter_user_id) REFERENCES users(id),
                                FOREIGN KEY (submitter_claw_id) REFERENCES openclaw_instances(id),
                                INDEX ix_test_reports_proj_type (project_id, report_type),
                                INDEX ix_test_reports_custom_category (custom_category_key),
                                INDEX ix_test_reports_proj_iter (project_id, iteration_id),
                                INDEX ix_test_reports_source (source_ref_type, source_ref_id),
                                INDEX ix_test_reports_deleted (is_deleted)
                            )
                        """))
                        logger.info('test_reports 表已创建')
                    except Exception as e:
                        logger.info(f'test_reports 表创建跳过: {e}')

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_report_custom_categories (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                title VARCHAR(120) NOT NULL UNIQUE,
                                description VARCHAR(500) DEFAULT '',
                                created_by VARCHAR(120) DEFAULT '',
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                INDEX ix_test_report_custom_categories_title (title)
                            )
                        """))
                        logger.info('test_report_custom_categories 表已创建')
                    except Exception as e:
                        logger.info(f'test_report_custom_categories 表创建跳过: {e}')

                    try:
                        conn.execute(text(
                            "ALTER TABLE test_reports "
                            "ADD COLUMN custom_category_key VARCHAR(120) DEFAULT '' "
                            "COMMENT '自定义报告类别 key'"
                        ))
                        logger.info('test_reports.custom_category_key 已添加')
                    except Exception:
                        pass
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_reports "
                            "ADD INDEX ix_test_reports_custom_category (custom_category_key)"
                        ))
                    except Exception:
                        pass

                    try:
                        conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS test_report_attachments (
                                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                                report_id INTEGER NOT NULL,
                                filename VARCHAR(255) NOT NULL,
                                stored_name VARCHAR(255) NOT NULL,
                                size_bytes BIGINT DEFAULT 0,
                                content_type VARCHAR(120) DEFAULT 'application/octet-stream',
                                uploaded_by VARCHAR(120) DEFAULT '',
                                uploaded_by_user_id INTEGER DEFAULT NULL,
                                uploaded_by_claw_id INTEGER DEFAULT NULL,
                                is_deleted TINYINT(1) DEFAULT 0,
                                uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (report_id) REFERENCES test_reports(id),
                                INDEX ix_test_report_attachments_report (report_id)
                            )
                        """))
                        logger.info('test_report_attachments 表已创建')
                    except Exception as e:
                        logger.info(f'test_report_attachments 表创建跳过: {e}')

                    # 给旧表加 linked_test_report_id（反向追溯）。重复 ALTER 会报错，吞掉。
                    for legacy_tbl in ('test_plan_reports', 'test_task_reports'):
                        try:
                            conn.execute(text(
                                f'ALTER TABLE {legacy_tbl} '
                                'ADD COLUMN linked_test_report_id INTEGER DEFAULT NULL'
                            ))
                            logger.info(f'{legacy_tbl}.linked_test_report_id 已添加')
                        except Exception:
                            pass  # 列已存在

                    # test_reports.status 字段补丁（MEMORY #134.C v2 新增）
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_reports "
                            "ADD COLUMN status VARCHAR(20) DEFAULT 'draft'"
                        ))
                        # 已经存在的报告默认视为 published（向后兼容旧记录）
                        conn.execute(text(
                            "UPDATE test_reports SET status='published' "
                            "WHERE status='draft' OR status IS NULL"
                        ))
                        # 同时把 risk_level='tbd' 的展示标签改了，但 key 保持 tbd（前端取 label）
                        logger.info('test_reports.status 列已添加，存量记录默认 published')
                    except Exception:
                        pass  # 列已存在

                    # test_reports.is_hidden 字段补丁（隐藏报告：Web 列表不显示，分享链接仍有效）
                    try:
                        conn.execute(text(
                            "ALTER TABLE test_reports "
                            "ADD COLUMN is_hidden TINYINT(1) DEFAULT 0"
                        ))
                        logger.info('test_reports.is_hidden 列已添加')
                    except Exception:
                        pass  # 列已存在

                    # 一次性历史数据回迁（仅在 test_reports 为空时执行，避免重复）
                    try:
                        existing = conn.execute(
                            text('SELECT COUNT(*) FROM test_reports')
                        ).scalar() or 0
                        if existing == 0:
                            # TestPlanReport -> TestReport
                            conn.execute(text("""
                                INSERT INTO test_reports
                                    (title, report_type, remark, project_id,
                                     iteration_id, version_name, content, format,
                                     risk_level, status, source_ref_type, source_ref_id,
                                     submitter_type, submitter_name,
                                     created_at, updated_at)
                                SELECT
                                    tpr.title, 'feature_test', '',
                                    COALESCE(tp.project_id,
                                             (SELECT project_id FROM test_iterations
                                              WHERE id = tp.iteration_id)),
                                    tp.iteration_id,
                                    COALESCE(tp.version_name, ''),
                                    tpr.content, tpr.format, 'tbd', 'published',
                                    'test_plan', tpr.plan_id,
                                    'user', COALESCE(tpr.created_by, ''),
                                    tpr.created_at, tpr.updated_at
                                FROM test_plan_reports tpr
                                JOIN test_plans tp ON tp.id = tpr.plan_id
                                WHERE COALESCE(tp.project_id,
                                               (SELECT project_id FROM test_iterations
                                                WHERE id = tp.iteration_id)) IS NOT NULL
                            """))
                            # TestTaskReport -> TestReport
                            conn.execute(text("""
                                INSERT INTO test_reports
                                    (title, report_type, remark, project_id,
                                     iteration_id, version_name, content, format,
                                     risk_level, status, source_ref_type, source_ref_id,
                                     submitter_type, submitter_name,
                                     created_at, updated_at)
                                SELECT
                                    ttr.title, 'feature_test', '',
                                    COALESCE(tp.project_id,
                                             (SELECT project_id FROM test_iterations
                                              WHERE id = tp.iteration_id)),
                                    tp.iteration_id,
                                    COALESCE(tp.version_name, ''),
                                    ttr.content, ttr.format, 'tbd', 'published',
                                    'test_task', ttr.task_id,
                                    'user', COALESCE(ttr.created_by, ''),
                                    ttr.created_at, ttr.updated_at
                                FROM test_task_reports ttr
                                JOIN test_tasks tt ON tt.id = ttr.task_id
                                JOIN test_plans tp ON tp.id = tt.plan_id
                                WHERE COALESCE(tp.project_id,
                                               (SELECT project_id FROM test_iterations
                                                WHERE id = tp.iteration_id)) IS NOT NULL
                            """))
                            # 反向 link
                            conn.execute(text("""
                                UPDATE test_plan_reports tpr
                                JOIN test_reports tr
                                  ON tr.source_ref_type = 'test_plan'
                                 AND tr.source_ref_id = tpr.plan_id
                                 AND tr.title = tpr.title
                                 AND tr.created_at = tpr.created_at
                                SET tpr.linked_test_report_id = tr.id
                            """))
                            conn.execute(text("""
                                UPDATE test_task_reports ttr
                                JOIN test_reports tr
                                  ON tr.source_ref_type = 'test_task'
                                 AND tr.source_ref_id = ttr.task_id
                                 AND tr.title = ttr.title
                                 AND tr.created_at = ttr.created_at
                                SET ttr.linked_test_report_id = tr.id
                            """))
                            logger.info('test_reports 历史数据已回迁')
                    except Exception as e:
                        logger.info(f'test_reports 回迁跳过: {e}')

                    # users 表添加 last_login_at 字段
                    try:
                        conn.execute(text(
                            "ALTER TABLE users ADD COLUMN last_login_at DATETIME DEFAULT NULL "
                            "COMMENT '最后登录时间'"
                        ))
                        logger.info('已添加 users.last_login_at 列')
                    except Exception:
                        pass

                    # ===== 游戏功能模块全景视图表 =====
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_workspaces (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            title VARCHAR(180) NOT NULL,
                            description TEXT DEFAULT NULL,
                            is_default TINYINT(1) DEFAULT 0,
                            created_by VARCHAR(120) DEFAULT NULL,
                            updated_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            UNIQUE KEY uq_panorama_workspace_title (project_id, title),
                            INDEX idx_project_id (project_id),
                            INDEX idx_is_default (is_default)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS game_module_panorama (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            parent_id INT DEFAULT NULL,
                            name VARCHAR(200) NOT NULL,
                            path VARCHAR(500) DEFAULT NULL,
                            description TEXT DEFAULT NULL,
                            code_paths LONGTEXT DEFAULT NULL,
                            resource_paths LONGTEXT DEFAULT NULL,
                            test_focus TEXT DEFAULT NULL,
                            related_case_libraries LONGTEXT DEFAULT NULL,
                            status VARCHAR(20) DEFAULT 'active',
                            risk_level VARCHAR(20) DEFAULT 'normal',
                            last_change_summary TEXT DEFAULT NULL,
                            last_changed_at DATETIME DEFAULT NULL,
                            extra LONGTEXT DEFAULT NULL,
                            created_by VARCHAR(120) DEFAULT NULL,
                            updated_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            FOREIGN KEY (parent_id) REFERENCES game_module_panorama(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS game_module_change_logs (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            module_id INT NOT NULL,
                            change_type VARCHAR(30) NOT NULL,
                            summary TEXT NOT NULL,
                            detail LONGTEXT DEFAULT NULL,
                            affected_cases LONGTEXT DEFAULT NULL,
                            test_suggestion TEXT DEFAULT NULL,
                            risk_level VARCHAR(20) DEFAULT 'normal',
                            source VARCHAR(50) DEFAULT NULL,
                            created_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (module_id) REFERENCES game_module_panorama(id),
                            INDEX idx_module_id (module_id),
                            INDEX idx_created_at (created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS game_module_relations (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            source_module_id INT NOT NULL,
                            target_module_id INT NOT NULL,
                            relation_type VARCHAR(40) NOT NULL DEFAULT 'depends_on',
                            confidence FLOAT DEFAULT 1.0,
                            evidence LONGTEXT DEFAULT NULL,
                            source VARCHAR(50) DEFAULT 'manual',
                            created_by VARCHAR(120) DEFAULT NULL,
                            updated_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            FOREIGN KEY (source_module_id) REFERENCES game_module_panorama(id),
                            FOREIGN KEY (target_module_id) REFERENCES game_module_panorama(id),
                            UNIQUE KEY uq_game_module_relation (
                                workspace_id, source_module_id, target_module_id, relation_type
                            ),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_source_module_id (source_module_id),
                            INDEX idx_target_module_id (target_module_id),
                            INDEX idx_relation_type (relation_type)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_code_entities (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            repo_key VARCHAR(120) DEFAULT 'default',
                            file_path VARCHAR(1000) NOT NULL,
                            entity_type VARCHAR(30) NOT NULL DEFAULT 'file',
                            symbol_name VARCHAR(300) DEFAULT '',
                            language VARCHAR(50) DEFAULT '',
                            start_line INT DEFAULT 0,
                            end_line INT DEFAULT 0,
                            content_hash VARCHAR(120) DEFAULT '',
                            extra LONGTEXT DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            UNIQUE KEY uq_panorama_code_entity (
                                workspace_id, repo_key, file_path(191), entity_type, symbol_name(100), start_line
                            ),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_repo_key (repo_key),
                            INDEX idx_file_path (file_path(255))
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_module_code_links (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            module_id INT NOT NULL,
                            entity_id INT NOT NULL,
                            link_type VARCHAR(30) NOT NULL DEFAULT 'owns',
                            confidence FLOAT DEFAULT 1.0,
                            evidence LONGTEXT DEFAULT NULL,
                            source VARCHAR(50) DEFAULT 'agent',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            FOREIGN KEY (module_id) REFERENCES game_module_panorama(id),
                            FOREIGN KEY (entity_id) REFERENCES panorama_code_entities(id),
                            UNIQUE KEY uq_panorama_module_code_link (module_id, entity_id, link_type),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_module_id (module_id),
                            INDEX idx_entity_id (entity_id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_impact_analyses (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            input_type VARCHAR(30) DEFAULT 'changed_files',
                            input_payload LONGTEXT DEFAULT NULL,
                            affected_modules LONGTEXT DEFAULT NULL,
                            affected_relations LONGTEXT DEFAULT NULL,
                            recommended_case_libraries LONGTEXT DEFAULT NULL,
                            risk_score INT DEFAULT 0,
                            risk_level VARCHAR(20) DEFAULT 'low',
                            test_context LONGTEXT DEFAULT NULL,
                            token_savings LONGTEXT DEFAULT NULL,
                            summary TEXT DEFAULT NULL,
                            created_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_created_at (created_at),
                            INDEX idx_risk_level (risk_level)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_snapshots (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            name VARCHAR(200) NOT NULL,
                            description TEXT DEFAULT NULL,
                            module_count INT DEFAULT 0,
                            relation_count INT DEFAULT 0,
                            code_entity_count INT DEFAULT 0,
                            created_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_created_at (created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_snapshot_items (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            snapshot_id INT NOT NULL,
                            item_type VARCHAR(30) NOT NULL,
                            item_key VARCHAR(500) NOT NULL,
                            payload LONGTEXT DEFAULT NULL,
                            FOREIGN KEY (snapshot_id) REFERENCES panorama_snapshots(id),
                            UNIQUE KEY uq_panorama_snapshot_item (snapshot_id, item_key(191)),
                            INDEX idx_snapshot_id (snapshot_id),
                            INDEX idx_item_type (item_type)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS testcase_panorama_links (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            module_id INT NOT NULL,
                            library_id INT NOT NULL,
                            module_path VARCHAR(500) DEFAULT '',
                            case_pk INT DEFAULT NULL,
                            link_level VARCHAR(20) DEFAULT 'library',
                            case_count INT DEFAULT 0,
                            source VARCHAR(50) DEFAULT 'manual',
                            confidence FLOAT DEFAULT 1.0,
                            created_by VARCHAR(120) DEFAULT NULL,
                            updated_by VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            last_verified_at DATETIME DEFAULT NULL,
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            FOREIGN KEY (module_id) REFERENCES game_module_panorama(id),
                            FOREIGN KEY (library_id) REFERENCES test_case_libraries(id),
                            FOREIGN KEY (case_pk) REFERENCES test_cases(id),
                            UNIQUE KEY uq_testcase_panorama_link (
                                workspace_id, module_id, library_id, module_path(191), case_pk, link_level
                            ),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_module_id (module_id),
                            INDEX idx_library_id (library_id),
                            INDEX idx_case_pk (case_pk),
                            INDEX idx_link_level (link_level)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS panorama_module_test_metrics (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            module_id INT NOT NULL,
                            project_id INT DEFAULT NULL,
                            workspace_id INT DEFAULT NULL,
                            direct_case_count INT DEFAULT 0,
                            subtree_case_count INT DEFAULT 0,
                            linked_library_count INT DEFAULT 0,
                            linked_directory_count INT DEFAULT 0,
                            bug_count INT DEFAULT 0,
                            bug_risk_score INT DEFAULT 0,
                            bug_risk_level VARCHAR(20) DEFAULT 'low',
                            metrics_payload LONGTEXT DEFAULT NULL,
                            source VARCHAR(50) DEFAULT 'sync',
                            updated_by VARCHAR(120) DEFAULT NULL,
                            synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            FOREIGN KEY (module_id) REFERENCES game_module_panorama(id),
                            FOREIGN KEY (project_id) REFERENCES projects(id),
                            FOREIGN KEY (workspace_id) REFERENCES panorama_workspaces(id),
                            UNIQUE KEY uq_panorama_module_test_metric (module_id),
                            INDEX idx_project_id (project_id),
                            INDEX idx_workspace_id (workspace_id),
                            INDEX idx_bug_risk_level (bug_risk_level)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS test_case_change_logs (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            library_id INT NOT NULL,
                            case_pk INT DEFAULT NULL,
                            case_id VARCHAR(100) DEFAULT '',
                            case_title VARCHAR(255) DEFAULT '',
                            module_path VARCHAR(500) DEFAULT '',
                            change_type VARCHAR(30) NOT NULL,
                            changed_fields LONGTEXT DEFAULT NULL,
                            old_snapshot LONGTEXT DEFAULT NULL,
                            new_snapshot LONGTEXT DEFAULT NULL,
                            operation_id VARCHAR(80) DEFAULT '',
                            linked_panorama_modules LONGTEXT DEFAULT NULL,
                            changed_by VARCHAR(120) DEFAULT '',
                            changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            source VARCHAR(50) DEFAULT 'web',
                            FOREIGN KEY (library_id) REFERENCES test_case_libraries(id),
                            INDEX idx_library_id (library_id),
                            INDEX idx_case_pk (case_pk),
                            INDEX idx_module_path (module_path(191)),
                            INDEX idx_change_type (change_type),
                            INDEX idx_operation_id (operation_id),
                            INDEX idx_changed_at (changed_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    logger.info('功能全景模块/关系/代码实体/影响分析/快照/用例关联表已就绪')

                    # 增量迁移：功能全景 workspace_id
                    for table in (
                        'game_module_panorama',
                        'game_module_relations',
                        'panorama_code_entities',
                        'panorama_module_code_links',
                        'panorama_impact_analyses',
                        'panorama_snapshots',
                    ):
                        try:
                            conn.execute(text(
                                f"ALTER TABLE {table} ADD COLUMN workspace_id INT DEFAULT NULL "
                                "COMMENT '功能全景标题/工作区 ID'"
                            ))
                            logger.info(f'{table}.workspace_id 已添加')
                        except Exception:
                            pass
                        try:
                            conn.execute(text(
                                f"ALTER TABLE {table} ADD INDEX idx_workspace_id (workspace_id)"
                            ))
                        except Exception:
                            pass

                    # 旧索引按 project_id 去重会阻止同项目多标题写入相同代码路径。
                    try:
                        conn.execute(text("ALTER TABLE game_module_relations DROP INDEX uq_game_module_relation"))
                    except Exception:
                        pass
                    try:
                        conn.execute(text("""
                            ALTER TABLE game_module_relations
                            ADD UNIQUE KEY uq_game_module_relation (
                                workspace_id, source_module_id, target_module_id, relation_type
                            )
                        """))
                    except Exception:
                        pass
                    try:
                        conn.execute(text("ALTER TABLE panorama_code_entities DROP INDEX uq_panorama_code_entity"))
                    except Exception:
                        pass
                    try:
                        conn.execute(text("""
                            ALTER TABLE panorama_code_entities
                            ADD UNIQUE KEY uq_panorama_code_entity (
                                workspace_id, repo_key, file_path(191),
                                entity_type, symbol_name(100), start_line
                            )
                        """))
                    except Exception:
                        pass

                    # 为所有已有项目/全局全景创建默认工作区。
                    try:
                        conn.execute(text("""
                            INSERT INTO panorama_workspaces
                                (project_id, title, description, is_default, created_by, updated_by)
                            SELECT DISTINCT src.project_id, '项目功能全景图',
                                   '历史数据自动迁移生成的默认功能全景', 1, 'system', 'system'
                            FROM (
                                SELECT project_id FROM game_module_panorama
                                UNION SELECT project_id FROM game_module_relations
                                UNION SELECT project_id FROM panorama_code_entities
                                UNION SELECT project_id FROM panorama_impact_analyses
                                UNION SELECT project_id FROM panorama_snapshots
                            ) src
                            LEFT JOIN panorama_workspaces w
                              ON ((w.project_id = src.project_id)
                                  OR (w.project_id IS NULL AND src.project_id IS NULL))
                             AND w.is_default = 1
                            WHERE w.id IS NULL
                        """))
                    except Exception as e:
                        logger.info(f'panorama 默认工作区创建跳过: {e}')

                    # 回填 workspace_id：按 project_id 绑定默认工作区。
                    for table in (
                        'game_module_panorama',
                        'game_module_relations',
                        'panorama_code_entities',
                        'panorama_module_code_links',
                        'panorama_impact_analyses',
                        'panorama_snapshots',
                    ):
                        try:
                            conn.execute(text(f"""
                                UPDATE {table} t
                                JOIN panorama_workspaces w
                                  ON ((w.project_id = t.project_id)
                                      OR (w.project_id IS NULL AND t.project_id IS NULL))
                                 AND w.is_default = 1
                                SET t.workspace_id = w.id
                                WHERE t.workspace_id IS NULL
                            """))
                        except Exception as e:
                            logger.info(f'{table}.workspace_id 回填跳过: {e}')

                    # 增量迁移: game_module_panorama 添加 is_new 列
                    try:
                        conn.execute(text("ALTER TABLE game_module_panorama ADD COLUMN is_new TINYINT(1) DEFAULT 0 COMMENT '是否新增模块，由Agent标记'"))
                        logger.info('game_module_panorama 添加 is_new 列成功')
                    except Exception:
                        pass

                    # 增量迁移: game_module_panorama 添加 highlight 列
                    try:
                        conn.execute(text("ALTER TABLE game_module_panorama ADD COLUMN highlight TINYINT(1) DEFAULT 0 COMMENT '本轮是否有变更，Agent每轮更新时标记'"))
                        logger.info('game_module_panorama 添加 highlight 列成功')
                    except Exception:
                        pass

                    # ===== Agent 考试系统 =====
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS exam_papers (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            name VARCHAR(200) NOT NULL,
                            description TEXT DEFAULT NULL,
                            category VARCHAR(50) DEFAULT 'general',
                            difficulty VARCHAR(20) DEFAULT 'normal',
                            total_score INT DEFAULT 100,
                            pass_score INT DEFAULT 60,
                            time_limit_min INT DEFAULT 60,
                            applicable_skill_ids LONGTEXT DEFAULT NULL,
                            remark TEXT DEFAULT NULL,
                            status VARCHAR(20) DEFAULT 'draft',
                            is_deleted TINYINT(1) DEFAULT 0,
                            created_by VARCHAR(120) DEFAULT NULL,
                            created_by_user_id INT DEFAULT NULL,
                            created_by_claw_id INT DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            INDEX idx_status (status),
                            INDEX idx_category (category)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    for col, ddl in (
                        ('remark', "ALTER TABLE exam_papers ADD COLUMN remark TEXT DEFAULT NULL COMMENT '试卷备注'"),
                        ('created_by_user_id', "ALTER TABLE exam_papers ADD COLUMN created_by_user_id INT DEFAULT NULL COMMENT '创建用户 ID'"),
                        ('created_by_claw_id', "ALTER TABLE exam_papers ADD COLUMN created_by_claw_id INT DEFAULT NULL COMMENT '创建 Agent ID'"),
                    ):
                        try:
                            conn.execute(text(ddl))
                            logger.info('exam_papers 添加 %s 列成功', col)
                        except Exception:
                            pass
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS exam_campaigns (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            paper_id INT NOT NULL,
                            name VARCHAR(200) NOT NULL,
                            scope VARCHAR(20) NOT NULL,
                            project_id INT DEFAULT NULL,
                            target_claw_ids LONGTEXT DEFAULT NULL,
                            starts_at DATETIME DEFAULT NULL,
                            ends_at DATETIME NOT NULL,
                            status VARCHAR(20) DEFAULT 'scheduled',
                            remark TEXT DEFAULT NULL,
                            launched_by_user_id INT DEFAULT NULL,
                            launched_by_claw_id INT DEFAULT NULL,
                            launched_by_display VARCHAR(120) DEFAULT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            INDEX idx_paper (paper_id),
                            INDEX idx_scope (scope),
                            INDEX idx_project (project_id),
                            INDEX idx_ends_at (ends_at),
                            FOREIGN KEY (paper_id) REFERENCES exam_papers(id),
                            FOREIGN KEY (project_id) REFERENCES projects(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS exam_questions (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            paper_id INT NOT NULL,
                            order_index INT DEFAULT 0,
                            type VARCHAR(30) NOT NULL,
                            title VARCHAR(500) NOT NULL,
                            description TEXT DEFAULT NULL,
                            options LONGTEXT DEFAULT NULL,
                            points INT DEFAULT 10,
                            standard_answer LONGTEXT DEFAULT NULL,
                            grading_criteria TEXT DEFAULT NULL,
                            auto_grade_script TEXT DEFAULT NULL,
                            skill_tag VARCHAR(80) DEFAULT NULL,
                            is_deleted TINYINT(1) DEFAULT 0,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            INDEX idx_paper (paper_id),
                            FOREIGN KEY (paper_id) REFERENCES exam_papers(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS exam_sessions (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            paper_id INT NOT NULL,
                            campaign_id INT DEFAULT NULL,
                            examinee_claw_id INT DEFAULT NULL,
                            examinee_user_id INT DEFAULT NULL,
                            examinee_display VARCHAR(120) DEFAULT NULL,
                            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            submitted_at DATETIME DEFAULT NULL,
                            deadline_at DATETIME DEFAULT NULL,
                            status VARCHAR(20) DEFAULT 'in_progress',
                            auto_score INT DEFAULT 0,
                            manual_score INT DEFAULT 0,
                            total_score INT DEFAULT 0,
                            passed TINYINT(1) DEFAULT 0,
                            summary TEXT DEFAULT NULL,
                            INDEX idx_paper (paper_id),
                            INDEX idx_campaign (campaign_id),
                            INDEX idx_examinee_claw (examinee_claw_id),
                            INDEX idx_status (status),
                            FOREIGN KEY (paper_id) REFERENCES exam_papers(id),
                            FOREIGN KEY (campaign_id) REFERENCES exam_campaigns(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    try:
                        conn.execute(text("ALTER TABLE exam_sessions ADD COLUMN campaign_id INT DEFAULT NULL COMMENT '所属考试场次'"))
                        conn.execute(text("ALTER TABLE exam_sessions ADD INDEX idx_campaign (campaign_id)"))
                        logger.info('exam_sessions 添加 campaign_id 列成功')
                    except Exception:
                        pass
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS exam_answers (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            session_id INT NOT NULL,
                            question_id INT NOT NULL,
                            answer_content TEXT DEFAULT NULL,
                            auto_score INT DEFAULT NULL,
                            peer_scores LONGTEXT DEFAULT NULL,
                            final_score INT DEFAULT 0,
                            grading_notes TEXT DEFAULT NULL,
                            answered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_session (session_id),
                            INDEX idx_question (question_id),
                            FOREIGN KEY (session_id) REFERENCES exam_sessions(id),
                            FOREIGN KEY (question_id) REFERENCES exam_questions(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS exam_peer_reviews (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            session_id INT NOT NULL,
                            reviewer_claw_id INT DEFAULT NULL,
                            reviewer_user_id INT DEFAULT NULL,
                            reviewer_display VARCHAR(120) DEFAULT NULL,
                            status VARCHAR(20) DEFAULT 'pending',
                            invited_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            completed_at DATETIME DEFAULT NULL,
                            INDEX idx_session (session_id),
                            INDEX idx_reviewer_claw (reviewer_claw_id),
                            FOREIGN KEY (session_id) REFERENCES exam_sessions(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    logger.info('exam_papers / exam_questions / exam_sessions / exam_answers / exam_peer_reviews 表已就绪')

                    # ===== Skill 密钥保险箱 =====
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS claw_secrets (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            owner_claw_id INT DEFAULT NULL,
                            owner_user_id INT DEFAULT NULL,
                            `key` VARCHAR(120) NOT NULL,
                            encrypted_value LONGTEXT NOT NULL,
                            description VARCHAR(500) DEFAULT NULL,
                            last_used_at DATETIME DEFAULT NULL,
                            use_count INT DEFAULT 0,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            UNIQUE KEY uq_claw_secret_owner_key (owner_claw_id, owner_user_id, `key`),
                            INDEX idx_claw_secret_owner (owner_claw_id, owner_user_id),
                            FOREIGN KEY (owner_claw_id) REFERENCES openclaw_instances(id),
                            FOREIGN KEY (owner_user_id) REFERENCES users(id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """))
                    logger.info('claw_secrets 表已就绪')

                    # 增量迁移：share_scope / share_project_id
                    for col, ddl in [
                        ('share_scope', "ALTER TABLE claw_secrets ADD COLUMN share_scope VARCHAR(20) DEFAULT 'private' COMMENT '共享范围：private/project/public'"),
                        ('share_project_id', "ALTER TABLE claw_secrets ADD COLUMN share_project_id INT DEFAULT NULL COMMENT '共享项目ID'"),
                    ]:
                        try:
                            conn.execute(text(ddl))
                            logger.info(f'claw_secrets 新增列 {col}')
                        except Exception:
                            pass  # 已存在则跳过

            except Exception as e:
                logger.warning(f'自动迁移检查异常: {e}')
                try:
                    db.session.remove()
                except Exception:
                    pass

    # 启动 5 分钟兜底守护进程（B+ 通信稳定化）
    # 内部用 system_config 表做主进程选举，gunicorn -w 4 安全
    try:
        from app.services.timeout_watcher import start_timeout_watcher
        start_timeout_watcher(app)
    except Exception as e:
        logger.warning(f'timeout_watcher 启动失败（不影响主服务）: {e}')

    return app
