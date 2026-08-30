-- P0-04: versioned Agent evaluation datasets, cases, runs, and scores.

CREATE TABLE IF NOT EXISTS agent_eval_datasets (
    id INT NOT NULL AUTO_INCREMENT,
    project_id INT NOT NULL,
    dataset_key VARCHAR(100) NOT NULL,
    role_key VARCHAR(80) NOT NULL,
    version INT NOT NULL DEFAULT 1,
    split VARCHAR(24) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'draft',
    rubric_version INT NOT NULL DEFAULT 1,
    dataset_sha256 VARCHAR(71) NOT NULL DEFAULT '',
    review_policy_json LONGTEXT NOT NULL,
    review_status VARCHAR(24) NOT NULL DEFAULT 'not_required',
    review_records_json LONGTEXT NOT NULL,
    review_completed_at DATETIME NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(71) NOT NULL,
    created_by_type VARCHAR(16) NOT NULL,
    created_by_id INT NOT NULL,
    created_by_name VARCHAR(160) NOT NULL DEFAULT '',
    approved_by_type VARCHAR(16) NULL,
    approved_by_id INT NULL,
    approved_by_name VARCHAR(160) NULL,
    frozen_at DATETIME NULL,
    retired_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_agent_eval_dataset_project
        FOREIGN KEY (project_id) REFERENCES projects (id),
    UNIQUE KEY uq_agent_eval_dataset_project_key_version
        (project_id, dataset_key, version),
    UNIQUE KEY uq_agent_eval_dataset_idempotency
        (project_id, created_by_type, created_by_id, idempotency_key),
    KEY ix_agent_eval_dataset_project_id (project_id),
    KEY ix_agent_eval_dataset_role_key (role_key),
    KEY ix_agent_eval_dataset_split (split),
    KEY ix_agent_eval_dataset_status (status),
    KEY ix_agent_eval_dataset_review_status (review_status),
    KEY ix_agent_eval_dataset_project_role_status
        (project_id, role_key, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_eval_cases (
    id INT NOT NULL AUTO_INCREMENT,
    dataset_id INT NOT NULL,
    case_key VARCHAR(120) NOT NULL,
    input_snapshot_json LONGTEXT NOT NULL,
    expected_contract_json LONGTEXT NOT NULL,
    required_evidence_json LONGTEXT NOT NULL,
    deterministic_checks_json LONGTEXT NOT NULL,
    allowed_variance_json LONGTEXT NOT NULL,
    hidden_tags_json LONGTEXT NOT NULL,
    input_sha256 VARCHAR(71) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(71) NOT NULL,
    created_by_type VARCHAR(16) NOT NULL,
    created_by_id INT NOT NULL,
    created_by_name VARCHAR(160) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_agent_eval_case_dataset
        FOREIGN KEY (dataset_id) REFERENCES agent_eval_datasets (id)
        ON DELETE CASCADE,
    UNIQUE KEY uq_agent_eval_case_dataset_key (dataset_id, case_key),
    UNIQUE KEY uq_agent_eval_case_idempotency
        (dataset_id, created_by_type, created_by_id, idempotency_key),
    KEY ix_agent_eval_case_dataset_id (dataset_id),
    KEY ix_agent_eval_case_input_sha256 (input_sha256),
    KEY ix_agent_eval_case_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_eval_runs (
    id INT NOT NULL AUTO_INCREMENT,
    project_id INT NOT NULL,
    case_id INT NOT NULL,
    dataset_version INT NOT NULL,
    case_input_sha256 VARCHAR(71) NOT NULL,
    claw_id INT NOT NULL,
    profile_version INT NOT NULL,
    provider VARCHAR(50) NOT NULL,
    model VARCHAR(120) NOT NULL,
    worker_release VARCHAR(160) NOT NULL,
    mission_id INT NULL,
    workflow_run_id INT NULL,
    artifact_id INT NULL,
    status VARCHAR(32) NOT NULL,
    usage_json LONGTEXT NOT NULL,
    timing_json LONGTEXT NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(71) NOT NULL,
    created_by_type VARCHAR(16) NOT NULL,
    created_by_id INT NOT NULL,
    created_by_name VARCHAR(160) NOT NULL DEFAULT '',
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_agent_eval_run_project
        FOREIGN KEY (project_id) REFERENCES projects (id),
    CONSTRAINT fk_agent_eval_run_case
        FOREIGN KEY (case_id) REFERENCES agent_eval_cases (id),
    CONSTRAINT fk_agent_eval_run_claw
        FOREIGN KEY (claw_id) REFERENCES openclaw_instances (id),
    CONSTRAINT fk_agent_eval_run_mission
        FOREIGN KEY (mission_id) REFERENCES workflow_missions (id),
    CONSTRAINT fk_agent_eval_run_workflow_run
        FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs (id),
    CONSTRAINT fk_agent_eval_run_artifact
        FOREIGN KEY (artifact_id) REFERENCES agent_artifacts (id),
    UNIQUE KEY uq_agent_eval_run_idempotency
        (project_id, created_by_type, created_by_id, idempotency_key),
    KEY ix_agent_eval_run_project_id (project_id),
    KEY ix_agent_eval_run_case_id (case_id),
    KEY ix_agent_eval_run_claw_id (claw_id),
    KEY ix_agent_eval_run_mission_id (mission_id),
    KEY ix_agent_eval_run_workflow_run_id (workflow_run_id),
    KEY ix_agent_eval_run_artifact_id (artifact_id),
    KEY ix_agent_eval_run_status (status),
    KEY ix_agent_eval_run_project_status_created
        (project_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_eval_scores (
    id INT NOT NULL AUTO_INCREMENT,
    eval_run_id INT NOT NULL,
    scorer_type VARCHAR(24) NOT NULL,
    scorer_version INT NOT NULL,
    scorer_actor_type VARCHAR(16) NOT NULL,
    scorer_actor_id INT NOT NULL,
    scorer_name VARCHAR(160) NOT NULL DEFAULT '',
    dimension_scores_json LONGTEXT NOT NULL,
    fatal_violations_json LONGTEXT NOT NULL,
    total_score DOUBLE NOT NULL,
    review_comment TEXT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(71) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_agent_eval_score_run
        FOREIGN KEY (eval_run_id) REFERENCES agent_eval_runs (id)
        ON DELETE CASCADE,
    UNIQUE KEY uq_agent_eval_score_scorer_version
        (eval_run_id, scorer_type, scorer_actor_type,
         scorer_actor_id, scorer_version),
    UNIQUE KEY uq_agent_eval_score_idempotency
        (scorer_actor_type, scorer_actor_id, idempotency_key),
    KEY ix_agent_eval_score_eval_run_id (eval_run_id),
    KEY ix_agent_eval_score_scorer_type (scorer_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
