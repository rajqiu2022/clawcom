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
                                UNIQUE KEY uq_artf_tpl_path (template_id, relative_path),
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
