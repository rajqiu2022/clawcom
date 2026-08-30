-- P0-I: normalized Finding feedback and replay-gated immutable analysis rules.

CREATE TABLE IF NOT EXISTS shift_left_finding_feedback (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  finding_id INT NOT NULL,
  label VARCHAR(48) NOT NULL,
  note TEXT NULL,
  evidence_refs_json JSON NULL,
  source_type VARCHAR(40) DEFAULT 'human',
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_key VARCHAR(160) NOT NULL,
  actor_name VARCHAR(160) DEFAULT '',
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_finding_feedback_actor_idempotency
    (finding_id, actor_key, idempotency_key),
  KEY ix_finding_feedback_project (project_id),
  KEY ix_finding_feedback_finding (finding_id),
  KEY ix_finding_feedback_label (label),
  KEY ix_finding_feedback_created (created_at),
  KEY ix_finding_feedback_project_label (project_id, label),
  CONSTRAINT fk_finding_feedback_project
    FOREIGN KEY (project_id) REFERENCES projects (id),
  CONSTRAINT fk_finding_feedback_finding
    FOREIGN KEY (finding_id) REFERENCES shift_left_findings (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS analysis_rules (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  rule_key VARCHAR(255) NOT NULL,
  name VARCHAR(300) NOT NULL,
  description TEXT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'draft',
  active_version_id INT NULL,
  latest_version INT NOT NULL DEFAULT 0,
  version INT NOT NULL DEFAULT 1,
  created_by VARCHAR(160) DEFAULT 'system',
  updated_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_analysis_rule_project_key (project_id, rule_key),
  KEY ix_analysis_rules_project (project_id),
  KEY ix_analysis_rules_status (status),
  KEY ix_analysis_rules_active_version (active_version_id),
  KEY ix_analysis_rules_updated (updated_at),
  KEY ix_analysis_rule_project_status (project_id, status),
  CONSTRAINT fk_analysis_rule_project
    FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS analysis_rule_versions (
  id INT NOT NULL AUTO_INCREMENT,
  rule_id INT NOT NULL,
  version_no INT NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'draft',
  definition_json JSON NOT NULL,
  thresholds_json JSON NOT NULL,
  change_summary TEXT NULL,
  based_on_version_id INT NULL,
  source_finding_ids_json JSON NULL,
  definition_hash VARCHAR(64) NOT NULL,
  activated_by VARCHAR(160) DEFAULT '',
  activated_at DATETIME NULL,
  retired_by VARCHAR(160) DEFAULT '',
  retired_at DATETIME NULL,
  created_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_analysis_rule_version_no (rule_id, version_no),
  UNIQUE KEY uq_analysis_rule_definition_hash (rule_id, definition_hash),
  KEY ix_analysis_rule_versions_rule (rule_id),
  KEY ix_analysis_rule_versions_status (status),
  KEY ix_analysis_rule_versions_based_on (based_on_version_id),
  KEY ix_analysis_rule_versions_hash (definition_hash),
  CONSTRAINT fk_analysis_rule_version_rule
    FOREIGN KEY (rule_id) REFERENCES analysis_rules (id)
    ON DELETE CASCADE,
  CONSTRAINT fk_analysis_rule_version_based_on
    FOREIGN KEY (based_on_version_id) REFERENCES analysis_rule_versions (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS analysis_rule_replays (
  id INT NOT NULL AUTO_INCREMENT,
  rule_version_id INT NOT NULL,
  workflow_run_id INT NULL,
  dataset_snapshot_ref VARCHAR(1000) NOT NULL,
  dataset_hash VARCHAR(64) NOT NULL,
  metrics_json JSON NOT NULL,
  impact_json JSON NULL,
  artifact_refs_json JSON NULL,
  gate_passed TINYINT(1) NOT NULL DEFAULT 0,
  gate_reasons_json JSON NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'completed',
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_name VARCHAR(160) DEFAULT '',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_analysis_rule_replay_actor_idempotency
    (rule_version_id, actor_type, actor_id, idempotency_key),
  KEY ix_analysis_rule_replays_version (rule_version_id),
  KEY ix_analysis_rule_replays_workflow (workflow_run_id),
  KEY ix_analysis_rule_replays_dataset (dataset_hash),
  KEY ix_analysis_rule_replays_gate (gate_passed),
  KEY ix_analysis_rule_replays_created (created_at),
  CONSTRAINT fk_analysis_rule_replay_version
    FOREIGN KEY (rule_version_id) REFERENCES analysis_rule_versions (id)
    ON DELETE CASCADE,
  CONSTRAINT fk_analysis_rule_replay_workflow
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- active_version_id deliberately has no FK. This avoids a circular migration
-- dependency while versions already enforce ownership through rule_id.
