-- 普通课题 Developer AI 临时协作：复用 collaboration_sessions。
-- 纯增量；存量用例评审、分析报告会话和课题回复保持不变。

ALTER TABLE collaboration_sessions
  MODIFY COLUMN project_id INT NULL;

ALTER TABLE topic_replies
  MODIFY COLUMN author_name VARCHAR(160) NOT NULL,
  ADD COLUMN IF NOT EXISTS collaboration_session_id INT NULL
    COMMENT '外部临时协作会话 ID；用于回复所有权' AFTER author_user_id,
  ADD COLUMN IF NOT EXISTS updated_at DATETIME NULL AFTER created_at;

UPDATE topic_replies
SET updated_at = created_at
WHERE updated_at IS NULL;

ALTER TABLE topic_replies
  ADD INDEX IF NOT EXISTS ix_topic_replies_collaboration_session
    (collaboration_session_id);
