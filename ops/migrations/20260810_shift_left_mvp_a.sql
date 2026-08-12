-- Hub 测试左移 MVP-A：纯增量数据面。
-- 不修改现有 Workflow、TestReport、用例评审或测试计划表。

CREATE TABLE IF NOT EXISTS shift_left_analysis_runs (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  iteration_id INT NULL,
  workflow_run_id INT NULL,
  report_id INT NULL,
  baseline_fingerprint VARCHAR(64) NOT NULL,
  rerun_sequence INT NOT NULL DEFAULT 0,
  rerun_of_id INT NULL,
  rerun_reason VARCHAR(500) DEFAULT '',
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  baseline_json JSON NOT NULL,
  context_snapshot_json JSON NULL,
  result_summary_json JSON NULL,
  output_schema_version VARCHAR(32) DEFAULT 'v1',
  created_by VARCHAR(120) DEFAULT 'system',
  updated_by VARCHAR(120) DEFAULT 'system',
  revision INT NOT NULL DEFAULT 1,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  finished_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_shift_left_analysis_baseline_rerun
    (project_id, baseline_fingerprint, rerun_sequence),
  KEY ix_shift_left_analysis_runs_project_id (project_id),
  KEY ix_shift_left_analysis_runs_iteration_id (iteration_id),
  KEY ix_shift_left_analysis_runs_workflow_run_id (workflow_run_id),
  KEY ix_shift_left_analysis_runs_report_id (report_id),
  KEY ix_shift_left_analysis_runs_fingerprint (baseline_fingerprint),
  KEY ix_shift_left_analysis_project_status (project_id, status),
  CONSTRAINT fk_shift_left_analysis_project
    FOREIGN KEY (project_id) REFERENCES projects (id),
  CONSTRAINT fk_shift_left_analysis_iteration
    FOREIGN KEY (iteration_id) REFERENCES test_iterations (id),
  CONSTRAINT fk_shift_left_analysis_workflow
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id),
  CONSTRAINT fk_shift_left_analysis_report
    FOREIGN KEY (report_id) REFERENCES test_reports (id),
  CONSTRAINT fk_shift_left_analysis_rerun
    FOREIGN KEY (rerun_of_id) REFERENCES shift_left_analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shift_left_findings (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  analysis_run_id INT NOT NULL,
  finding_key VARCHAR(255) NOT NULL,
  title VARCHAR(300) NOT NULL,
  module VARCHAR(160) DEFAULT '',
  severity VARCHAR(20) NOT NULL DEFAULT 'medium',
  confidence DOUBLE NOT NULL DEFAULT 0,
  evidence_level VARCHAR(32) NOT NULL DEFAULT 'hypothesis',
  source_type VARCHAR(40) NOT NULL DEFAULT 'agent',
  status VARCHAR(32) NOT NULL DEFAULT 'new',
  owner VARCHAR(120) DEFAULT '',
  description LONGTEXT NULL,
  business_impact LONGTEXT NULL,
  context_quality VARCHAR(20) DEFAULT 'fresh',
  code_locations_json JSON NULL,
  associations_json JSON NULL,
  fix_ref VARCHAR(500) DEFAULT '',
  verification_ref VARCHAR(500) DEFAULT '',
  fix_actor_key VARCHAR(80) DEFAULT '',
  verification_actor_key VARCHAR(80) DEFAULT '',
  status_reason LONGTEXT NULL,
  first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(120) DEFAULT 'system',
  created_by_actor_key VARCHAR(80) DEFAULT 'system:0',
  updated_by VARCHAR(120) DEFAULT 'system',
  revision INT NOT NULL DEFAULT 1,
  is_archived TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_shift_left_finding_project_key (project_id, finding_key),
  KEY ix_shift_left_findings_project_id (project_id),
  KEY ix_shift_left_findings_analysis_run_id (analysis_run_id),
  KEY ix_shift_left_findings_module (module),
  KEY ix_shift_left_findings_severity (severity),
  KEY ix_shift_left_findings_evidence_level (evidence_level),
  KEY ix_shift_left_findings_status (status),
  KEY ix_shift_left_findings_last_seen_at (last_seen_at),
  KEY ix_shift_left_findings_project_state (project_id, status, severity),
  CONSTRAINT fk_shift_left_finding_project
    FOREIGN KEY (project_id) REFERENCES projects (id),
  CONSTRAINT fk_shift_left_finding_analysis
    FOREIGN KEY (analysis_run_id) REFERENCES shift_left_analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shift_left_analysis_findings (
  id INT NOT NULL AUTO_INCREMENT,
  analysis_run_id INT NOT NULL,
  finding_id INT NOT NULL,
  snapshot_json JSON NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_shift_left_analysis_finding (analysis_run_id, finding_id),
  KEY ix_shift_left_analysis_findings_run (analysis_run_id),
  KEY ix_shift_left_analysis_findings_finding (finding_id),
  CONSTRAINT fk_shift_left_occurrence_analysis
    FOREIGN KEY (analysis_run_id) REFERENCES shift_left_analysis_runs (id)
    ON DELETE CASCADE,
  CONSTRAINT fk_shift_left_occurrence_finding
    FOREIGN KEY (finding_id) REFERENCES shift_left_findings (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shift_left_finding_events (
  id INT NOT NULL AUTO_INCREMENT,
  finding_id INT NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  payload_json JSON NOT NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_key VARCHAR(80) NOT NULL,
  actor_name VARCHAR(120) DEFAULT '',
  idempotency_key VARCHAR(128) NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_shift_left_finding_event_idempotency
    (finding_id, actor_key, idempotency_key),
  KEY ix_shift_left_finding_events_finding (finding_id),
  KEY ix_shift_left_finding_events_type (event_type),
  KEY ix_shift_left_finding_events_created_at (created_at),
  KEY ix_shift_left_finding_event_type (finding_id, event_type),
  CONSTRAINT fk_shift_left_event_finding
    FOREIGN KEY (finding_id) REFERENCES shift_left_findings (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS collaboration_sessions (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  subject_type VARCHAR(32) NOT NULL,
  subject_id INT NOT NULL,
  agent_identity VARCHAR(160) NOT NULL,
  scopes_json JSON NOT NULL,
  invitation_hash VARCHAR(64) NOT NULL,
  access_token_hash VARCHAR(64) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  invitation_expires_at DATETIME NOT NULL,
  token_expires_at DATETIME NULL,
  token_ttl_seconds INT NOT NULL DEFAULT 7200,
  max_calls INT NOT NULL DEFAULT 500,
  call_count INT NOT NULL DEFAULT 0,
  created_by_type VARCHAR(24) NOT NULL,
  created_by_id INT NOT NULL,
  created_by_name VARCHAR(120) DEFAULT '',
  creator_actor_key VARCHAR(80) NOT NULL,
  create_idempotency_key VARCHAR(128) NOT NULL,
  create_request_hash VARCHAR(64) NOT NULL,
  exchanged_at DATETIME NULL,
  last_activity_at DATETIME NULL,
  revoked_at DATETIME NULL,
  revoked_by VARCHAR(120) DEFAULT '',
  revoke_reason VARCHAR(500) DEFAULT '',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_collaboration_invitation_hash (invitation_hash),
  UNIQUE KEY uq_collaboration_access_token_hash (access_token_hash),
  UNIQUE KEY uq_collaboration_creator_idempotency
    (creator_actor_key, create_idempotency_key),
  KEY ix_collaboration_sessions_project (project_id),
  KEY ix_collaboration_sessions_subject_type (subject_type),
  KEY ix_collaboration_sessions_subject_id (subject_id),
  KEY ix_collaboration_sessions_status (status),
  KEY ix_collaboration_sessions_invite_expiry (invitation_expires_at),
  KEY ix_collaboration_sessions_token_expiry (token_expires_at),
  KEY ix_collaboration_subject (subject_type, subject_id),
  KEY ix_collaboration_project_status (project_id, status),
  CONSTRAINT fk_collaboration_session_project
    FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS collaboration_session_events (
  id INT NOT NULL AUTO_INCREMENT,
  session_id INT NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_name VARCHAR(160) DEFAULT '',
  detail_json JSON NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_collaboration_session_events_session (session_id),
  KEY ix_collaboration_session_events_type (event_type),
  KEY ix_collaboration_session_events_created (created_at),
  CONSTRAINT fk_collaboration_event_session
    FOREIGN KEY (session_id) REFERENCES collaboration_sessions (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
