"""数据库迁移脚本：添加 role 列和 system_config 表（SQLite 兼容）"""
import sys
sys.path.insert(0, 'f:/Code/claw_team/web')

from app import create_app, db
from sqlalchemy import text, inspect

app = create_app()
with app.app_context():
    conn = db.engine.connect()

    # 检查 openclaw_instances 表有哪些列
    inspector = inspect(db.engine)
    claw_columns = [c['name'] for c in inspector.get_columns('openclaw_instances')]

    # 1. 添加 role 列
    if 'role' not in claw_columns:
        conn.execute(text(
            "ALTER TABLE openclaw_instances ADD COLUMN role TEXT DEFAULT 'test_member'"
        ))
        conn.commit()
        print('[ok] openclaw_instances.role 已添加（TEXT 类型）')
    else:
        print('[skip] openclaw_instances.role 已存在')

    # 2. 创建 system_config 表
    existing_tables = inspector.get_table_names()
    if 'system_config' not in existing_tables:
        conn.execute(text('''
            CREATE TABLE system_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_key VARCHAR(100) UNIQUE NOT NULL,
                config_value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        conn.commit()
        print('[ok] system_config 表已创建')

        # 插入默认 LLM 配置
        defaults = [
            ('llm_provider', 'doubao'),
            ('llm_model', 'doubao-pro-32k'),
            ('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3'),
            ('llm_api_key', ''),
        ]
        for k, v in defaults:
            conn.execute(
                text("INSERT OR IGNORE INTO system_config (config_key, config_value) VALUES (:k, :v)"),
                {"k": k, "v": v}
            )
        conn.commit()
        print('[ok] 默认 LLM 配置已插入')
    else:
        print('[skip] system_config 表已存在')

    conn.close()
    print('迁移完成')
