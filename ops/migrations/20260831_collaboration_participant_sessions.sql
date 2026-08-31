-- Separate reusable collaboration invitations from per-token participant sessions.
-- Additive and backward compatible: existing rows remain legacy invite/token sessions.

SET @has_parent_invite_id := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'collaboration_sessions'
    AND COLUMN_NAME = 'parent_invite_id'
);
SET @add_parent_invite_id_sql := IF(
  @has_parent_invite_id > 0,
  'SELECT 1',
  'ALTER TABLE collaboration_sessions ADD COLUMN parent_invite_id INT NULL'
);
PREPARE add_parent_invite_id_stmt FROM @add_parent_invite_id_sql;
EXECUTE add_parent_invite_id_stmt;
DEALLOCATE PREPARE add_parent_invite_id_stmt;

SET @has_parent_invite_index := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'collaboration_sessions'
    AND INDEX_NAME = 'ix_collaboration_parent_invite'
);
SET @add_parent_invite_index_sql := IF(
  @has_parent_invite_index > 0,
  'SELECT 1',
  'CREATE INDEX ix_collaboration_parent_invite ON collaboration_sessions (parent_invite_id)'
);
PREPARE add_parent_invite_index_stmt FROM @add_parent_invite_index_sql;
EXECUTE add_parent_invite_index_stmt;
DEALLOCATE PREPARE add_parent_invite_index_stmt;
