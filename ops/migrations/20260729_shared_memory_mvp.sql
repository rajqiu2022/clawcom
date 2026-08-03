-- Shared memory MVP schema for OpenClaw Hub (MySQL).
-- Rollback strategy: disable memory_* feature flags; do not drop tables.

CREATE TABLE IF NOT EXISTS memory_scope_refs (
  id CHAR(36) NOT NULL PRIMARY KEY,
  scope VARCHAR(16) NOT NULL,
  owner_username VARCHAR(50) NULL,
  project_id INT NULL,
  created_at DATETIME NOT NULL,
  UNIQUE KEY uq_memory_scope_owner (scope, owner_username),
  UNIQUE KEY uq_memory_scope_project (scope, project_id),
  KEY ix_memory_scope_lookup (scope, owner_username, project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS memory_records (
  id CHAR(36) NOT NULL PRIMARY KEY,
  tenant_id INT NOT NULL DEFAULT 1,
  scope VARCHAR(16) NOT NULL,
  scope_ref CHAR(36) NOT NULL,
  kind VARCHAR(16) NOT NULL,
  memory_key VARCHAR(128) NOT NULL,
  value VARCHAR(4000) NULL,
  confidence DECIMAL(4,3) NULL,
  record_version BIGINT NOT NULL DEFAULT 1,
  tombstone TINYINT(1) NOT NULL DEFAULT 0,
  disabled TINYINT(1) NOT NULL DEFAULT 0,
  source_kind VARCHAR(24) NOT NULL,
  source_id VARCHAR(256) NOT NULL,
  created_by_claw_id INT NOT NULL,
  updated_by_claw_id INT NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  deleted_at DATETIME NULL,
  purge_after DATETIME NULL,
  UNIQUE KEY uq_memory_record_business_key (
    tenant_id, scope, scope_ref, kind, memory_key
  ),
  KEY ix_memory_records_delta_read (
    tenant_id, scope, scope_ref, tombstone, updated_at, id
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS memory_mutation_requests (
  id CHAR(36) NOT NULL PRIMARY KEY,
  tenant_id INT NOT NULL DEFAULT 1,
  claw_id INT NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  request_hash CHAR(64) NOT NULL,
  http_status SMALLINT NULL,
  response_body LONGTEXT NULL,
  state VARCHAR(12) NOT NULL DEFAULT 'processing',
  created_at DATETIME NOT NULL,
  completed_at DATETIME NULL,
  expires_at DATETIME NOT NULL,
  UNIQUE KEY uq_memory_request_idempotency (
    tenant_id, claw_id, idempotency_key
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS memory_mutations (
  id CHAR(36) NOT NULL PRIMARY KEY,
  tenant_id INT NOT NULL DEFAULT 1,
  claw_id INT NOT NULL,
  request_id CHAR(36) NOT NULL,
  mutation_id CHAR(36) NOT NULL,
  source_id VARCHAR(256) NOT NULL,
  source_kind VARCHAR(24) NOT NULL,
  record_id CHAR(36) NULL,
  result VARCHAR(12) NOT NULL,
  error_code VARCHAR(48) NULL,
  base_version BIGINT NULL,
  result_version BIGINT NULL,
  response_item LONGTEXT NOT NULL,
  created_at DATETIME NOT NULL,
  UNIQUE KEY uq_memory_mutation_once (tenant_id, claw_id, mutation_id),
  KEY ix_memory_mutation_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS memory_change_log (
  sequence BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  tenant_id INT NOT NULL DEFAULT 1,
  scope VARCHAR(16) NOT NULL,
  scope_ref CHAR(36) NOT NULL,
  record_id CHAR(36) NOT NULL,
  record_version BIGINT NOT NULL,
  change_type VARCHAR(12) NOT NULL,
  changed_at DATETIME NOT NULL,
  UNIQUE KEY uq_memory_change_record_version (record_id, record_version),
  KEY ix_memory_change_delta (tenant_id, scope, scope_ref, sequence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
