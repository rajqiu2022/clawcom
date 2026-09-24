-- Immutable dated executions for Hub-owned recurring TestTask templates.
-- Template/report columns are added conditionally by the release schema step
-- because production MariaDB 10.1 has no ADD COLUMN IF NOT EXISTS support.
-- The deployment schema step also adds nullable TestTask workflow_definition_id
-- and workflow_start_vars_json columns. They are omitted here because MariaDB
-- 10.1 cannot express additive idempotent ALTER TABLE in static SQL.
CREATE TABLE IF NOT EXISTS test_task_occurrences (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    plan_id INT NOT NULL,
    test_task_id INT NOT NULL,
    occurrence_date DATE NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    not_before_at DATETIME NOT NULL,
    due_at DATETIME,
    status VARCHAR(24) NOT NULL DEFAULT 'scheduled',
    assignee_claw_id INT NOT NULL,
    mission_stage_id INT,
    agent_task_id INT,
    workflow_run_id INT,
    attempt_count INT NOT NULL DEFAULT 0,
    next_action VARCHAR(64) DEFAULT 'wait_not_before',
    next_check_at DATETIME,
    last_heartbeat_at DATETIME,
    result_summary LONGTEXT,
    evidence_refs_json LONGTEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_test_task_occurrence_date
        UNIQUE (plan_id, test_task_id, occurrence_date),
    KEY ix_test_task_occurrence_dispatch (plan_id, status, not_before_at),
    KEY ix_test_task_occurrence_task (test_task_id),
    KEY ix_test_task_occurrence_agent_task (agent_task_id),
    FOREIGN KEY (plan_id) REFERENCES test_plans(id),
    FOREIGN KEY (test_task_id) REFERENCES test_tasks(id),
    FOREIGN KEY (assignee_claw_id) REFERENCES openclaw_instances(id),
    FOREIGN KEY (mission_stage_id) REFERENCES mission_stages(id),
    FOREIGN KEY (agent_task_id) REFERENCES agent_tasks(id),
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
