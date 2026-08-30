-- Workflow worker strong claim lease fields.
-- Idempotent for MariaDB 10.3+ / MySQL 8 style deployments used by Hub.

ALTER TABLE workflow_run_steps
  ADD COLUMN IF NOT EXISTS claimed_claw_id INT NULL AFTER claimed_at,
  ADD COLUMN IF NOT EXISTS claim_expires_at DATETIME NULL AFTER claimed_claw_id,
  ADD COLUMN IF NOT EXISTS claim_lease_seconds INT NULL DEFAULT 180 AFTER claim_expires_at;

ALTER TABLE workflow_run_steps
  ADD INDEX IF NOT EXISTS idx_workflow_step_claimed_claw_id (claimed_claw_id),
  ADD INDEX IF NOT EXISTS idx_workflow_step_claim_expires_at (claim_expires_at);

-- Legacy claimed_by values cannot be safely mapped to a Claw id when they are
-- arbitrary worker ids. Expire them so workers must re-claim under the new
-- Bearer Claw + worker_id contract instead of inheriting ambiguous ownership.
UPDATE workflow_run_steps
SET claim_expires_at = COALESCE(claim_expires_at, claimed_at),
    claim_lease_seconds = COALESCE(claim_lease_seconds, 180)
WHERE claimed_by IS NOT NULL
  AND claimed_by <> ''
  AND claimed_claw_id IS NULL;
