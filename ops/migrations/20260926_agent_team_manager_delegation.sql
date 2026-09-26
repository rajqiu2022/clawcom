-- 测试经理临时转正/回退：被委派人、原主经理与新备经理的可逆凭据。
-- MariaDB 10.1 无原生 JSON 类型，用 LONGTEXT（SQLAlchemy JSON 序列化兼容）。
ALTER TABLE agent_teams
    ADD COLUMN manager_delegation_json LONGTEXT DEFAULT NULL;
