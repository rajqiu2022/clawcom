-- Hub 主题聊天室与外部临时成员协作（纯增量、可由 CHAT_ROOM_ENABLED 灰度）
-- MariaDB / MySQL 5.7+

CREATE TABLE IF NOT EXISTS chat_rooms (
  id INT NOT NULL AUTO_INCREMENT,
  project_id INT NULL,
  title VARCHAR(160) NOT NULL,
  description TEXT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  agent_policy VARCHAR(30) NOT NULL DEFAULT 'mention_only',
  created_by_type VARCHAR(20) NOT NULL,
  created_by_id INT NOT NULL,
  owner_member_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  deleted_at DATETIME NULL,
  PRIMARY KEY (id),
  KEY ix_chat_rooms_project_id (project_id),
  KEY ix_chat_rooms_status (status),
  KEY ix_chat_rooms_owner_member_id (owner_member_id),
  CONSTRAINT fk_chat_rooms_project FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_guest_identities (
  id INT NOT NULL AUTO_INCREMENT,
  display_name VARCHAR(80) NOT NULL,
  resume_secret_hash VARCHAR(64) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_members (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  member_type VARCHAR(20) NOT NULL,
  user_id INT NULL,
  claw_id INT NULL,
  guest_identity_id INT NULL,
  display_name VARCHAR(80) NOT NULL,
  role VARCHAR(20) NOT NULL DEFAULT 'member',
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  last_read_message_id INT NULL,
  invitation_notified_at DATETIME NULL,
  last_event_id INT NULL,
  joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  removed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_room_member_user (room_id, user_id),
  UNIQUE KEY uq_chat_room_member_claw (room_id, claw_id),
  UNIQUE KEY uq_chat_room_member_guest (room_id, guest_identity_id),
  KEY ix_chat_room_member_room_status (room_id, status),
  KEY ix_chat_room_members_user_id (user_id),
  KEY ix_chat_room_members_claw_id (claw_id),
  KEY ix_chat_room_members_guest_identity_id (guest_identity_id),
  CONSTRAINT fk_chat_room_members_room FOREIGN KEY (room_id) REFERENCES chat_rooms (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_members_user FOREIGN KEY (user_id) REFERENCES users (id),
  CONSTRAINT fk_chat_room_members_claw FOREIGN KEY (claw_id) REFERENCES openclaw_instances (id),
  CONSTRAINT fk_chat_room_members_guest FOREIGN KEY (guest_identity_id) REFERENCES chat_room_guest_identities (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_messages (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  sender_member_id INT NOT NULL,
  client_message_id VARCHAR(100) NOT NULL,
  message_type VARCHAR(20) NOT NULL DEFAULT 'text',
  content TEXT NOT NULL,
  reply_to_message_id INT NULL,
  origin_delivery_id INT NULL,
  automation_depth INT NOT NULL DEFAULT 0,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  edited_at DATETIME NULL,
  deleted_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_room_message_idempotency (room_id, sender_member_id, client_message_id),
  KEY ix_chat_room_message_room_id (room_id, id),
  KEY ix_chat_room_messages_sender_member_id (sender_member_id),
  KEY ix_chat_room_messages_origin_delivery_id (origin_delivery_id),
  KEY ix_chat_room_messages_created_at (created_at),
  CONSTRAINT fk_chat_room_messages_room FOREIGN KEY (room_id) REFERENCES chat_rooms (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_messages_sender FOREIGN KEY (sender_member_id) REFERENCES chat_room_members (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_events (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  actor_member_id INT NULL,
  payload_json LONGTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_chat_room_events_room_id (room_id),
  KEY ix_chat_room_events_event_type (event_type),
  CONSTRAINT fk_chat_room_events_room FOREIGN KEY (room_id) REFERENCES chat_rooms (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_mentions (
  id INT NOT NULL AUTO_INCREMENT,
  message_id INT NOT NULL,
  mention_type VARCHAR(20) NOT NULL,
  member_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_chat_room_mentions_message_id (message_id),
  KEY ix_chat_room_mentions_member_id (member_id),
  CONSTRAINT fk_chat_room_mentions_message FOREIGN KEY (message_id) REFERENCES chat_room_messages (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_mentions_member FOREIGN KEY (member_id) REFERENCES chat_room_members (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_deliveries (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  message_id INT NOT NULL,
  member_id INT NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'unread',
  notify_agent TINYINT(1) NOT NULL DEFAULT 0,
  delivered_at DATETIME NULL,
  read_at DATETIME NULL,
  processing_at DATETIME NULL,
  done_at DATETIME NULL,
  failed_reason TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_room_delivery_member (message_id, member_id),
  KEY ix_chat_room_delivery_member_status (member_id, status),
  KEY ix_chat_room_deliveries_room_id (room_id),
  KEY ix_chat_room_deliveries_notify_agent (notify_agent),
  CONSTRAINT fk_chat_room_deliveries_room FOREIGN KEY (room_id) REFERENCES chat_rooms (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_deliveries_message FOREIGN KEY (message_id) REFERENCES chat_room_messages (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_deliveries_member FOREIGN KEY (member_id) REFERENCES chat_room_members (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_invites (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  token_hash VARCHAR(64) NOT NULL,
  label VARCHAR(100) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  max_joins INT NULL,
  join_count INT NOT NULL DEFAULT 0,
  expires_at DATETIME NULL,
  created_by_member_id INT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_room_invites_token_hash (token_hash),
  KEY ix_chat_room_invites_room_id (room_id),
  CONSTRAINT fk_chat_room_invites_room FOREIGN KEY (room_id) REFERENCES chat_rooms (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_invites_creator FOREIGN KEY (created_by_member_id) REFERENCES chat_room_members (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_guest_sessions (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  member_id INT NOT NULL,
  access_token_hash VARCHAR(64) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  expires_at DATETIME NOT NULL,
  last_activity_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_room_guest_sessions_token_hash (access_token_hash),
  KEY ix_chat_room_guest_sessions_room_id (room_id),
  KEY ix_chat_room_guest_sessions_member_id (member_id),
  KEY ix_chat_room_guest_sessions_status (status),
  KEY ix_chat_room_guest_sessions_expires_at (expires_at),
  CONSTRAINT fk_chat_room_guest_sessions_room FOREIGN KEY (room_id) REFERENCES chat_rooms (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_guest_sessions_member FOREIGN KEY (member_id) REFERENCES chat_room_members (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_room_audits (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NULL,
  actor_member_id INT NULL,
  action VARCHAR(50) NOT NULL,
  target_type VARCHAR(30) NULL,
  target_id INT NULL,
  detail_json LONGTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_chat_room_audits_room_id (room_id),
  KEY ix_chat_room_audits_action (action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
