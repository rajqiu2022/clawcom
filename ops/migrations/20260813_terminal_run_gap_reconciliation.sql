-- Reconcile historical derived state. These updates are idempotent and do
-- not touch any active Workflow Run or testcase content.

UPDATE workflow_runs AS run
LEFT JOIN (
  SELECT run_id, MAX(finished_at) AS step_finished_at
  FROM workflow_run_steps
  GROUP BY run_id
) AS terminal_step ON terminal_step.run_id = run.id
SET run.finished_at = COALESCE(
  terminal_step.step_finished_at,
  run.updated_at,
  run.started_at,
  run.created_at,
  NOW()
)
WHERE run.status IN ('blocked', 'failed', 'succeeded', 'cancelled')
  AND run.finished_at IS NULL;

UPDATE capability_gaps AS gap
LEFT JOIN (
  SELECT capability_gap_id, COUNT(*) AS waiting_count
  FROM automation_case_candidates
  WHERE state = 'WAITING_CAPABILITY'
    AND capability_gap_id IS NOT NULL
  GROUP BY capability_gap_id
) AS waiting ON waiting.capability_gap_id = gap.id
SET gap.candidate_requeue_pending = 0,
    gap.version = gap.version + 1,
    gap.updated_by = 'migration',
    gap.updated_at = NOW()
WHERE gap.status = 'resolved'
  AND gap.candidate_requeue_pending = 1
  AND COALESCE(waiting.waiting_count, 0) = 0;
