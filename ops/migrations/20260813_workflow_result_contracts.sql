-- Hub-owned Workflow result contracts, conclusions, evidence ingestion and
-- notification audit. All changes are additive for legacy Flow compatibility.

ALTER TABLE workflow_runs
  ADD COLUMN IF NOT EXISTS business_conclusion VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS automation_conclusion VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS evidence_ingest_status VARCHAR(64) NULL;

ALTER TABLE workflow_run_steps
  ADD COLUMN IF NOT EXISTS contract_result_json JSON NULL;

ALTER TABLE wecom_send_logs
  ADD COLUMN IF NOT EXISTS workflow_run_id INT NULL,
  ADD COLUMN IF NOT EXISTS workflow_step_id VARCHAR(100) NULL,
  ADD COLUMN IF NOT EXISTS request_summary_json JSON NULL,
  ADD COLUMN IF NOT EXISTS template_version VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS message_hash VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS receipt_json JSON NULL,
  ADD COLUMN IF NOT EXISTS gate_result_json JSON NULL,
  ADD INDEX IF NOT EXISTS ix_wecom_send_logs_workflow_run (workflow_run_id),
  ADD INDEX IF NOT EXISTS ix_wecom_send_logs_message_hash (message_hash);
