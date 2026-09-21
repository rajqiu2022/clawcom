-- Additive migration. Historical installed_at is retained but is NOT proof.
ALTER TABLE openclaw_skills
    ADD COLUMN IF NOT EXISTS assigned_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS installation_generation VARCHAR(32) NULL,
    ADD COLUMN IF NOT EXISTS installation_status VARCHAR(24) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS installation_verified_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS installation_receipt_json LONGTEXT NULL,
    ADD COLUMN IF NOT EXISTS installation_todo_id INT NULL;
UPDATE openclaw_skills SET installation_generation = REPLACE(UUID(), '-', '')
    WHERE installation_generation IS NULL;
-- Bind only the newest still-enabled historical installation todo.
-- Neither old chat/todo completion nor installed_at implies verified status.
UPDATE openclaw_skills a
JOIN skills s ON s.id = a.skill_id
JOIN (
    SELECT openclaw_id,
           SUBSTRING(verification_target, LOCATE(':', verification_target) + 1) AS skill_name,
           MAX(id) AS todo_id
    FROM claw_todos
    WHERE enabled = 1 AND (verification_target LIKE 'install-skill:%'
                           OR verification_target LIKE 'reinstall-skill:%')
    GROUP BY openclaw_id, SUBSTRING(verification_target, LOCATE(':', verification_target) + 1)
) t ON t.openclaw_id = a.openclaw_id AND t.skill_name = s.name
SET a.installation_todo_id = t.todo_id
WHERE a.enabled = 1 AND a.installation_todo_id IS NULL;
-- Retain old rows/logs for audit, but stop delivering superseded install jobs.
UPDATE claw_todos t
JOIN skills s ON t.verification_target IN (CONCAT('install-skill:', s.name),
                                         CONCAT('reinstall-skill:', s.name))
JOIN openclaw_skills a ON a.skill_id = s.id AND a.openclaw_id = t.openclaw_id
SET t.enabled = 0
WHERE t.enabled = 1 AND a.enabled = 1 AND a.installation_todo_id IS NOT NULL
      AND t.id < a.installation_todo_id;
CREATE TABLE IF NOT EXISTS skill_installation_receipts (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    assignment_id INT NOT NULL,
    generation VARCHAR(32) NOT NULL,
    event_id VARCHAR(96) NOT NULL,
    request_sha256 VARCHAR(64) NOT NULL,
    response_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_skill_install_receipt_event UNIQUE (assignment_id, generation, event_id),
    CONSTRAINT fk_skill_install_assignment FOREIGN KEY (assignment_id) REFERENCES openclaw_skills(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
