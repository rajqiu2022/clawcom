from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'web' / 'app' / 'models.py'
MIGRATION = (
    ROOT / 'ops' / 'migrations'
    / '20260917_knowledge_content_longtext.sql'
)


def test_knowledge_content_uses_longtext_on_mysql():
    source = MODELS.read_text(encoding='utf-8')

    assert 'from sqlalchemy.dialects.mysql import LONGTEXT' in source
    assert "db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False" in source


def test_knowledge_content_migration_is_idempotent_and_preserves_not_null():
    source = MIGRATION.read_text(encoding='utf-8')

    assert "TABLE_NAME='knowledge_entries'" in source
    assert "COLUMN_NAME='content'" in source
    assert "DATA_TYPE='longtext'" in source
    assert 'MODIFY COLUMN content LONGTEXT NOT NULL' in source
