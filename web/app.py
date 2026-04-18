# -*- coding: utf-8 -*-
import click
from app import create_app, db

app = create_app()


@app.cli.command('seed')
def seed_command():
    """初始化种子数据"""
    from app.seed import seed_all
    result = seed_all()
    click.echo(result)


@app.cli.command('init-db')
def init_db_command():
    """创建所有表"""
    db.create_all()
    click.echo('数据库表已创建')


@app.cli.command('migrate-db')
def migrate_db_command():
    """执行数据库结构增量迁移"""
    import sqlalchemy as sa
    from sqlalchemy import text

    with app.app_context():
        conn = db.engine.connect()

        # Detect database type
        db_url = str(db.engine.url)
        is_mysql = 'mysql' in db_url

        # --- openclaw_instances.role ---
        try:
            conn.execute(text("SELECT role FROM openclaw_instances LIMIT 1"))
            click.echo('  [skip] openclaw_instances.role 已存在')
        except Exception:
            conn.execute(text(
                "ALTER TABLE openclaw_instances ADD COLUMN role "
                "ENUM('admin','test_manager','test_member','test_executor') "
                "DEFAULT 'test_member' COMMENT '角色'"
            ))
            conn.commit()
            click.echo('  [ok] openclaw_instances.role 已添加')

        # --- system_config 表（如果不存在）---
        try:
            conn.execute(text("SELECT 1 FROM system_config LIMIT 1"))
        except Exception:
            if is_mysql:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS system_config (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        config_key VARCHAR(100) UNIQUE NOT NULL,
                        config_value TEXT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            else:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS system_config (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        config_key VARCHAR(100) UNIQUE NOT NULL,
                        config_value TEXT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            conn.commit()
            click.echo('  [ok] system_config 表已创建')

            # 插入默认 LLM 配置
            defaults = [
                ('llm_provider', 'doubao'),
                ('llm_model', 'doubao-pro-32k'),
                ('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3'),
                ('llm_api_key', ''),
            ]
            for k, v in defaults:
                if is_mysql:
                    conn.execute(text(
                        "INSERT IGNORE INTO system_config (config_key, config_value) VALUES (:k, :v)"
                    ), {'k': k, 'v': v})
                else:
                    conn.execute(text(
                        "INSERT OR IGNORE INTO system_config (config_key, config_value) VALUES (:k, :v)"
                    ), {'k': k, 'v': v})
            conn.commit()
            click.echo('  [ok] 默认 LLM 配置已插入')

        # --- rules.category (旧 ENUM 迁移已废弃，改用 VARCHAR) ---
        # if is_mysql:
        #     ...old enum migration...

        # --- rules.scope add 'admin' enum value ---
        try:
            conn.execute(text("SELECT owner_claw_id FROM rules LIMIT 1"))
            click.echo('  [skip] rules.owner_claw_id 已存在')
        except Exception:
            if is_mysql:
                conn.execute(text(
                    "ALTER TABLE rules MODIFY COLUMN scope ENUM('global','project','module','admin') DEFAULT 'global'"
                ))
                conn.execute(text(
                    "ALTER TABLE rules ADD COLUMN owner_claw_id INT NULL COMMENT '所属 OpenClaw ID（admin scope 时指定归属）'"
                ))
            else:
                conn.execute(text(
                    "ALTER TABLE rules ADD COLUMN owner_claw_id INTEGER"
                ))
            conn.commit()
            click.echo('  [ok] rules.scope 已添加 admin + owner_claw_id 列')

        # --- rules.category ENUM → VARCHAR(30) 统一分类 ---
        if is_mysql:
            try:
                conn.execute(text(
                    "ALTER TABLE rules MODIFY COLUMN category VARCHAR(30) DEFAULT 'standard' "
                    "COMMENT '分类：standard/hub_system/project/business_test/special_test/evolved'"
                ))
                conn.commit()
                click.echo('  [ok] rules.category 已从 ENUM 迁移为 VARCHAR(30)')
            except Exception as e:
                click.echo(f'  [skip] rules.category 迁移跳过: {e}')

        # --- skills.review_status 添加 'revise' 枚举值 ---
        if is_mysql:
            try:
                conn.execute(text(
                    "ALTER TABLE skills MODIFY COLUMN review_status "
                    "ENUM('approved','pending','revise','rejected') DEFAULT 'approved' "
                    "COMMENT '评审状态：approved=已通过，pending=待评审，revise=待修改，rejected=已废弃'"
                ))
                conn.commit()
                click.echo('  [ok] skills.review_status 已添加 revise 枚举值')
            except Exception as e:
                click.echo(f'  [skip] skills.review_status 迁移跳过: {e}')

        # --- skills.review_comment 审核意见列 ---
        try:
            conn.execute(text("SELECT review_comment FROM skills LIMIT 1"))
            click.echo('  [skip] skills.review_comment 已存在')
        except Exception:
            conn.execute(text(
                "ALTER TABLE skills ADD COLUMN review_comment TEXT COMMENT '审核意见（打回/废弃时填写）'"
            ))
            conn.commit()
            click.echo('  [ok] skills.review_comment 已添加')

        # --- rules.review_status 添加 'revise' 枚举值 ---
        if is_mysql:
            try:
                conn.execute(text(
                    "ALTER TABLE rules MODIFY COLUMN review_status "
                    "ENUM('approved','pending','revise','rejected') DEFAULT 'approved' "
                    "COMMENT '评审状态：approved=已通过，pending=待评审，revise=待修改，rejected=已废弃'"
                ))
                conn.commit()
                click.echo('  [ok] rules.review_status 已添加 revise 枚举值')
            except Exception as e:
                click.echo(f'  [skip] rules.review_status 迁移跳过: {e}')

        # --- rules.review_comment 审核意见列 ---
        try:
            conn.execute(text("SELECT review_comment FROM rules LIMIT 1"))
            click.echo('  [skip] rules.review_comment 已存在')
        except Exception:
            conn.execute(text(
                "ALTER TABLE rules ADD COLUMN review_comment TEXT COMMENT '审核意见（打回/废弃时填写）'"
            ))
            conn.commit()
            click.echo('  [ok] rules.review_comment 已添加')

        conn.close()
        click.echo('迁移完成')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
