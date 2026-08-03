-- 用例评审改造 P0：课题可见性四档 + 定向授权表（MySQL / MariaDB）。
--
-- 兼容性说明：
--   1. `topics.visibility` 是 VARCHAR，新增取值 public_all / assigned 无需改列，
--      存量 public / project 数据语义不变，**不做数据迁移**。
--   2. 回滚策略：不删表。把课题 visibility 改回 public/project 即可恢复原行为。
--   3. MariaDB 老版本不支持 JSON 列，本次没有 JSON 字段，无需替换成 LONGTEXT。

CREATE TABLE IF NOT EXISTS topic_grants (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  topic_id INT NOT NULL COMMENT '课题 ID',
  grant_type VARCHAR(10) NOT NULL COMMENT 'user=指定用户, claw=指定 Agent',
  target_user_id INT NULL COMMENT 'grant_type=user 时的用户 ID',
  target_claw_id INT NULL COMMENT 'grant_type=claw 时的 Agent ID',
  target_name VARCHAR(100) NULL DEFAULT '' COMMENT '被授权对象显示名（冗余展示）',
  granted_by VARCHAR(100) NULL DEFAULT '' COMMENT '授权人 username/claw_name',
  granted_at DATETIME NULL,
  expires_at DATETIME NULL COMMENT '为空表示长期有效',
  UNIQUE KEY uq_topic_grant_target (topic_id, grant_type, target_user_id, target_claw_id),
  KEY ix_topic_grant_topic (topic_id),
  KEY ix_topic_grant_user (target_user_id),
  KEY ix_topic_grant_claw (target_claw_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='课题定向授权：visibility=assigned 的唯一鉴权依据';

-- visibility 列注释同步为四档语义（仅注释变更，不改类型与默认值，安全可重复执行）
ALTER TABLE topics
  MODIFY COLUMN visibility VARCHAR(20) DEFAULT 'public'
  COMMENT 'public_all=完全公开(免项目权限), public=Hub 用户, project=项目内, assigned=指定用户/Agent';

-- 评审脑图节点标记（镜像层，不回写 test_cases）
CREATE TABLE IF NOT EXISTS case_review_node_marks (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  topic_id INT NOT NULL COMMENT '所属评审课题 ID',
  node_type VARCHAR(10) NOT NULL COMMENT 'module=目录节点, case=用例节点',
  node_key VARCHAR(500) NOT NULL COMMENT 'case 时为 test_cases.id；module 时为 module_path',
  -- 唯一键用定长哈希而不是 node_key：utf8mb4 下 VARCHAR(500) 约 2000 字节，
  -- 老版本 MariaDB 索引前缀上限 767 字节会报 1071。
  node_key_hash VARCHAR(64) NOT NULL COMMENT 'sha256(node_type:node_key)，仅用于唯一索引',
  mark VARCHAR(20) NOT NULL COMMENT 'question=有问题, risk=风险待确认, flag=重点关注',
  note VARCHAR(500) NULL DEFAULT '' COMMENT '可选短备注',
  marked_by VARCHAR(100) NULL DEFAULT '' COMMENT '标记人 username/claw_name',
  marked_at DATETIME NULL,
  UNIQUE KEY uq_case_review_node_mark (topic_id, node_key_hash),
  KEY ix_case_review_mark_topic (topic_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='评审脑图节点标记：仅评审镜像层，不同步回用例库';

-- `scope_type`/`scope_module_path`/`scope_case_count` 在 test_case_library_reviews
-- 上早已存在（此前建表时就有，只是接口没接线），本次不需要新增列。
