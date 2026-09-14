ALTER TABLE automation_capabilities
  MODIFY COLUMN status VARCHAR(32) NOT NULL DEFAULT 'planned',
  ADD COLUMN IF NOT EXISTS implementation_status VARCHAR(32) NOT NULL DEFAULT 'declared',
  ADD COLUMN IF NOT EXISTS verification_status VARCHAR(32) NOT NULL DEFAULT 'unverified',
  ADD COLUMN IF NOT EXISTS producer_claw_id INT NULL,
  ADD COLUMN IF NOT EXISTS release_id VARCHAR(255) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS source_commit VARCHAR(64) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS manifest_sha256 VARCHAR(64) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS verification_json JSON NULL,
  ADD COLUMN IF NOT EXISTS health_expires_at DATETIME NULL;

CREATE INDEX IF NOT EXISTS ix_automation_capability_implementation_status
  ON automation_capabilities (implementation_status);
CREATE INDEX IF NOT EXISTS ix_automation_capability_verification_status
  ON automation_capabilities (verification_status);
CREATE INDEX IF NOT EXISTS ix_automation_capability_producer_claw_id
  ON automation_capabilities (producer_claw_id);
CREATE INDEX IF NOT EXISTS ix_automation_capability_health_expires_at
  ON automation_capabilities (health_expires_at);

CREATE TABLE IF NOT EXISTS automation_capability_events (
  id INT NOT NULL AUTO_INCREMENT,
  capability_id INT NOT NULL,
  event_type VARCHAR(48) NOT NULL,
  from_status VARCHAR(32) NULL,
  to_status VARCHAR(32) NULL,
  version_before INT NULL,
  version_after INT NULL,
  payload_json JSON NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NOT NULL,
  actor_name VARCHAR(160) NOT NULL DEFAULT '',
  request_id VARCHAR(128) NULL,
  idempotency_key VARCHAR(128) NULL,
  request_hash VARCHAR(64) NULL,
  created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  CONSTRAINT fk_automation_capability_event_capability
    FOREIGN KEY (capability_id) REFERENCES automation_capabilities (id)
    ON DELETE CASCADE,
  UNIQUE KEY uq_capability_event_actor_idempotency
    (capability_id, actor_type, actor_id, idempotency_key),
  KEY ix_automation_capability_event_capability_id (capability_id),
  KEY ix_automation_capability_event_type (event_type),
  KEY ix_automation_capability_event_request_id (request_id),
  KEY ix_automation_capability_event_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
