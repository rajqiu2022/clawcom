-- P0-G: revisioned Workflow evidence manifests and hard post-run verdict rules.

CREATE TABLE IF NOT EXISTS workflow_evidence_manifests (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  workflow_run_id INT NOT NULL,
  analysis_run_id INT NULL,
  coverage_json JSON NOT NULL,
  artifacts_json JSON NOT NULL,
  required_evidence_json JSON NOT NULL,
  completeness_status VARCHAR(32) NOT NULL DEFAULT 'incomplete',
  missing_required_json JSON NULL,
  classification VARCHAR(40) NULL,
  analysis_summary_json JSON NULL,
  finding_ids_json JSON NULL,
  analyzed_by VARCHAR(160) DEFAULT '',
  analyzed_at DATETIME NULL,
  revision INT NOT NULL DEFAULT 1,
  created_by VARCHAR(160) DEFAULT 'system',
  updated_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_workflow_evidence_manifest_run (workflow_run_id),
  KEY ix_workflow_evidence_manifest_project (project_id),
  KEY ix_workflow_evidence_manifest_analysis (analysis_run_id),
  KEY ix_workflow_evidence_manifest_status (completeness_status),
  KEY ix_workflow_evidence_manifest_classification (classification),
  KEY ix_workflow_evidence_manifest_updated (updated_at),
  KEY ix_workflow_evidence_project_status (project_id, completeness_status),
  KEY ix_workflow_evidence_project_updated (project_id, updated_at),
  CONSTRAINT fk_workflow_evidence_project
    FOREIGN KEY (project_id) REFERENCES projects (id),
  CONSTRAINT fk_workflow_evidence_run
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id)
    ON DELETE CASCADE,
  CONSTRAINT fk_workflow_evidence_analysis
    FOREIGN KEY (analysis_run_id) REFERENCES shift_left_analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Large logs, screenshots, video and performance captures stay in artifact
-- storage. artifacts_json contains only URI, SHA-256, size and small metadata.
