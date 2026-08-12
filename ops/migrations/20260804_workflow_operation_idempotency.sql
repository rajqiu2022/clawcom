-- Persist workflow worker idempotency keys for write-after-retry safety.

CREATE TABLE IF NOT EXISTS workflow_operation_idempotencies (
  id INT AUTO_INCREMENT PRIMARY KEY,
  actor_type VARCHAR(20) NOT NULL DEFAULT 'claw',
  actor_id INT NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  method VARCHAR(10) NOT NULL,
  path VARCHAR(300) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  response_status INT NULL,
  response_body_json LONGTEXT NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  expires_at DATETIME NULL,
  UNIQUE KEY uq_workflow_operation_idempotency_actor_key
    (actor_type, actor_id, idempotency_key),
  KEY idx_workflow_idempotency_actor (actor_type, actor_id),
  KEY idx_workflow_idempotency_expires_at (expires_at)
);
