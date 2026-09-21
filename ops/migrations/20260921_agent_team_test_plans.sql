-- Explicit ownership only; never infer teams from names or assignees.
ALTER TABLE test_plans ADD COLUMN IF NOT EXISTS team_id INT NULL;
CREATE INDEX IF NOT EXISTS ix_test_plans_team_id ON test_plans(team_id);
SET @team_plan_fk_exists = (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'test_plans'
          AND CONSTRAINT_NAME = 'fk_test_plans_team');
SET @team_plan_fk_ddl = IF(@team_plan_fk_exists > 0, 'SELECT 1',
    'ALTER TABLE test_plans ADD CONSTRAINT fk_test_plans_team FOREIGN KEY (team_id) REFERENCES agent_teams(id)');
PREPARE team_plan_fk_statement FROM @team_plan_fk_ddl;
EXECUTE team_plan_fk_statement;
DEALLOCATE PREPARE team_plan_fk_statement;
-- Existing plans stay unbound until explicitly associated by a manager/admin.
