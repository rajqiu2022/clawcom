-- Long-running Agent control-plane pilot (shadow mode).
-- Creates isolated tables only; legacy agent_task / Flow #12 paths are unchanged.

CREATE TABLE IF NOT EXISTS agent_goals (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  title VARCHAR(255) NOT NULL,
  objective TEXT NOT NULL,
  scope_json JSON NULL,
  non_goals_json JSON NULL,
  authority_sources_json JSON NULL,
  current_belief TEXT NULL,
  next_action TEXT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
  priority VARCHAR(16) NOT NULL DEFAULT 'P1',
  compute_quota INT NOT NULL DEFAULT 0,
  control_mode VARCHAR(32) NOT NULL DEFAULT 'shadow',
  version INT NOT NULL DEFAULT 1,
  created_by_type VARCHAR(24) NOT NULL DEFAULT 'user',
  created_by_id INT NOT NULL,
  created_by_name VARCHAR(160) NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  PRIMARY KEY (id),
  KEY ix_agent_goal_project_id (project_id),
  KEY ix_agent_goal_status (status),
  KEY ix_agent_goal_priority (priority),
  KEY ix_agent_goal_control_mode (control_mode),
  KEY ix_agent_goal_created_at (created_at),
  KEY ix_agent_goal_project_status (project_id, status),
  CONSTRAINT fk_agent_goal_project
    FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS goal_todos (
  id INT NOT NULL AUTO_INCREMENT,
  goal_id INT NOT NULL,
  title VARCHAR(255) NOT NULL,
  description TEXT NULL,
  task_class VARCHAR(32) NOT NULL DEFAULT 'advancement_task',
  action_kind VARCHAR(64) NOT NULL DEFAULT 'analyze',
  priority VARCHAR(16) NOT NULL DEFAULT 'P1',
  status VARCHAR(24) NOT NULL DEFAULT 'open',
  assigned_agent_id INT NULL,
  claimed_by_worker_id VARCHAR(160) NULL,
  claimed_claw_id INT NULL,
  claimed_at DATETIME NULL,
  claim_expires_at DATETIME NULL,
  claim_lease_seconds INT NOT NULL DEFAULT 180,
  fencing_token INT NOT NULL DEFAULT 0,
  authorization_envelope_json JSON NULL,
  required_capabilities_json JSON NULL,
  required_write_scopes_json JSON NULL,
  resume_when_json JSON NULL,
  completion_criteria_json JSON NULL,
  verification_policy_json JSON NULL,
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  PRIMARY KEY (id),
  KEY ix_goal_todo_goal_id (goal_id),
  KEY ix_goal_todo_task_class (task_class),
  KEY ix_goal_todo_priority (priority),
  KEY ix_goal_todo_status (status),
  KEY ix_goal_todo_assigned_agent_id (assigned_agent_id),
  KEY ix_goal_todo_claimed_by_worker_id (claimed_by_worker_id),
  KEY ix_goal_todo_claimed_claw_id (claimed_claw_id),
  KEY ix_goal_todo_claim_expires_at (claim_expires_at),
  KEY ix_goal_todo_created_at (created_at),
  KEY ix_goal_todo_frontier (goal_id, status, priority, id),
  KEY ix_goal_todo_claim_expiry (status, claim_expires_at),
  CONSTRAINT fk_goal_todo_goal
    FOREIGN KEY (goal_id) REFERENCES agent_goals (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_control_gates (
  id INT NOT NULL AUTO_INCREMENT,
  goal_id INT NOT NULL,
  work_item_type VARCHAR(32) NOT NULL DEFAULT 'goal_todo',
  work_item_id INT NULL,
  turn_id VARCHAR(80) NULL,
  gate_type VARCHAR(40) NOT NULL,
  policy_level VARCHAR(16) NOT NULL DEFAULT 'hard',
  status VARCHAR(20) NOT NULL DEFAULT 'open',
  blocking_scope VARCHAR(20) NOT NULL DEFAULT 'work_item',
  question TEXT NOT NULL,
  reason_code VARCHAR(80) NOT NULL,
  requested_scope_json JSON NULL,
  safe_fallback_json JSON NULL,
  decision_options_json JSON NULL,
  requested_by VARCHAR(160) NOT NULL,
  resolved_by VARCHAR(160) NULL,
  resolution VARCHAR(40) NULL,
  resolution_note TEXT NULL,
  expires_at DATETIME NULL,
  version INT NOT NULL DEFAULT 1,
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  resolution_idempotency_key VARCHAR(128) NULL,
  resolution_request_hash VARCHAR(64) NULL,
  created_at DATETIME NULL,
  resolved_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_agent_control_gate_goal_idempotency
    (goal_id, idempotency_key),
  KEY ix_agent_control_gate_goal_id (goal_id),
  KEY ix_agent_control_gate_work_item_id (work_item_id),
  KEY ix_agent_control_gate_turn_id (turn_id),
  KEY ix_agent_control_gate_gate_type (gate_type),
  KEY ix_agent_control_gate_status (status),
  KEY ix_agent_control_gate_expires_at (expires_at),
  KEY ix_agent_control_gate_created_at (created_at),
  KEY ix_agent_control_gate_active
    (goal_id, status, policy_level, work_item_id),
  CONSTRAINT fk_agent_control_gate_goal
    FOREIGN KEY (goal_id) REFERENCES agent_goals (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_turns (
  id INT NOT NULL AUTO_INCREMENT,
  turn_id VARCHAR(80) NOT NULL,
  goal_id INT NOT NULL,
  todo_id INT NULL,
  active_todo_slot VARCHAR(80) NULL,
  todo_version INT NULL,
  claw_id INT NULL,
  agent_id INT NULL,
  worker_id VARCHAR(160) NOT NULL,
  execution_mode VARCHAR(32) NOT NULL DEFAULT 'legacy_unmanaged',
  route VARCHAR(40) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'dispatched',
  result_kind VARCHAR(40) NULL,
  fencing_token INT NOT NULL DEFAULT 0,
  claim_expires_at DATETIME NULL,
  schedule_version INT NOT NULL DEFAULT 1,
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  envelope_json JSON NOT NULL,
  effect_manifest_json JSON NULL,
  authorization_payload_json JSON NULL,
  authorization_signature TEXT NULL,
  authorization_key_id VARCHAR(80) NULL,
  authorization_idempotency_key VARCHAR(128) NULL,
  authorization_request_hash VARCHAR(64) NULL,
  quota_reserved TINYINT(1) NOT NULL DEFAULT 0,
  deadline_at DATETIME NULL,
  created_at DATETIME NULL,
  completed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_agent_turn_turn_id (turn_id),
  UNIQUE KEY uq_agent_turn_active_todo_slot (active_todo_slot),
  UNIQUE KEY uq_agent_turn_dispatch_idempotency
    (goal_id, worker_id, idempotency_key),
  KEY ix_agent_turn_goal_id (goal_id),
  KEY ix_agent_turn_todo_id (todo_id),
  KEY ix_agent_turn_claw_id (claw_id),
  KEY ix_agent_turn_agent_id (agent_id),
  KEY ix_agent_turn_worker_id (worker_id),
  KEY ix_agent_turn_status (status),
  KEY ix_agent_turn_created_at (created_at),
  CONSTRAINT fk_agent_turn_goal
    FOREIGN KEY (goal_id) REFERENCES agent_goals (id) ON DELETE CASCADE,
  CONSTRAINT fk_agent_turn_todo
    FOREIGN KEY (todo_id) REFERENCES goal_todos (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_transition_receipts (
  id INT NOT NULL AUTO_INCREMENT,
  transition_id VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  turn_id VARCHAR(80) NOT NULL,
  goal_id INT NOT NULL,
  todo_id INT NULL,
  todo_version INT NULL,
  worker_id VARCHAR(160) NOT NULL,
  fencing_token INT NOT NULL DEFAULT 0,
  from_state VARCHAR(24) NOT NULL,
  to_state VARCHAR(24) NOT NULL,
  result_kind VARCHAR(40) NOT NULL,
  summary TEXT NULL,
  verification_json JSON NOT NULL,
  evidence_json JSON NULL,
  effect_receipts_json JSON NULL,
  created_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_agent_transition_receipt_transition_id (transition_id),
  KEY ix_agent_transition_receipt_turn_id (turn_id),
  KEY ix_agent_transition_receipt_goal_id (goal_id),
  KEY ix_agent_transition_receipt_todo_id (todo_id),
  KEY ix_agent_transition_receipt_result_kind (result_kind),
  KEY ix_agent_transition_receipt_created_at (created_at),
  CONSTRAINT fk_agent_transition_receipt_turn
    FOREIGN KEY (turn_id) REFERENCES agent_turns (turn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Optional adapters for the existing Workflow path. Both fields default to
-- legacy-compatible values, so Flow #12 and old Workers continue unchanged.
ALTER TABLE workflow_runs
  ADD COLUMN IF NOT EXISTS goal_id INT NULL AFTER definition_revision_id;

ALTER TABLE workflow_runs
  ADD INDEX IF NOT EXISTS ix_workflow_runs_goal_id (goal_id);

ALTER TABLE workflow_run_steps
  ADD COLUMN IF NOT EXISTS claim_fencing_token INT NOT NULL DEFAULT 0
    AFTER claim_lease_seconds;

ALTER TABLE agent_turns
  ADD COLUMN IF NOT EXISTS effect_manifest_json JSON NULL AFTER envelope_json;
