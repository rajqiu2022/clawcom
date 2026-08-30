CREATE TABLE IF NOT EXISTS skill_usage_events (
    id INT NOT NULL AUTO_INCREMENT,
    skill_id INT NOT NULL,
    access_type VARCHAR(32) NOT NULL,
    accessed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_skill_usage_events_accessed_at (accessed_at),
    KEY ix_skill_usage_recent (skill_id, accessed_at),
    CONSTRAINT fk_skill_usage_events_skill
        FOREIGN KEY (skill_id) REFERENCES skills (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
