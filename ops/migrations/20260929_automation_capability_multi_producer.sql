-- Run before deploying the multi-Worker capability publication API.
-- Existing rows retain their producer_claw_id and event history.
-- MariaDB 10.1 has innodb_large_prefix=OFF (767-byte composite key limit).
-- The API accepts at most 189 characters, so this prefix is the full
-- application-level capability key: 4 + 189*4 + 4 = 764 bytes.
ALTER TABLE automation_capabilities
    DROP INDEX uq_automation_capability_project_key,
    ADD UNIQUE KEY uq_automation_capability_project_key_producer
        (project_id, capability_key(189), producer_claw_id);
