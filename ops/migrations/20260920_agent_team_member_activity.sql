-- Agent-reported observations only. Apply after 20260920_agent_teams.sql.
-- Never backfill "idle" for members that have not reported.
CREATE TABLE IF NOT EXISTS agent_team_member_tasks (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    team_id INT NOT NULL,
    claw_id INT NOT NULL,
    task_key VARCHAR(96) NOT NULL,
    title VARCHAR(240) NOT NULL,
    task_type VARCHAR(24) NOT NULL,
    reference VARCHAR(240) NOT NULL DEFAULT '',
    status VARCHAR(24) NOT NULL,
    progress_percent INT NULL,
    progress_message TEXT,
    started_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    UNIQUE KEY uq_team_member_task_key (team_id, claw_id, task_key),
    KEY ix_team_member_tasks_history (team_id, claw_id, id),
    FOREIGN KEY (team_id) REFERENCES agent_teams(id),
    FOREIGN KEY (claw_id) REFERENCES openclaw_instances(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS agent_team_member_statuses (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    team_id INT NOT NULL,
    claw_id INT NOT NULL,
    state VARCHAR(24) NOT NULL,
    summary VARCHAR(500) NOT NULL DEFAULT '',
    current_task_id INT NULL,
    version INT NOT NULL DEFAULT 0,
    reported_at DATETIME NOT NULL,
    UNIQUE KEY uq_team_member_status (team_id, claw_id),
    FOREIGN KEY (team_id) REFERENCES agent_teams(id),
    FOREIGN KEY (claw_id) REFERENCES openclaw_instances(id),
    FOREIGN KEY (current_task_id) REFERENCES agent_team_member_tasks(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS agent_team_member_reports (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    team_id INT NOT NULL,
    claw_id INT NOT NULL,
    event_id VARCHAR(96) NOT NULL,
    request_sha256 VARCHAR(64) NOT NULL,
    task_id INT NULL,
    response_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE KEY uq_team_member_report_event (team_id, claw_id, event_id),
    KEY ix_team_member_report_task (team_id, claw_id, task_id, id),
    FOREIGN KEY (team_id) REFERENCES agent_teams(id),
    FOREIGN KEY (claw_id) REFERENCES openclaw_instances(id),
    FOREIGN KEY (task_id) REFERENCES agent_team_member_tasks(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
