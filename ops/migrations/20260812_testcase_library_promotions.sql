-- P0-C: formal testcase-library revisions and atomic candidate promotion.
-- Additive only: legacy testcase CRUD, testcase_snapshots and Flow #12 are unchanged.

ALTER TABLE test_case_libraries
  ADD COLUMN IF NOT EXISTS revision INT NOT NULL DEFAULT 0 AFTER updated_by,
  ADD COLUMN IF NOT EXISTS content_hash VARCHAR(71) NOT NULL DEFAULT '' AFTER revision;

CREATE TABLE IF NOT EXISTS testcase_library_promotions (
  id INT NOT NULL AUTO_INCREMENT,
  library_id INT NOT NULL,
  operation_type VARCHAR(24) NOT NULL DEFAULT 'promotion',
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(71) NOT NULL,
  before_revision INT NOT NULL,
  after_revision INT NOT NULL,
  content_hash VARCHAR(71) NOT NULL,
  rollback_from_revision INT NULL,
  candidate_ids_json JSON NULL,
  created_case_ids_json JSON NULL,
  updated_case_ids_json JSON NULL,
  source_run_ids_json JSON NULL,
  message VARCHAR(500) DEFAULT '',
  response_json JSON NOT NULL,
  created_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_testcase_library_promotion_idempotency
    (library_id, idempotency_key),
  KEY ix_testcase_library_promotions_library (library_id),
  KEY ix_testcase_library_promotions_type (operation_type),
  KEY ix_testcase_library_promotions_created (created_at),
  KEY ix_testcase_library_promotion_created (library_id, created_at),
  CONSTRAINT fk_testcase_library_promotion_library
    FOREIGN KEY (library_id) REFERENCES test_case_libraries (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS testcase_library_revisions (
  id INT NOT NULL AUTO_INCREMENT,
  library_id INT NOT NULL,
  revision INT NOT NULL,
  content_hash VARCHAR(71) NOT NULL,
  case_count INT NOT NULL DEFAULT 0,
  snapshot_json JSON NOT NULL,
  source_type VARCHAR(32) NOT NULL DEFAULT 'baseline',
  source_reference VARCHAR(160) DEFAULT '',
  rollback_from_revision INT NULL,
  message VARCHAR(500) DEFAULT '',
  created_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_testcase_library_revision (library_id, revision),
  KEY ix_testcase_library_revisions_library (library_id),
  KEY ix_testcase_library_revisions_hash (content_hash),
  KEY ix_testcase_library_revisions_created (created_at),
  KEY ix_testcase_library_revision_created (library_id, created_at),
  CONSTRAINT fk_testcase_library_revision_library
    FOREIGN KEY (library_id) REFERENCES test_case_libraries (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Existing libraries are initialized lazily by the first new revision API call.
-- This computes a canonical full-content SHA-256 in application code and records
-- revision 0 without touching testcase content or legacy snapshots.
