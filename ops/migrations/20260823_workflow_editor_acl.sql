ALTER TABLE workflow_definitions
    ADD COLUMN IF NOT EXISTS editor_acl_json LONGTEXT DEFAULT NULL;
