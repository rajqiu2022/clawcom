-- P0-D: immutable testcase-library content frozen for each Workflow Run.
-- Additive only. Snapshot failures are handled as warnings by application code.

CREATE TABLE IF NOT EXISTS workflow_run_library_snapshots (
  id INT NOT NULL AUTO_INCREMENT,
  workflow_run_id INT NOT NULL,
  library_id INT NOT NULL,
  library_revision INT NOT NULL,
  snapshot_version INT NOT NULL,
  content_hash VARCHAR(71) NOT NULL,
  case_ids_json JSON NULL,
  case_count INT NOT NULL DEFAULT 0,
  snapshot_json JSON NOT NULL,
  frozen_by VARCHAR(160) DEFAULT 'system',
  frozen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_workflow_run_library_snapshot_run (workflow_run_id),
  KEY ix_workflow_run_library_snapshots_run (workflow_run_id),
  KEY ix_workflow_run_library_snapshots_library (library_id),
  KEY ix_workflow_run_library_snapshots_hash (content_hash),
  KEY ix_workflow_run_library_snapshots_frozen (frozen_at),
  KEY ix_workflow_run_library_snapshot_library_revision
    (library_id, library_revision),
  CONSTRAINT fk_workflow_run_library_snapshot_run
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- library_id intentionally has no FK. A Run snapshot is historical evidence
-- and must remain readable if the source library is archived or deleted.
