-- Test task ↔ testcase library synchronization with one-step restore.
-- Additive only; existing task cases continue to work with NULL snapshots.

SET @has_task_backup := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'test_tasks'
    AND COLUMN_NAME = 'case_sync_backup_json'
);
SET @sql := IF(@has_task_backup > 0, 'SELECT 1',
  'ALTER TABLE test_tasks ADD COLUMN case_sync_backup_json LONGTEXT NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_task_backup_created := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'test_tasks'
    AND COLUMN_NAME = 'case_sync_backup_created_at'
);
SET @sql := IF(@has_task_backup_created > 0, 'SELECT 1',
  'ALTER TABLE test_tasks ADD COLUMN case_sync_backup_created_at DATETIME NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_task_backup_restored := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'test_tasks'
    AND COLUMN_NAME = 'case_sync_backup_restored_at'
);
SET @sql := IF(@has_task_backup_restored > 0, 'SELECT 1',
  'ALTER TABLE test_tasks ADD COLUMN case_sync_backup_restored_at DATETIME NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_case_source_snapshot := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'test_task_cases'
    AND COLUMN_NAME = 'case_source_snapshot'
);
SET @sql := IF(@has_case_source_snapshot > 0, 'SELECT 1',
  'ALTER TABLE test_task_cases ADD COLUMN case_source_snapshot LONGTEXT NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
