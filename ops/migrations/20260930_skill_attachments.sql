CREATE TABLE IF NOT EXISTS skill_attachments (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    skill_id INT NOT NULL,
    filename VARCHAR(180) NOT NULL,
    stored_name VARCHAR(64) NOT NULL,
    size_bytes INT NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    uploaded_by VARCHAR(100) NOT NULL,
    uploaded_at DATETIME NOT NULL,
    INDEX ix_skill_attachments_skill_id (skill_id),
    CONSTRAINT fk_skill_attachments_skill FOREIGN KEY (skill_id) REFERENCES skills(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
