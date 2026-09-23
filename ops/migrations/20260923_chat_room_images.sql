-- Private image attachments for human and Agent team-room messages.
-- MariaDB / MySQL 5.7+, additive and safe for existing rooms/messages.

CREATE TABLE IF NOT EXISTS chat_room_images (
  id INT NOT NULL AUTO_INCREMENT,
  room_id INT NOT NULL,
  message_id INT NULL,
  uploaded_by_member_id INT NOT NULL,
  client_upload_id VARCHAR(100) NULL,
  original_name VARCHAR(255) NOT NULL,
  content_type VARCHAR(50) NOT NULL,
  file_size INT NOT NULL,
  sha256 VARCHAR(64) NOT NULL,
  storage_key VARCHAR(255) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  attached_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_room_image_storage_key (storage_key),
  UNIQUE KEY uq_chat_room_image_idempotency (room_id, uploaded_by_member_id, client_upload_id),
  KEY ix_chat_room_images_room_id (room_id),
  KEY ix_chat_room_images_message_id (message_id),
  KEY ix_chat_room_images_uploader (uploaded_by_member_id),
  KEY ix_chat_room_images_status (status),
  CONSTRAINT fk_chat_room_images_room FOREIGN KEY (room_id)
    REFERENCES chat_rooms (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_images_message FOREIGN KEY (message_id)
    REFERENCES chat_room_messages (id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_room_images_uploader FOREIGN KEY (uploaded_by_member_id)
    REFERENCES chat_room_members (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
