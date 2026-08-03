"""SQLAlchemy models for Hub-canonical shared memory."""

from datetime import timedelta
import uuid

from app import db
from app.models import _now


def new_uuid():
    return str(uuid.uuid4())


class MemoryScopeRef(db.Model):
    __tablename__ = "memory_scope_refs"

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    scope = db.Column(db.String(16), nullable=False)
    owner_username = db.Column(db.String(50))
    project_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("scope", "owner_username", name="uq_memory_scope_owner"),
        db.UniqueConstraint("scope", "project_id", name="uq_memory_scope_project"),
        db.Index("ix_memory_scope_lookup", "scope", "owner_username", "project_id"),
    )


class MemoryRecord(db.Model):
    __tablename__ = "memory_records"

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    tenant_id = db.Column(db.Integer, nullable=False, default=1)
    scope = db.Column(db.String(16), nullable=False)
    scope_ref = db.Column(db.String(36), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    memory_key = db.Column(db.String(128), nullable=False)
    value = db.Column(db.String(4000))
    confidence = db.Column(db.Numeric(4, 3))
    record_version = db.Column(db.BigInteger, nullable=False, default=1)
    tombstone = db.Column(db.Boolean, nullable=False, default=False)
    disabled = db.Column(db.Boolean, nullable=False, default=False)
    source_kind = db.Column(db.String(24), nullable=False)
    source_id = db.Column(db.String(256), nullable=False)
    created_by_claw_id = db.Column(db.Integer, nullable=False)
    updated_by_claw_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)
    deleted_at = db.Column(db.DateTime)
    purge_after = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id", "scope", "scope_ref", "kind", "memory_key",
            name="uq_memory_record_business_key",
        ),
        db.Index(
            "ix_memory_records_delta_read",
            "tenant_id", "scope", "scope_ref", "tombstone", "updated_at", "id",
        ),
    )

    def to_protocol_record(self, sequence=None):
        record = {
            "id": self.id,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "kind": self.kind,
            "key": self.memory_key,
            "value": None if self.tombstone else self.value,
            "confidence": None if self.tombstone else float(self.confidence),
            "version": int(self.record_version),
            "tombstone": bool(self.tombstone),
            "disabled": bool(self.disabled),
            "source": {"kind": self.source_kind, "id": self.source_id},
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }
        if sequence is not None:
            record["sequence"] = int(sequence)
        return record

    def mark_deleted(self):
        self.tombstone = True
        self.value = None
        self.confidence = None
        self.deleted_at = _now()
        self.purge_after = self.deleted_at + timedelta(days=30)


class MemoryMutationRequest(db.Model):
    __tablename__ = "memory_mutation_requests"

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    tenant_id = db.Column(db.Integer, nullable=False, default=1)
    claw_id = db.Column(db.Integer, nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    http_status = db.Column(db.SmallInteger)
    response_body = db.Column(db.JSON)
    state = db.Column(db.String(12), nullable=False, default="processing")
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    completed_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id", "claw_id", "idempotency_key",
            name="uq_memory_request_idempotency",
        ),
    )


class MemoryMutation(db.Model):
    __tablename__ = "memory_mutations"

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    tenant_id = db.Column(db.Integer, nullable=False, default=1)
    claw_id = db.Column(db.Integer, nullable=False)
    request_id = db.Column(db.String(36), nullable=False)
    mutation_id = db.Column(db.String(36), nullable=False)
    source_id = db.Column(db.String(256), nullable=False)
    source_kind = db.Column(db.String(24), nullable=False)
    record_id = db.Column(db.String(36))
    result = db.Column(db.String(12), nullable=False)
    error_code = db.Column(db.String(48))
    base_version = db.Column(db.BigInteger)
    result_version = db.Column(db.BigInteger)
    response_item = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id", "claw_id", "mutation_id",
            name="uq_memory_mutation_once",
        ),
        db.Index("ix_memory_mutation_request", "request_id"),
    )


class MemoryChangeLog(db.Model):
    __tablename__ = "memory_change_log"

    sequence = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1)
    scope = db.Column(db.String(16), nullable=False)
    scope_ref = db.Column(db.String(36), nullable=False)
    record_id = db.Column(db.String(36), nullable=False)
    record_version = db.Column(db.BigInteger, nullable=False)
    change_type = db.Column(db.String(12), nullable=False)
    changed_at = db.Column(db.DateTime, default=_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            "record_id", "record_version",
            name="uq_memory_change_record_version",
        ),
        db.Index("ix_memory_change_delta", "tenant_id", "scope", "scope_ref", "sequence"),
    )
