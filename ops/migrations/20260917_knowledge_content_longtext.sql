-- Expand knowledge article / journal snapshot bodies beyond MySQL TEXT's
-- 65,535-byte limit. Idempotent: environments already on LONGTEXT are no-op.

SET @sql := IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE()
    AND TABLE_NAME='knowledge_entries'
    AND COLUMN_NAME='content'
    AND DATA_TYPE='longtext') > 0,
  'SELECT 1',
  'ALTER TABLE knowledge_entries MODIFY COLUMN content LONGTEXT NOT NULL');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
