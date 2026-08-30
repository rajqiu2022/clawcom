CREATE TABLE IF NOT EXISTS automation_capabilities (
    id INT NOT NULL AUTO_INCREMENT,
    project_id INT NOT NULL,
    capability_key VARCHAR(255) NOT NULL,
    name VARCHAR(300) NOT NULL,
    operations_json JSON NULL,
    observables_json JSON NULL,
    reset_hooks_json JSON NULL,
    platforms_json JSON NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'available',
    implementation_version VARCHAR(80) NOT NULL DEFAULT '',
    health_checked_at DATETIME NULL,
    version INT NOT NULL DEFAULT 1,
    created_by VARCHAR(160) NOT NULL DEFAULT 'system',
    updated_by VARCHAR(160) NOT NULL DEFAULT 'system',
    created_at DATETIME NULL,
    updated_at DATETIME NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_automation_capability_project
        FOREIGN KEY (project_id) REFERENCES projects (id),
    UNIQUE KEY uq_automation_capability_project_key
        (project_id, capability_key),
    KEY ix_automation_capability_project_status (project_id, status),
    KEY ix_automation_capabilities_project_id (project_id),
    KEY ix_automation_capabilities_status (status),
    KEY ix_automation_capabilities_health_checked_at (health_checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
