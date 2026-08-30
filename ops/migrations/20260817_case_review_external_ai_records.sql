-- Developer AI 用例评审记录 CRUD：以临时协作会话作为不可伪造的所有权键。
-- 旧记录保持 collaboration_session_id=NULL，仅新增的外部 AI 评审记录可由其会话修改/删除。

ALTER TABLE case_review_comments
  ADD COLUMN IF NOT EXISTS collaboration_session_id INT NULL
    COMMENT 'Developer AI 临时协作会话 ID；用于评审记录所有权' AFTER author_user_id,
  ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'
    COMMENT 'active/deleted' AFTER is_edited,
  ADD COLUMN IF NOT EXISTS updated_at DATETIME NULL AFTER created_at,
  ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER updated_at;

UPDATE case_review_comments
SET status = 'active'
WHERE status IS NULL OR status = '';

ALTER TABLE case_review_comments
  ADD INDEX IF NOT EXISTS ix_crc_collaboration_session (collaboration_session_id),
  ADD INDEX IF NOT EXISTS ix_crc_status (status),
  ADD INDEX IF NOT EXISTS ix_crc_round_status (round_id, status);
