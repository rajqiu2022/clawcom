-- Project-scoped, versioned release-test journal Wiki.
-- Additive: existing knowledge entries remain entry_type=article.

CREATE TABLE IF NOT EXISTS knowledge_notebooks (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  title VARCHAR(255) NOT NULL,
  version_name VARCHAR(120) DEFAULT '',
  iteration_id INT NULL,
  modules_json LONGTEXT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  version INT NOT NULL DEFAULT 1,
  created_by_type VARCHAR(20) NOT NULL DEFAULT 'user',
  created_by_id INT NULL,
  created_by_name VARCHAR(100) DEFAULT '',
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  PRIMARY KEY (id),
  KEY ix_knowledge_notebook_project_status (project_id, status),
  KEY ix_knowledge_notebook_iteration (iteration_id),
  CONSTRAINT fk_knowledge_notebook_project
    FOREIGN KEY (project_id) REFERENCES projects(id),
  CONSTRAINT fk_knowledge_notebook_iteration
    FOREIGN KEY (iteration_id) REFERENCES test_iterations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='project_id') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD COLUMN project_id INT NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='entry_type') > 0, 'SELECT 1',
  "ALTER TABLE knowledge_entries ADD COLUMN entry_type VARCHAR(30) NOT NULL DEFAULT 'article'");
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='notebook_id') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD COLUMN notebook_id INT NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='current_revision') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD COLUMN current_revision INT NOT NULL DEFAULT 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='lock_version') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD COLUMN lock_version INT NOT NULL DEFAULT 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='archived_at') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD COLUMN archived_at DATETIME NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

UPDATE knowledge_entries SET entry_type='article'
WHERE entry_type IS NULL OR entry_type='';

CREATE TABLE IF NOT EXISTS knowledge_entry_revisions (
  id INT NOT NULL AUTO_INCREMENT,
  knowledge_id INT NOT NULL,
  revision_no INT NOT NULL,
  title VARCHAR(255) NOT NULL,
  content_markdown LONGTEXT NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  change_summary VARCHAR(500) DEFAULT '',
  based_on_revision INT NULL,
  rollback_from_revision INT NULL,
  editor_type VARCHAR(20) NOT NULL,
  editor_id INT NULL,
  editor_name VARCHAR(100) DEFAULT '',
  idempotency_key VARCHAR(128) NOT NULL,
  request_sha256 VARCHAR(64) NOT NULL,
  created_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_knowledge_revision_no (knowledge_id, revision_no),
  UNIQUE KEY uq_knowledge_revision_idem
    (editor_type, editor_id, idempotency_key),
  KEY ix_knowledge_revision_created (knowledge_id, created_at),
  CONSTRAINT fk_knowledge_revision_entry
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_entries(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND INDEX_NAME='ix_knowledge_entries_project_id') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD INDEX ix_knowledge_entries_project_id (project_id)');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND INDEX_NAME='ix_knowledge_entries_entry_type') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD INDEX ix_knowledge_entries_entry_type (entry_type)');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF((SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_entries'
    AND INDEX_NAME='ix_knowledge_entries_notebook_id') > 0, 'SELECT 1',
  'ALTER TABLE knowledge_entries ADD INDEX ix_knowledge_entries_notebook_id (notebook_id)');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
