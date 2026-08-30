-- P0-B automation candidate lifecycle and minimum capability-gap linkage.
-- This migration does not change the production testcase CRUD path.

CREATE TABLE IF NOT EXISTS capability_gaps (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  gap_key VARCHAR(255) NOT NULL,
  title VARCHAR(300) NOT NULL,
  missing_capabilities_json JSON NULL,
  evidence_json JSON NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'open',
  candidate_requeue_pending TINYINT(1) NOT NULL DEFAULT 0,
  version INT NOT NULL DEFAULT 1,
  created_by VARCHAR(160) DEFAULT 'system',
  updated_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_capability_gap_project_key (project_id, gap_key),
  KEY ix_capability_gaps_project_id (project_id),
  KEY ix_capability_gaps_status (status),
  KEY ix_capability_gaps_requeue (candidate_requeue_pending),
  KEY ix_capability_gap_project_status (project_id, status),
  CONSTRAINT fk_capability_gap_project
    FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS automation_case_candidates (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  title VARCHAR(300) NOT NULL,
  module_key VARCHAR(200) DEFAULT '',
  source_type VARCHAR(64) DEFAULT 'manual',
  source_refs_json JSON NULL,
  case_draft_json JSON NULL,
  required_capabilities_json JSON NULL,
  state VARCHAR(40) NOT NULL DEFAULT 'DISCOVERED',
  qualification_outcome VARCHAR(48) NULL,
  qualification_run_id INT NULL,
  qualification_evidence_json JSON NULL,
  production_library_id INT NULL,
  production_case_id INT NULL,
  capability_gap_id INT NULL,
  dedupe_key VARCHAR(255) NOT NULL,
  version INT NOT NULL DEFAULT 1,
  created_by VARCHAR(160) DEFAULT 'system',
  updated_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_automation_candidate_project_dedupe (project_id, dedupe_key),
  KEY ix_automation_candidates_project_id (project_id),
  KEY ix_automation_candidates_module_key (module_key),
  KEY ix_automation_candidates_source_type (source_type),
  KEY ix_automation_candidates_state (state),
  KEY ix_automation_candidates_qualification_outcome (qualification_outcome),
  KEY ix_automation_candidates_qualification_run (qualification_run_id),
  KEY ix_automation_candidates_library (production_library_id),
  KEY ix_automation_candidates_case (production_case_id),
  KEY ix_automation_candidates_capability_gap (capability_gap_id),
  KEY ix_automation_candidates_created (created_at),
  KEY ix_automation_candidates_updated (updated_at),
  KEY ix_automation_candidate_project_state_updated
    (project_id, state, updated_at),
  CONSTRAINT fk_automation_candidate_project
    FOREIGN KEY (project_id) REFERENCES projects (id),
  CONSTRAINT fk_automation_candidate_qualification_run
    FOREIGN KEY (qualification_run_id) REFERENCES workflow_runs (id),
  CONSTRAINT fk_automation_candidate_library
    FOREIGN KEY (production_library_id) REFERENCES test_case_libraries (id),
  CONSTRAINT fk_automation_candidate_case
    FOREIGN KEY (production_case_id) REFERENCES test_cases (id),
  CONSTRAINT fk_automation_candidate_capability_gap
    FOREIGN KEY (capability_gap_id) REFERENCES capability_gaps (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS automation_case_candidate_events (
  id INT NOT NULL AUTO_INCREMENT,
  candidate_id INT NOT NULL,
  event_type VARCHAR(48) NOT NULL,
  from_state VARCHAR(40) NULL,
  to_state VARCHAR(40) NULL,
  version_before INT NULL,
  version_after INT NULL,
  payload_json JSON NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_name VARCHAR(160) DEFAULT '',
  request_id VARCHAR(128) NULL,
  idempotency_key VARCHAR(128) NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_candidate_event_actor_idempotency
    (candidate_id, event_type, actor_type, actor_id, idempotency_key),
  KEY ix_candidate_events_candidate (candidate_id),
  KEY ix_candidate_events_type (event_type),
  KEY ix_candidate_events_request (request_id),
  KEY ix_candidate_events_created (created_at),
  CONSTRAINT fk_candidate_event_candidate
    FOREIGN KEY (candidate_id) REFERENCES automation_case_candidates (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
