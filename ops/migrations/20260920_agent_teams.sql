-- Team MVP: separate roles, one active manager lease, immutable Mission binding.
-- Prerequisites: workflow_missions, openclaw_instances, projects.
-- Delivery contracts additionally require, in order:
-- 20260804_workflow_operation_idempotency.sql
-- 20260828_agent_artifacts.sql
-- 20260828_mission_stages.sql
-- 20260828_mission_handoffs.sql
-- MariaDB 10.1: LONGTEXT intentionally used instead of JSON.
-- Additive only; keep feature gates OFF until schema and API smoke checks pass.

CREATE TABLE IF NOT EXISTS agent_teams (
    id INT NOT NULL AUTO_INCREMENT,
    project_id INT NOT NULL,
    name VARCHAR(160) NOT NULL,
    objective TEXT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    primary_manager_claw_id INT NOT NULL,
    backup_manager_claw_id INT NULL,
    active_manager_claw_id INT NULL,
    manager_epoch INT NOT NULL DEFAULT 0,
    manager_session_id VARCHAR(128) NOT NULL DEFAULT '',
    manager_lease_expires_at DATETIME NULL,
    policy_json LONGTEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_agent_team_project_name (project_id, name),
    KEY ix_agent_teams_project_id (project_id),
    FOREIGN KEY (project_id) REFERENCES projects (id),
    FOREIGN KEY (primary_manager_claw_id) REFERENCES openclaw_instances (id),
    FOREIGN KEY (backup_manager_claw_id) REFERENCES openclaw_instances (id),
    FOREIGN KEY (active_manager_claw_id) REFERENCES openclaw_instances (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_team_members (
    id INT NOT NULL AUTO_INCREMENT,
    team_id INT NOT NULL,
    claw_id INT NOT NULL,
    role_key VARCHAR(40) NOT NULL,
    specialties_json LONGTEXT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_agent_team_member_role (team_id, claw_id, role_key),
    KEY ix_agent_team_members_team_id (team_id),
    KEY ix_agent_team_members_claw_id (claw_id),
    FOREIGN KEY (team_id) REFERENCES agent_teams (id),
    FOREIGN KEY (claw_id) REFERENCES openclaw_instances (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_team_missions (
    mission_id INT NOT NULL,
    team_id INT NOT NULL,
    team_version INT NOT NULL,
    snapshot_json LONGTEXT NOT NULL,
    plan_sha256 VARCHAR(64) NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (mission_id),
    KEY ix_agent_team_missions_team_id (team_id),
    FOREIGN KEY (mission_id) REFERENCES workflow_missions (id),
    FOREIGN KEY (team_id) REFERENCES agent_teams (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
