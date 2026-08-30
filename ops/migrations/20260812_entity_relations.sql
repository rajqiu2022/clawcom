-- P0-F: project-scoped generic entity lineage and bidirectional traceability.

CREATE TABLE IF NOT EXISTS entity_relations (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NOT NULL,
  from_type VARCHAR(64) NOT NULL,
  from_id VARCHAR(500) NOT NULL,
  relation_type VARCHAR(64) NOT NULL,
  to_type VARCHAR(64) NOT NULL,
  to_id VARCHAR(500) NOT NULL,
  relation_key VARCHAR(71) NOT NULL,
  metadata_json JSON NULL,
  created_by VARCHAR(160) DEFAULT 'system',
  updated_by VARCHAR(160) DEFAULT 'system',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_entity_relations_relation_key (relation_key),
  KEY ix_entity_relations_project (project_id),
  KEY ix_entity_relations_from_type (from_type),
  KEY ix_entity_relations_from_id (from_id(191)),
  KEY ix_entity_relations_relation_type (relation_type),
  KEY ix_entity_relations_to_type (to_type),
  KEY ix_entity_relations_to_id (to_id(191)),
  KEY ix_entity_relations_created (created_at),
  KEY ix_entity_relations_updated (updated_at),
  KEY ix_entity_relation_from (project_id, from_type, from_id(191)),
  KEY ix_entity_relation_to (project_id, to_type, to_id(191)),
  KEY ix_entity_relation_project_updated (project_id, updated_at),
  CONSTRAINT fk_entity_relation_project
    FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- relation_key is SHA-256(project_id + from + relation_type + to). It provides
-- race-safe natural-key idempotency without an oversized five-column utf8mb4
-- UNIQUE index. Metadata is mutable; endpoint identity and created_at are not.
