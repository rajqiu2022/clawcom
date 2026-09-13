-- Widen the legacy AgentTask status enum before Workers report `blocked`.
-- MariaDB 5.5 does not support ALTER ... IF EXISTS for column types, so the
-- information_schema guard keeps this migration safe to re-run.

SET @agent_task_status_is_varchar := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'agent_tasks'
    AND COLUMN_NAME = 'status'
    AND DATA_TYPE = 'varchar'
);
SET @widen_agent_task_status_sql := IF(
  @agent_task_status_is_varchar > 0,
  'SELECT 1',
  "ALTER TABLE agent_tasks MODIFY COLUMN status VARCHAR(20) NULL DEFAULT 'pending'"
);
PREPARE widen_agent_task_status_stmt FROM @widen_agent_task_status_sql;
EXECUTE widen_agent_task_status_stmt;
DEALLOCATE PREPARE widen_agent_task_status_stmt;

-- Legacy MySQL enum coercion stored unsupported `blocked` as an empty value.
-- Only recover rows whose terminal reason proves the intended state.
UPDATE agent_tasks
SET status = 'blocked'
WHERE (status IS NULL OR status = '')
  AND terminal_reason = 'blocked';
