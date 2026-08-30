-- P0-02: Mission Stage state, optimistic locking, and execution fencing.
-- Apply after 20260828_agent_artifacts.sql.

CREATE TABLE IF NOT EXISTS mission_stages (
    id INT NOT NULL AUTO_INCREMENT,
    mission_id INT NOT NULL,
    stage_key VARCHAR(80) NOT NULL,
    stage_version INT NOT NULL DEFAULT 1,
    role_key VARCHAR(80) NOT NULL,
    assigned_claw_id INT NULL,
    workflow_run_id INT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'ready',
    input_snapshot_json LONGTEXT NOT NULL,
    output_artifact_id INT NULL,
    active_handoff_id INT NULL,
    evidence_refs_json LONGTEXT NOT NULL,
    last_reason_code VARCHAR(80) NOT NULL DEFAULT '',
    fencing_token INT NOT NULL DEFAULT 0,
    retry_count INT NOT NULL DEFAULT 0,
    repair_count INT NOT NULL DEFAULT 0,
    version INT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_mission_stage_mission
        FOREIGN KEY (mission_id) REFERENCES workflow_missions (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_mission_stage_assigned_claw
        FOREIGN KEY (assigned_claw_id) REFERENCES openclaw_instances (id),
    CONSTRAINT fk_mission_stage_workflow_run
        FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id),
    CONSTRAINT fk_mission_stage_output_artifact
        FOREIGN KEY (output_artifact_id) REFERENCES agent_artifacts (id),
    UNIQUE KEY uq_mission_stage_key_version
        (mission_id, stage_key, stage_version),
    KEY ix_mission_stage_mission_id (mission_id),
    KEY ix_mission_stage_assigned_claw_id (assigned_claw_id),
    KEY ix_mission_stage_workflow_run_id (workflow_run_id),
    KEY ix_mission_stage_state (state),
    KEY ix_mission_stage_output_artifact_id (output_artifact_id),
    KEY ix_mission_stage_active_handoff_id (active_handoff_id),
    KEY ix_mission_stage_mission_state
        (mission_id, state, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

