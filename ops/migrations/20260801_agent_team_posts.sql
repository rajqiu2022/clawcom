-- Agent 测试团队闭环：岗位说明书、工位和值守关系。
-- MySQL / MariaDB，可重复执行。

CREATE TABLE IF NOT EXISTS agent_profiles (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  profile_key VARCHAR(80) NOT NULL COMMENT '稳定键，如 requirement_reviewer',
  name VARCHAR(120) NOT NULL COMMENT '岗位说明书名称',
  description TEXT NULL,
  system_prompt TEXT NULL COMMENT '岗位 system prompt / 人格说明',
  workflow_config TEXT NULL COMMENT '岗位工作规范',
  required_skills_json LONGTEXT NULL COMMENT '必装 skill key/path 列表（兼容旧 MariaDB）',
  contract_json LONGTEXT NULL COMMENT '岗位产出契约声明（兼容旧 MariaDB）',
  exam_paper_id INT NULL COMMENT '准入考试试卷 ID，可空',
  version INT NOT NULL DEFAULT 1,
  status VARCHAR(20) DEFAULT 'active' COMMENT 'active/disabled',
  created_by VARCHAR(100) DEFAULT '',
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  UNIQUE KEY uq_agent_profile_key (profile_key),
  KEY ix_agent_profile_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Agent 岗位说明书：Hub 可下发、可版本化、可考核的行为配置';

CREATE TABLE IF NOT EXISTS agent_posts (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  post_key VARCHAR(80) NOT NULL COMMENT '工位 key，如 requirement_analyst',
  name VARCHAR(120) NOT NULL,
  description TEXT NULL,
  project_id INT NULL COMMENT '为空表示全局工位',
  profile_id INT NOT NULL COMMENT '岗位说明书 ID',
  required_profile_version INT NOT NULL DEFAULT 1,
  status VARCHAR(20) DEFAULT 'active' COMMENT 'active/disabled',
  created_by VARCHAR(100) DEFAULT '',
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  UNIQUE KEY uq_agent_post_project_key (project_id, post_key),
  KEY ix_agent_post_key (post_key),
  KEY ix_agent_post_project (project_id),
  KEY ix_agent_post_profile (profile_id),
  KEY ix_agent_post_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Agent 团队工位：需求分析岗、工程分析岗、用例设计岗、独立评审岗等';

CREATE TABLE IF NOT EXISTS agent_post_assignments (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  post_id INT NOT NULL COMMENT '工位 ID',
  claw_id INT NOT NULL COMMENT 'OpenClaw 实例 ID',
  profile_version INT NOT NULL DEFAULT 1 COMMENT '该 agent 接受并通过的 profile 版本',
  exam_session_id INT NULL COMMENT '最近一次准入考试 session，可空',
  is_primary TINYINT(1) DEFAULT 0,
  status VARCHAR(20) DEFAULT 'active' COMMENT 'active/paused/disabled',
  assigned_by VARCHAR(100) DEFAULT '',
  assigned_at DATETIME NULL,
  updated_at DATETIME NULL,
  UNIQUE KEY uq_agent_post_assignment (post_id, claw_id),
  KEY ix_agent_assignment_post (post_id),
  KEY ix_agent_assignment_claw (claw_id),
  KEY ix_agent_assignment_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Agent 工位值守关系';

ALTER TABLE workflow_run_steps
  ADD COLUMN IF NOT EXISTS target_post VARCHAR(80) DEFAULT ''
  COMMENT '目标工位 key，如 requirement_analyst';

ALTER TABLE workflow_run_steps
  ADD INDEX IF NOT EXISTS ix_workflow_run_steps_target_post (target_post);

CREATE TABLE IF NOT EXISTS requirement_review_verdicts (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  requirement_item_id INT NOT NULL COMMENT '需求快照 ID',
  iteration_id INT NOT NULL COMMENT 'Hub TestIteration ID',
  review_key VARCHAR(80) NOT NULL DEFAULT 'default'
    COMMENT '评审批次键：workflow run id 或 default',
  verdict VARCHAR(20) NOT NULL DEFAULT 'pass'
    COMMENT 'pass/problem/risk/not_testable',
  risk_level VARCHAR(20) DEFAULT 'low'
    COMMENT 'low/medium/high/critical',
  testability VARCHAR(20) DEFAULT 'testable'
    COMMENT 'testable/unclear/not_testable',
  issues_json LONGTEXT NULL COMMENT '评审问题列表（兼容旧 MariaDB）',
  summary TEXT NULL,
  reviewer_name VARCHAR(100) DEFAULT '',
  reviewer_claw_id INT NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  UNIQUE KEY uq_req_review_verdict_item_key (requirement_item_id, review_key),
  KEY ix_req_review_verdict_iter (iteration_id),
  KEY ix_req_review_verdict_item (requirement_item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='需求逐条评审结论：需求评审岗的可交接产物';
