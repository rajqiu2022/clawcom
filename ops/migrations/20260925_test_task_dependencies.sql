-- Explicit same-plan TestTask dependencies for deterministic team dispatch.
ALTER TABLE test_tasks
    ADD COLUMN depends_on_task_ids_json LONGTEXT DEFAULT NULL;
