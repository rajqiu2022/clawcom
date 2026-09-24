-- TestTask execution references: canonical IDs on the template and immutable
-- copies on each scheduled occurrence.  Full content remains in canonical
-- Skill/knowledge/report stores and is pulled on demand under normal ACLs.
ALTER TABLE test_tasks
    ADD COLUMN reference_skill_ids_json LONGTEXT DEFAULT NULL,
    ADD COLUMN reference_knowledge_ids_json LONGTEXT DEFAULT NULL,
    ADD COLUMN reference_report_ids_json LONGTEXT DEFAULT NULL;

ALTER TABLE test_task_occurrences
    ADD COLUMN reference_skill_ids_json LONGTEXT DEFAULT NULL,
    ADD COLUMN reference_knowledge_ids_json LONGTEXT DEFAULT NULL,
    ADD COLUMN reference_report_ids_json LONGTEXT DEFAULT NULL;
