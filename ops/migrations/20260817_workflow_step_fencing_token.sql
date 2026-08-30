-- Flow worker lease fencing for MariaDB 5.5 production.
-- Deployment must check INFORMATION_SCHEMA first because MariaDB 5.5 does
-- not support ADD COLUMN IF NOT EXISTS.
ALTER TABLE workflow_run_steps
  ADD COLUMN claim_fencing_token INT NOT NULL DEFAULT 0
  AFTER claim_lease_seconds;
