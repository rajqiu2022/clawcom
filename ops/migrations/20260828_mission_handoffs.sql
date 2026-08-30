-- P0-03: immutable, versioned Mission Handoff contracts.
-- Apply after 20260828_agent_artifacts.sql and 20260828_mission_stages.sql.

CREATE TABLE IF NOT EXISTS mission_handoffs (
    id INT NOT NULL AUTO_INCREMENT,
    mission_id INT NOT NULL,
    mission_version INT NOT NULL,
    handoff_version INT NOT NULL DEFAULT 1,
    source_stage_record_id INT NOT NULL,
    target_stage_record_id INT NOT NULL,
    source_stage_key VARCHAR(80) NOT NULL,
    target_stage_key VARCHAR(80) NOT NULL,
    from_role VARCHAR(80) NOT NULL,
    to_role VARCHAR(80) NOT NULL,
    objective TEXT NOT NULL,
    input_artifact_refs_json LONGTEXT NOT NULL,
    accepted_findings_json LONGTEXT NOT NULL,
    open_questions_json LONGTEXT NOT NULL,
    constraints_json LONGTEXT NOT NULL,
    acceptance_criteria_json LONGTEXT NOT NULL,
    known_pitfalls_json LONGTEXT NOT NULL,
    recommended_next_actions_json LONGTEXT NOT NULL,
    evidence_refs_json LONGTEXT NOT NULL,
    source_run_id INT NULL,
    source_step_id VARCHAR(100) NOT NULL DEFAULT '',
    producer_type VARCHAR(16) NOT NULL,
    producer_id INT NOT NULL,
    producer_name VARCHAR(160) NOT NULL DEFAULT '',
    producer_claw_id INT NULL,
    producer_role_key VARCHAR(80) NOT NULL DEFAULT '',
    producer_provider VARCHAR(50) NOT NULL DEFAULT '',
    producer_profile_version INT NOT NULL DEFAULT 1,
    status VARCHAR(24) NOT NULL DEFAULT 'draft',
    content_sha256 VARCHAR(71) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(71) NOT NULL,
    version INT NOT NULL DEFAULT 1,
    decision_reason_code VARCHAR(80) NOT NULL DEFAULT '',
    decision_comment TEXT NULL,
    decided_by_type VARCHAR(16) NULL,
    decided_by_id INT NULL,
    decided_by_name VARCHAR(160) NULL,
    submitted_at DATETIME NULL,
    accepted_at DATETIME NULL,
    rejected_at DATETIME NULL,
    superseded_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_mission_handoff_mission
        FOREIGN KEY (mission_id) REFERENCES workflow_missions (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_mission_handoff_source_stage
        FOREIGN KEY (source_stage_record_id) REFERENCES mission_stages (id),
    CONSTRAINT fk_mission_handoff_target_stage
        FOREIGN KEY (target_stage_record_id) REFERENCES mission_stages (id),
    CONSTRAINT fk_mission_handoff_producer_claw
        FOREIGN KEY (producer_claw_id) REFERENCES openclaw_instances (id),
    UNIQUE KEY uq_mission_handoff_path_version
        (mission_id, source_stage_key, target_stage_key, handoff_version),
    UNIQUE KEY uq_mission_handoff_producer_idempotency
        (mission_id, producer_type, producer_id, idempotency_key),
    KEY ix_mission_handoff_mission_id (mission_id),
    KEY ix_mission_handoff_source_stage_record_id (source_stage_record_id),
    KEY ix_mission_handoff_target_stage_record_id (target_stage_record_id),
    KEY ix_mission_handoff_producer_claw_id (producer_claw_id),
    KEY ix_mission_handoff_status (status),
    KEY ix_mission_handoff_content_sha256 (content_sha256),
    KEY ix_mission_handoff_target_status
        (mission_id, target_stage_record_id, status),
    KEY ix_mission_handoff_source_run_step
        (source_run_id, source_step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

