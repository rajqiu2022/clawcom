-- External mutations are intentionally independent from destructive local
-- actions. Existing Missions remain deny-by-default.
ALTER TABLE workflow_missions
    ADD COLUMN allow_external_mutations TINYINT(1) NOT NULL DEFAULT 0
    AFTER allow_destructive_actions;
