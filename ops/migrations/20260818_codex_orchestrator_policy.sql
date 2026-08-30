-- Per-Worker trusted context policy for Codex multi-Flow orchestration.
-- Raw policy is persisted in Hub, validated on write, intersected with the
-- Workflow execute ACL on read, and never sent outside system_context.policy.
ALTER TABLE claw_sidecar_configs
    ADD COLUMN system_context_policy_json LONGTEXT NULL
    COMMENT 'Hub-managed trusted context policy; raw value is not downlinked';
