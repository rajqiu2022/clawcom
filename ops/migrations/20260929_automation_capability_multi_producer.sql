-- Run before deploying the multi-Worker capability publication API.
-- Existing rows retain their producer_claw_id and event history.
ALTER TABLE automation_capabilities
    DROP INDEX uq_automation_capability_project_key,
    ADD UNIQUE KEY uq_automation_capability_project_key_producer
        (project_id, capability_key, producer_claw_id);
