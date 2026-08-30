-- P0-H: extend the existing minimum Capability Gap into a delivery lifecycle.

ALTER TABLE capability_gaps
  ADD COLUMN IF NOT EXISTS required_operations_json JSON NULL
    AFTER missing_capabilities_json,
  ADD COLUMN IF NOT EXISTS required_observables_json JSON NULL
    AFTER required_operations_json,
  ADD COLUMN IF NOT EXISTS required_reset_hooks_json JSON NULL
    AFTER required_observables_json,
  ADD COLUMN IF NOT EXISTS owner VARCHAR(160) DEFAULT ''
    AFTER candidate_requeue_pending,
  ADD COLUMN IF NOT EXISTS development_requirement_ref VARCHAR(500) DEFAULT ''
    AFTER owner,
  ADD COLUMN IF NOT EXISTS development_requirement_url VARCHAR(1000) DEFAULT ''
    AFTER development_requirement_ref,
  ADD COLUMN IF NOT EXISTS development_requirement_status VARCHAR(64) DEFAULT ''
    AFTER development_requirement_url,
  ADD COLUMN IF NOT EXISTS resolution_json JSON NULL
    AFTER development_requirement_status,
  ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(160) DEFAULT ''
    AFTER resolution_json,
  ADD COLUMN IF NOT EXISTS resolved_at DATETIME NULL
    AFTER resolved_by;

ALTER TABLE capability_gaps
  ADD INDEX IF NOT EXISTS ix_capability_gaps_owner (owner),
  ADD INDEX IF NOT EXISTS ix_capability_gaps_requirement_ref
    (development_requirement_ref(191));

CREATE TABLE IF NOT EXISTS capability_gap_events (
  id INT NOT NULL AUTO_INCREMENT,
  capability_gap_id INT NOT NULL,
  event_type VARCHAR(48) NOT NULL,
  from_status VARCHAR(32) NULL,
  to_status VARCHAR(32) NULL,
  version_before INT NULL,
  version_after INT NULL,
  payload_json JSON NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_name VARCHAR(160) DEFAULT '',
  request_id VARCHAR(128) NULL,
  idempotency_key VARCHAR(128) NULL,
  request_hash VARCHAR(64) NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_gap_event_actor_idempotency
    (capability_gap_id, event_type, actor_type, actor_id, idempotency_key),
  KEY ix_capability_gap_events_gap (capability_gap_id),
  KEY ix_capability_gap_events_type (event_type),
  KEY ix_capability_gap_events_request (request_id),
  KEY ix_capability_gap_events_created (created_at),
  CONSTRAINT fk_capability_gap_event_gap
    FOREIGN KEY (capability_gap_id) REFERENCES capability_gaps (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Existing rows stay open and version 1. They can be linked to development
-- requirements with the idempotent upsert API without changing candidates.
