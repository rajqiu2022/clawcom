from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / 'web' / 'app' / '__init__.py'
MIGRATION = (
    ROOT / 'ops' / 'migrations'
    / '20260913_agent_task_status_blocked.sql'
)


def test_agent_task_status_startup_migration_widens_legacy_mysql_enum():
    source = INIT.read_text(encoding='utf-8')

    assert "TABLE_NAME = 'agent_tasks'" in source
    assert "COLUMN_NAME = 'status'" in source
    assert "MODIFY COLUMN status VARCHAR(20) NULL DEFAULT 'pending'" in source
    assert "terminal_reason = 'blocked'" in source


def test_agent_task_status_explicit_migration_is_idempotent_and_targeted():
    source = MIGRATION.read_text(encoding='utf-8')

    assert 'information_schema.COLUMNS' in source
    assert "DATA_TYPE = 'varchar'" in source
    assert "MODIFY COLUMN status VARCHAR(20) NULL DEFAULT 'pending'" in source
    assert "terminal_reason = 'blocked'" in source
    assert "status IS NULL OR status = ''" in source
