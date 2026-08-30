ALTER TABLE claw_sidecar_configs
    ADD COLUMN IF NOT EXISTS runtime_config_json LONGTEXT DEFAULT NULL
    COMMENT '无密钥的运行时身份：kind/provider/mode/platform/release 等';

ALTER TABLE claw_sidecar_configs
    ADD COLUMN IF NOT EXISTS config_owner VARCHAR(20) NOT NULL DEFAULT 'hub'
    COMMENT '运行配置归属：hub / worker';

ALTER TABLE claw_sidecar_configs
    ADD COLUMN IF NOT EXISTS runtime_reported_at DATETIME DEFAULT NULL
    COMMENT 'Worker 最近一次上报结构化运行时身份的时间';
