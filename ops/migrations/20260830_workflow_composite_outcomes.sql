-- Additive composite outcomes for Workflow Runs.
-- Use LONGTEXT for compatibility with the production MariaDB version;
-- SQLAlchemy db.JSON continues to serialize the Python object.

SET @has_outcomes_json := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'workflow_runs'
    AND COLUMN_NAME = 'outcomes_json'
);
SET @add_outcomes_json_sql := IF(
  @has_outcomes_json > 0,
  'SELECT 1',
  'ALTER TABLE workflow_runs ADD COLUMN outcomes_json LONGTEXT NULL'
);
PREPARE add_outcomes_json_stmt FROM @add_outcomes_json_sql;
EXECUTE add_outcomes_json_stmt;
DEALLOCATE PREPARE add_outcomes_json_stmt;
