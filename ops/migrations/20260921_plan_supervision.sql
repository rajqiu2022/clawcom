-- Apply before enabling PLAN_SUPERVISION_ENABLED. No Plan is activated here.
-- LONGTEXT retains compatibility with the production MariaDB 10.1 JSON mapping.
CREATE TABLE IF NOT EXISTS plan_supervisors (
    plan_id INT NOT NULL PRIMARY KEY,
    team_id INT,
    orchestrator_claw_id INT NOT NULL,
    mission_id INT UNIQUE,
    status VARCHAR(24) NOT NULL,
    `cursor` INT NOT NULL,
    acknowledged_cursor INT NOT NULL,
    next_check_at DATETIME,
    resume_condition VARCHAR(32),
    last_progress_at DATETIME,
    last_decision_json LONGTEXT,
    observations_json LONGTEXT,
    wake_message_id INT,
    last_wake_at DATETIME,
    lease_owner VARCHAR(128),
    fencing_token INT NOT NULL,
    lease_cursor INT,
    lease_expires_at DATETIME,
    turn_deadline_at DATETIME,
    expired_turns INT NOT NULL,
    start_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    KEY ix_plan_supervisors_next_check_at (next_check_at),
    KEY ix_plan_supervisors_team_id (team_id),
    FOREIGN KEY (team_id) REFERENCES agent_teams(id),
    FOREIGN KEY (plan_id) REFERENCES test_plans(id),
    FOREIGN KEY (orchestrator_claw_id) REFERENCES openclaw_instances(id),
    FOREIGN KEY (mission_id) REFERENCES workflow_missions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS plan_supervisor_events (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    plan_id INT NOT NULL,
    event_key VARCHAR(64) NOT NULL,
    kind VARCHAR(48) NOT NULL,
    payload_json LONGTEXT NOT NULL,
    sequence INT,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_plan_supervisor_event UNIQUE (plan_id, event_key),
    KEY ix_plan_supervisor_events_plan_id (plan_id),
    KEY ix_plan_supervisor_sequence (plan_id, sequence),
    FOREIGN KEY (plan_id) REFERENCES test_plans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS plan_supervisor_receipts (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    plan_id INT NOT NULL,
    action VARCHAR(32) NOT NULL,
    command_key VARCHAR(96) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_plan_supervisor_receipt UNIQUE (plan_id, action, command_key),
    FOREIGN KEY (plan_id) REFERENCES test_plans(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
