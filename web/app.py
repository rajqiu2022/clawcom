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
                conn.execute(text(
                    "INSERT OR IGNORE INTO system_config (config_key, config_value) VALUES (:k, :v)"
                ), {'k': k, 'v': v})
            conn.commit()
            click.echo('  [ok] 默认 LLM 配置已插入')

        conn.close()
        click.echo('迁移完成')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
