"""Signed authorization envelopes for managed Agent execution.

Hashes bind content; the Ed25519 signature proves Hub authorization.  The
private key is configuration-only and never returned to Sidecar/Job Service.
"""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from flask import current_app


class AuthorizationSigningError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str)


def _encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _decode(value):
    raw = str(value or '').strip().encode('ascii')
    raw += b'=' * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw)


def signing_key_id():
    return str(
        current_app.config.get('AGENT_CONTROL_SIGNING_KEY_ID')
        or os.getenv('AGENT_CONTROL_SIGNING_KEY_ID')
        or ('agent-control-test-key' if current_app.config.get('TESTING') else '')
    ).strip()


def _revoked_key_ids():
    value = current_app.config.get('AGENT_CONTROL_REVOKED_KEY_IDS')
    if value is None:
        value = os.getenv('AGENT_CONTROL_REVOKED_KEY_IDS', '')
    if isinstance(value, str):
        return {item.strip() for item in value.split(',') if item.strip()}
    return {str(item).strip() for item in (value or []) if str(item).strip()}


def _private_key():
    encoded = (
        current_app.config.get('AGENT_CONTROL_SIGNING_PRIVATE_KEY_B64')
        or os.getenv('AGENT_CONTROL_SIGNING_PRIVATE_KEY_B64'))
    if encoded:
        try:
            raw = _decode(encoded)
            if len(raw) != 32:
                raise ValueError('expected a 32-byte Ed25519 private seed')
            return Ed25519PrivateKey.from_private_bytes(raw)
        except Exception as exc:
            raise AuthorizationSigningError(
                'AUTHORIZATION_SIGNING_KEY_INVALID', str(exc))
    if current_app.config.get('TESTING'):
        secret = str(current_app.config.get('SECRET_KEY') or 'test-secret')
        seed = hashlib.sha256(
            ('agent-control-test-key:' + secret).encode('utf-8')).digest()
        return Ed25519PrivateKey.from_private_bytes(seed)
    raise AuthorizationSigningError(
        'AUTHORIZATION_SIGNING_UNAVAILABLE',
        'AGENT_CONTROL_SIGNING_PRIVATE_KEY_B64 is not configured')


def public_key_document():
    key_id = signing_key_id()
    if not key_id:
        raise AuthorizationSigningError(
            'AUTHORIZATION_SIGNING_UNAVAILABLE',
            'AGENT_CONTROL_SIGNING_KEY_ID is not configured')
    raw = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return {
        'key_id': key_id,
        'algorithm': 'Ed25519',
        'public_key': _encode(raw),
        'status': 'revoked' if key_id in _revoked_key_ids() else 'active',
    }


def sign_envelope(payload):
    key_id = signing_key_id()
    if not key_id or key_id in _revoked_key_ids():
        raise AuthorizationSigningError(
            'AUTHORIZATION_SIGNING_UNAVAILABLE',
            'signing key is missing or revoked')
    body = dict(payload, key_id=key_id, algorithm='Ed25519')
    signature = _private_key().sign(canonical_json(body).encode('utf-8'))
    return body, _encode(signature)


def verify_envelope(
    payload,
    signature,
    public_key=None,
    now=None,
    *,
    expected_issuer=None,
    expected_subject=None,
    expected_audience=None,
    expected_manifest_hash=None,
    expected_approval_scope_hash=None,
):
    """Verify signature, lifetime and caller-supplied Job Service bindings."""
    if not isinstance(payload, dict) or not signature:
        return False, 'authorization payload or signature is missing'
    key_id = str(payload.get('key_id') or '')
    if key_id in _revoked_key_ids():
        return False, 'authorization signing key is revoked'
    try:
        public_key = public_key or public_key_document()['public_key']
        Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
            _decode(signature), canonical_json(payload).encode('utf-8'))
    except Exception:
        return False, 'authorization signature is invalid'
    if payload.get('algorithm') != 'Ed25519':
        return False, 'authorization algorithm is invalid'
    if payload.get('schema_version') != 'authorization_envelope_v1':
        return False, 'authorization schema_version is invalid'
    expected_fields = {
        'issuer': expected_issuer,
        'subject': expected_subject,
        'audience': expected_audience,
        'manifest_hash': expected_manifest_hash,
        'approval_scope_hash': expected_approval_scope_hash,
    }
    for field, expected in expected_fields.items():
        if expected is not None and payload.get(field) != expected:
            return False, f'authorization {field} does not match'
    if not str(payload.get('nonce') or '').strip():
        return False, 'authorization nonce is missing'
    try:
        expires_at = datetime.fromisoformat(str(payload.get('expires_at') or ''))
    except ValueError:
        return False, 'authorization expires_at is invalid'
    if expires_at.tzinfo is None:
        return False, 'authorization expires_at must include timezone'
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires_at <= current:
        return False, 'authorization is expired'
    return True, ''
