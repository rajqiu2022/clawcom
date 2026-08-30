CREATE TABLE IF NOT EXISTS worker_releases (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    release_id VARCHAR(80) NOT NULL,
    source_repository VARCHAR(500) DEFAULT '',
    source_ref VARCHAR(120) DEFAULT '',
    source_commit VARCHAR(40) NOT NULL,
    committed_at VARCHAR(40) DEFAULT '',
    channel VARCHAR(30) DEFAULT 'candidate',
    signature_status VARCHAR(30) DEFAULT 'unsigned',
    release_manifest_sha256 VARCHAR(64) NOT NULL,
    platform VARCHAR(40) NOT NULL DEFAULT 'linux-x86_64',
    platform_manifest_sha256 VARCHAR(64) NOT NULL,
    package_manifest_sha256 VARCHAR(64) NOT NULL,
    artifact_filename VARCHAR(255) NOT NULL,
    artifact_sha256 VARCHAR(64) NOT NULL,
    artifact_size BIGINT NOT NULL,
    artifact_path VARCHAR(1000) NOT NULL,
    approval_status VARCHAR(20) DEFAULT 'candidate',
    is_default BOOLEAN NOT NULL DEFAULT 0,
    imported_by VARCHAR(100) DEFAULT 'system',
    imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    approved_by VARCHAR(100) DEFAULT '',
    approved_at DATETIME DEFAULT NULL,
    rejected_by VARCHAR(100) DEFAULT '',
    rejected_at DATETIME DEFAULT NULL,
    UNIQUE KEY uq_worker_release_platform (release_id, platform),
    INDEX ix_worker_release_approval (platform, approval_status, is_default)
);

ALTER TABLE agent_deployments
    ADD COLUMN IF NOT EXISTS worker_release_record_id INTEGER DEFAULT NULL;
ALTER TABLE agent_deployments
    ADD COLUMN IF NOT EXISTS worker_release_id VARCHAR(80) DEFAULT '';
ALTER TABLE agent_deployments
    ADD COLUMN IF NOT EXISTS worker_source_commit VARCHAR(40) DEFAULT '';
ALTER TABLE agent_deployments
    ADD COLUMN IF NOT EXISTS worker_artifact_sha256 VARCHAR(64) DEFAULT '';
