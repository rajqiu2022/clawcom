-- P0-E: atomic Unity/Bridge/device leases and TTL hardening for the existing
-- test-account-manager. No parallel account lease table is introduced.

CREATE TABLE IF NOT EXISTS resource_leases (
  id INT NOT NULL AUTO_INCREMENT,
  lease_group_id VARCHAR(64) NOT NULL,
  resource_key VARCHAR(255) NOT NULL,
  resource_type VARCHAR(32) NOT NULL,
  active_slot VARCHAR(255) NULL,
  owner_type VARCHAR(40) NOT NULL,
  owner_id VARCHAR(160) NOT NULL,
  controller_run_id VARCHAR(160) NULL,
  priority INT NOT NULL DEFAULT 0,
  status VARCHAR(24) NOT NULL DEFAULT 'active',
  ttl_seconds INT NOT NULL DEFAULT 900,
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(71) NOT NULL,
  holder_actor_type VARCHAR(24) NOT NULL,
  holder_actor_id INT NOT NULL,
  holder_actor_name VARCHAR(160) DEFAULT '',
  metadata_json JSON NULL,
  acquired_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  heartbeat_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NOT NULL,
  released_at DATETIME NULL,
  release_reason VARCHAR(255) DEFAULT '',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_resource_lease_active_slot (active_slot),
  UNIQUE KEY uq_resource_lease_owner_idempotency_resource
    (owner_type, owner_id, idempotency_key, resource_key),
  KEY ix_resource_leases_group (lease_group_id),
  KEY ix_resource_leases_resource (resource_key),
  KEY ix_resource_leases_type (resource_type),
  KEY ix_resource_leases_owner_type (owner_type),
  KEY ix_resource_leases_owner_id (owner_id),
  KEY ix_resource_leases_controller (controller_run_id),
  KEY ix_resource_leases_status (status),
  KEY ix_resource_leases_acquired (acquired_at),
  KEY ix_resource_leases_expires (expires_at),
  KEY ix_resource_lease_owner_status (owner_type, owner_id, status),
  KEY ix_resource_lease_status_expiry (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS resource_lease_events (
  id INT NOT NULL AUTO_INCREMENT,
  lease_id INT NOT NULL,
  lease_group_id VARCHAR(64) NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  actor_type VARCHAR(24) NOT NULL,
  actor_id INT NULL,
  actor_name VARCHAR(160) DEFAULT '',
  payload_json JSON NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_resource_lease_events_lease (lease_id),
  KEY ix_resource_lease_events_group (lease_group_id),
  KEY ix_resource_lease_events_type (event_type),
  KEY ix_resource_lease_events_created (created_at),
  CONSTRAINT fk_resource_lease_event_lease
    FOREIGN KEY (lease_id) REFERENCES resource_leases (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE test_accounts
  ADD COLUMN IF NOT EXISTS lease_ttl_seconds INT NULL DEFAULT 1800
    AFTER last_login_at,
  ADD COLUMN IF NOT EXISTS lease_expires_at DATETIME NULL
    AFTER lease_ttl_seconds,
  ADD COLUMN IF NOT EXISTS lease_heartbeat_at DATETIME NULL
    AFTER lease_expires_at,
  ADD COLUMN IF NOT EXISTS workflow_run_id INT NULL
    AFTER lease_heartbeat_at,
  ADD COLUMN IF NOT EXISTS controller_run_id VARCHAR(160) NULL
    AFTER workflow_run_id;

ALTER TABLE test_accounts
  ADD INDEX IF NOT EXISTS ix_test_accounts_lease_expires (lease_expires_at),
  ADD INDEX IF NOT EXISTS ix_test_accounts_workflow_run (workflow_run_id),
  ADD INDEX IF NOT EXISTS ix_test_accounts_controller_run (controller_run_id);

-- MySQL ENUMs must be widened for keepalive and automatic-expiry audit.
ALTER TABLE test_account_usage_logs
  MODIFY COLUMN action ENUM(
    'acquire', 'renew', 'expire', 'release', 'mark_abnormal',
    'recover', 'create', 'delete') NOT NULL,
  MODIFY COLUMN actor_type ENUM('claw', 'user', 'system')
    NOT NULL DEFAULT 'claw';

-- Existing in-use rows deliberately keep lease_expires_at=NULL and therefore
-- retain legacy indefinite ownership until explicitly released. Only new
-- acquire calls receive a TTL, avoiding surprise production account release.
