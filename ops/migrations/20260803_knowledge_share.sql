-- 知识库收藏、匿名分享与 Markdown 导出
-- Markdown 为动态生成，无需持久化。

CREATE TABLE IF NOT EXISTS knowledge_favorites (
    id INT NOT NULL AUTO_INCREMENT,
    knowledge_id INT NOT NULL,
    user_id INT NULL,
    claw_id INT NULL,
    created_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_knowledge_fav_user (knowledge_id, user_id),
    UNIQUE KEY uq_knowledge_fav_claw (knowledge_id, claw_id),
    KEY ix_knowledge_favorites_knowledge_id (knowledge_id),
    KEY ix_knowledge_favorites_user_id (user_id),
    KEY ix_knowledge_favorites_claw_id (claw_id),
    CONSTRAINT fk_knowledge_favorites_knowledge
        FOREIGN KEY (knowledge_id) REFERENCES knowledge_entries (id),
    CONSTRAINT fk_knowledge_favorites_user
        FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_knowledge_favorites_claw
        FOREIGN KEY (claw_id) REFERENCES openclaw_instances (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE knowledge_entries
    ADD COLUMN IF NOT EXISTS is_shared TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE knowledge_entries
    ADD COLUMN IF NOT EXISTS share_token VARCHAR(64) NULL;

ALTER TABLE knowledge_entries
    ADD COLUMN IF NOT EXISTS shared_at DATETIME NULL;

ALTER TABLE knowledge_entries
    ADD UNIQUE INDEX IF NOT EXISTS uq_knowledge_share_token (share_token);
