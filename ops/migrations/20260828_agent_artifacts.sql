-- P0-01: versioned Agent-to-Agent delivery artifacts.
-- MariaDB 10.1 compatible: JSON payloads are persisted as LONGTEXT.

CREATE TABLE IF NOT EXISTS agent_artifacts (
    id INT NOT NULL AUTO_INCREMENT,
    project_id INT NOT NULL,
    mission_id INT NOT NULL,
    stage_id VARCHAR(80) NOT NULL,
    workflow_run_id INT NULL,
    artifact_type VARCHAR(80) NOT NULL,
    schema_version INT NOT NULL DEFAULT 1,
    artifact_version INT NOT NULL DEFAULT 1,
    producer_type VARCHAR(16) NOT NULL,
    producer_id INT NOT NULL,
    producer_name VARCHAR(160) NOT NULL DEFAULT '',
    producer_claw_id INT NULL,
    producer_role_key VARCHAR(80) NOT NULL DEFAULT '',
    producer_provider VARCHAR(50) NOT NULL DEFAULT '',
    producer_profile_version INT NOT NULL DEFAULT 1,
    input_baseline_sha256 VARCHAR(71) NOT NULL DEFAULT '',
    content_json LONGTEXT NOT NULL,
    content_sha256 VARCHAR(71) NOT NULL,
    contract_validation_json LONGTEXT NOT NULL,
    validated_at DATETIME NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'draft',
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(64) NOT NULL,
    review_comment TEXT NULL,
    reviewed_by_type VARCHAR(16) NULL,
    reviewed_by_id INT NULL,
    reviewed_by_name VARCHAR(160) NULL,
    submitted_at DATETIME NULL,
    reviewed_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_agent_artifact_project
        FOREIGN KEY (project_id) REFERENCES projects (id),
    CONSTRAINT fk_agent_artifact_mission
        FOREIGN KEY (mission_id) REFERENCES workflow_missions (id),
    CONSTRAINT fk_agent_artifact_run
        FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id),
    CONSTRAINT fk_agent_artifact_producer_claw
        FOREIGN KEY (producer_claw_id) REFERENCES openclaw_instances (id),
    UNIQUE KEY uq_agent_artifact_mission_stage_type_version
        (mission_id, stage_id, artifact_type, artifact_version),
    UNIQUE KEY uq_agent_artifact_producer_idempotency
        (project_id, producer_type, producer_id, idempotency_key),
    KEY ix_agent_artifact_project_id (project_id),
    KEY ix_agent_artifact_mission_id (mission_id),
    KEY ix_agent_artifact_workflow_run_id (workflow_run_id),
    KEY ix_agent_artifact_artifact_type (artifact_type),
    KEY ix_agent_artifact_producer_claw_id (producer_claw_id),
    KEY ix_agent_artifact_content_sha256 (content_sha256),
    KEY ix_agent_artifact_status (status),
    KEY ix_agent_artifact_mission_status_created
        (mission_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
