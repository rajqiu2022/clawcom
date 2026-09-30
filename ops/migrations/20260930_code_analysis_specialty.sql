-- Additive migration. Back up the database first; execute before enabling
-- CODE_ANALYSIS_ENABLED. Requires the existing shift-left and journal tables.
-- MariaDB 10.1-compatible: ORM JSON values are stored as LONGTEXT.
CREATE TABLE IF NOT EXISTS shared_resource_policies (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  resource_kind VARCHAR(20) NOT NULL,
  resource_id INT NOT NULL,
  owner_project_id INT NULL,
  scope VARCHAR(20) NOT NULL DEFAULT 'project',
  project_ids_json LONGTEXT NULL,
  created_by VARCHAR(120) NOT NULL,
  revision INT NOT NULL DEFAULT 1,
  updated_at DATETIME NULL,
  UNIQUE KEY uq_shared_resource_identity (resource_kind, resource_id),
  FOREIGN KEY (owner_project_id) REFERENCES projects(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS code_analysis_projects (
  project_id INT NOT NULL PRIMARY KEY,
  knowledge_id INT NOT NULL,
  knowledge_ids_json LONGTEXT NULL,
  skill_id INT NULL,
  executor_claw_id INT NULL,
  definition_id INT NULL,
  repository_url VARCHAR(1000) NULL,
  source_ref VARCHAR(255) NULL,
  revision INT NOT NULL DEFAULT 1,
  updated_at DATETIME NULL,
  FOREIGN KEY (project_id) REFERENCES projects(id),
  FOREIGN KEY (knowledge_id) REFERENCES knowledge_entries(id),
  FOREIGN KEY (skill_id) REFERENCES skills(id),
  FOREIGN KEY (executor_claw_id) REFERENCES openclaw_instances(id),
  FOREIGN KEY (definition_id) REFERENCES workflow_definitions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS code_analysis_jobs (
  analysis_run_id INT NOT NULL PRIMARY KEY,
  plan_id INT NOT NULL,
  executor_claw_id INT NOT NULL,
  definition_id INT NOT NULL,
  snapshot_json LONGTEXT NOT NULL,
  request_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  created_at DATETIME NULL,
  UNIQUE KEY uq_code_analysis_job_request (request_key),
  KEY ix_code_analysis_jobs_plan_id (plan_id),
  FOREIGN KEY (analysis_run_id) REFERENCES shift_left_analysis_runs(id),
  FOREIGN KEY (plan_id) REFERENCES test_plans(id),
  FOREIGN KEY (executor_claw_id) REFERENCES openclaw_instances(id),
  FOREIGN KEY (definition_id) REFERENCES workflow_definitions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS code_analysis_decisions (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  finding_id INT NOT NULL,
  analysis_run_id INT NOT NULL,
  feedback_id INT NULL,
  action VARCHAR(20) NOT NULL,
  truth_label VARCHAR(32) NOT NULL DEFAULT 'unknown',
  reason LONGTEXT NOT NULL,
  snapshot_json LONGTEXT NOT NULL,
  actor_name VARCHAR(120) NOT NULL,
  request_key VARCHAR(128) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  submission_status VARCHAR(24) NULL DEFAULT 'not_requested',
  bug_id VARCHAR(64) NULL,
  learning_status VARCHAR(24) NOT NULL DEFAULT 'pending',
  learning_run_id INT NULL,
  learned_revision INT NULL,
  learning_summary LONGTEXT NULL,
  general_proposal LONGTEXT NULL,
  general_published_revision INT NULL,
  learning_result_hash VARCHAR(64) NULL,
  created_at DATETIME NULL,
  UNIQUE KEY uq_code_analysis_decision_request (request_key),
  KEY ix_code_analysis_decisions_finding_id (finding_id),
  FOREIGN KEY (finding_id) REFERENCES shift_left_findings(id),
  FOREIGN KEY (analysis_run_id) REFERENCES code_analysis_jobs(analysis_run_id),
  FOREIGN KEY (feedback_id) REFERENCES shift_left_finding_feedback(id),
  FOREIGN KEY (learning_run_id) REFERENCES workflow_runs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
