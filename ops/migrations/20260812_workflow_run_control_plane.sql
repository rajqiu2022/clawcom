-- Codex multi-Flow control-plane metadata for Workflow Runs.
-- All columns are nullable so legacy Run creation and Flow #12 remain unchanged.

ALTER TABLE workflow_runs
  ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128) NULL AFTER triggered_by,
  ADD COLUMN IF NOT EXISTS idempotency_request_hash VARCHAR(64) NULL AFTER idempotency_key,
  ADD COLUMN IF NOT EXISTS controller_run_id VARCHAR(160) NULL AFTER idempotency_request_hash,
  ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(160) NULL AFTER controller_run_id,
  ADD COLUMN IF NOT EXISTS trigger_source VARCHAR(64) NULL AFTER correlation_id;

ALTER TABLE workflow_runs
  ADD UNIQUE INDEX IF NOT EXISTS uq_workflow_run_definition_idempotency
    (definition_id, idempotency_key),
  ADD INDEX IF NOT EXISTS ix_workflow_runs_controller_run_id
    (controller_run_id),
  ADD INDEX IF NOT EXISTS ix_workflow_runs_correlation_id
    (correlation_id),
  ADD INDEX IF NOT EXISTS ix_workflow_runs_trigger_source
    (trigger_source),
  ADD INDEX IF NOT EXISTS ix_workflow_runs_controller_created
    (controller_run_id, created_at),
  ADD INDEX IF NOT EXISTS ix_workflow_runs_correlation_created
    (correlation_id, created_at);
