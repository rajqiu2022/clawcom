"""Ephemeral loopback proxy that lets Pi call Hub without seeing CLAW_TOKEN."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH"}
DENIED_HEADERS = {
    "authorization",
    "content-length",
    "host",
    "idempotency-key",
}
DEFAULT_REQUEST_BYTES = 256 * 1024
DEFAULT_RESPONSE_BYTES = 512 * 1024
DEFAULT_RAW_RESPONSE_BYTES = 9 * 1024 * 1024
_SKILL_DOWNLOAD_PATH = re.compile(
    r"^/api/v1/openclaws/[1-9][0-9]*/skills/[1-9][0-9]*/"
    r"(?:files/.+|pack)"
)
_RAW_RESPONSE_HEADERS = (
    "etag",
    "x-skill-content-version",
)
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WORKFLOW_RUN_CREATE_PATH = "/api/v1/workflow-runs"
_WORKFLOW_RUN_CREATE_ALLOWED_FIELDS = frozenset({
    "controller_run_id",
    "correlation_id",
    "definition_id",
    "run_name",
    "trigger_source",
})
_WRITE_VERIFY_POLICIES = (
    (
        re.compile(r"^/api/v1/workflow-runs$"),
        "read",
        "/api/v1/workflow-runs/{resource_id}",
    ),
    (
        re.compile(
            r"^/api/v1/workflow-runs/(?P<id>[1-9][0-9]*)"
            r"(?:/steps/(?P<step_id>[^/?]+)(?:/[^?]*)?|/[^?]*)?$"
        ),
        "read",
        "/api/v1/workflow-runs/{resource_id}",
    ),
    (
        re.compile(r"^/api/v1/topics(?:/(?P<id>[1-9][0-9]*))?$"),
        "read",
        "/api/v1/topics/{resource_id}",
    ),
)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_workflow_create_definition_ids(value):
    """Return a canonical, fail-closed workflow creation allowlist."""

    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(
            "workflow create definition ids must be a collection"
        )
    normalized = set()
    for item in value:
        if type(item) is not int or item <= 0:
            raise ValueError(
                "workflow create definition ids must be positive integers"
            )
        normalized.add(item)
    return tuple(sorted(normalized))


def _redact_value(value, redactor):
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, list):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redact_value(item, redactor)
            for key, item in value.items()
        }
    return value


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        error = getattr(__import__("sys"), "exc_info")()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, OSError)):
            return
        super().handle_error(request, client_address)


class EphemeralHubProxy:
    def __init__(
        self,
        http_func,
        *,
        redactor=lambda text: text,
        max_request_bytes=DEFAULT_REQUEST_BYTES,
        max_response_bytes=DEFAULT_RESPONSE_BYTES,
        max_calls=64,
        timeout=30,
        raw_http_func=None,
        max_raw_response_bytes=DEFAULT_RAW_RESPONSE_BYTES,
        workflow_create_definition_ids=(),
        workflow_run_receipt_callback: Callable[[Mapping], bool] | None = None,
        wecom_reply_callback: Callable[[Mapping], Mapping | bool] | None = None,
        allow_writes: bool = True,
        approved_write_action: str | None = None,
    ):
        self._http = http_func
        self._redactor = redactor
        self._max_request_bytes = int(max_request_bytes)
        self._max_response_bytes = int(max_response_bytes)
        self._max_calls = int(max_calls)
        self._timeout = timeout
        self._raw_http = raw_http_func
        self._max_raw_response_bytes = int(max_raw_response_bytes)
        self._workflow_create_definition_ids = frozenset(
            normalize_workflow_create_definition_ids(
                workflow_create_definition_ids
            )
        )
        self._workflow_run_receipt_callback = workflow_run_receipt_callback
        self._wecom_reply_callback = wecom_reply_callback
        self._allow_writes = bool(allow_writes)
        self._approved_write_action = self._normalize_approved_write_action(
            approved_write_action
        )
        self.token = secrets.token_urlsafe(32)
        self.url = ""
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._calls = 0
        self._server = None
        self._thread = None

    def start(self):
        if self._server is not None:
            raise RuntimeError("Hub proxy is already running")
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format_string, *args):
                return

            def do_POST(self):
                if self.path not in ("/hub-api", "/wecom-reply"):
                    self._error_response(404, "not_found")
                    return
                if not self._authorized():
                    self._error_response(401, "unauthorized")
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0 or length > proxy._max_request_bytes:
                    self._error_response(400, "invalid_request_size")
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = (
                        proxy._handle(payload)
                        if self.path == "/hub-api"
                        else proxy._handle_wecom_reply(payload)
                    )
                    self._json_response(200, result)
                except ValueError as exc:
                    self._error_response(400, str(exc))
                except Exception as exc:
                    self._json_response(
                        200,
                        {
                            "status": 0,
                            "error_origin": "worker_hub_proxy",
                            "body": {
                                "error": proxy._redactor(
                                    f"{type(exc).__name__}: {exc}"
                                )[:500],
                            },
                        },
                    )

            def _authorized(self):
                scheme, _, credential = (
                    self.headers.get("Authorization", "").partition(" ")
                )
                return (
                    scheme.lower() == "bearer"
                    and hmac.compare_digest(credential, proxy.token)
                )

            def _json_response(self, status, body):
                data = _json_bytes(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _error_response(self, status, error):
                self._json_response(
                    status,
                    {
                        "status": int(status),
                        "error_origin": "worker_hub_proxy",
                        "body": {"error": proxy._redactor(str(error))[:500]},
                    },
                )

        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="hub-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def environment(self):
        if not self.url:
            raise RuntimeError("Hub proxy is not running")
        environment = {
            "HUB_PROXY_URL": self.url,
            "HUB_PROXY_TOKEN": self.token,
        }
        if self._workflow_create_definition_ids:
            environment["HUB_WORKFLOW_CREATE_DEFINITION_IDS"] = ",".join(
                str(item)
                for item in sorted(self._workflow_create_definition_ids)
            )
        if self._wecom_reply_callback is not None:
            environment["CLAW_WECOM_REPLY_ENABLED"] = "1"
        return environment

    def close(self):
        server = self._server
        if server is None:
            return
        self._server = None
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _handle(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("hub_api payload must be an object")
        self._claim_call("hub_api")

        method = str(payload.get("method", "GET")).upper()
        if method not in ALLOWED_METHODS:
            raise ValueError("hub_api method is not allowed")
        path = self._validate_path(payload.get("path"))
        query = payload.get("query")
        if query is not None and not isinstance(query, dict):
            raise ValueError("hub_api query must be an object")
        body = payload.get("body")
        headers = self._validate_headers(payload.get("headers") or {})
        response_mode = payload.get("response_mode")
        if response_mode is not None:
            if response_mode != "raw":
                raise ValueError("hub_api response mode is not allowed")
            return self._handle_raw(method, path, query, headers)
        verification = None
        operation_id = payload.get("operation_id")
        if method != "GET":
            if not self._allow_writes:
                raise ValueError("hub_api writes are disabled for this invocation")
            path_only = urlsplit(path).path
            policy_known = any(
                pattern.fullmatch(path_only) is not None
                for pattern, _kind, _target in _WRITE_VERIFY_POLICIES
            )
            approved_action = (method, path_only) == self._approved_write_action
            if policy_known or not approved_action:
                verification = self._validate_verification(
                    payload.get("verify"),
                    body,
                    path,
                    method,
                    query,
                    headers,
                )
            if not _OPERATION_ID.fullmatch(str(operation_id or "")):
                raise ValueError("hub_api operation_id is required for writes")
            headers["Idempotency-Key"] = self._idempotency_key(
                method,
                path,
                operation_id,
                query=query,
                body=body,
            )

        lock = self._write_lock if method != "GET" else None
        if lock is not None:
            lock.acquire()
        try:
            status, response_body = self._http(
                method,
                path,
                body=body,
                timeout=self._timeout,
                query=query,
                headers=headers,
            )
            compact_workflow_receipt = (
                verification is not None
                and verification.get("compact_workflow_receipt") is True
                and 200 <= int(status) < 300
            )
            write_response = {
                "status": int(status),
                "body": (
                    {}
                    if compact_workflow_receipt
                    else _redact_value(response_body, self._redactor)
                ),
            }
            response = write_response
            if verification is not None and 200 <= int(status) < 300:
                response = self._verify_write(
                    write_response,
                    response_body,
                    verification,
                    operation_id,
                )
        finally:
            if lock is not None:
                lock.release()
        encoded = _json_bytes(response)
        if len(encoded) > self._max_response_bytes:
            response = {
                "status": int(status),
                "body": {
                    "error": "hub_api response exceeds limit",
                    "truncated": True,
                    "bytes": len(encoded),
                },
            }
        return response

    def _claim_call(self, name):
        with self._lock:
            if self._calls >= self._max_calls:
                raise ValueError(f"{name} call limit exceeded")
            self._calls += 1

    def _handle_wecom_reply(self, payload):
        if self._wecom_reply_callback is None:
            raise ValueError("wecom_reply is not enabled for this invocation")
        if not isinstance(payload, dict) or set(payload) != {
            "operation_id", "message"
        }:
            raise ValueError("wecom_reply payload is invalid")
        self._claim_call("wecom_reply")
        operation_id = str(payload.get("operation_id") or "")
        message = str(payload.get("message") or "").strip()
        if not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("wecom_reply operation_id is invalid")
        if (
            not message
            or len(message.encode("utf-8", "replace")) > 4000
            or "\x00" in message
        ):
            raise ValueError("wecom_reply message is invalid")
        result = self._wecom_reply_callback({
            "operation_id": operation_id,
            "message": self._redactor(message),
        })
        if result is False or result is None:
            raise ValueError("wecom_reply was rejected by Worker")
        body = dict(result) if isinstance(result, Mapping) else {"queued": True}
        body.setdefault("queued", True)
        body["operation_id"] = operation_id
        return {"status": 202, "body": body}

    @staticmethod
    def _normalize_approved_write_action(value):
        rendered = str(value or "").strip()
        if not rendered:
            return None
        method, separator, path = rendered.partition(" ")
        if (
            not separator
            or method not in {"POST", "PUT", "PATCH"}
            or re.fullmatch(
                r"/api/[A-Za-z0-9._~!$&'()*+,;=:@/-]+", path
            ) is None
            or "?" in path
            or "#" in path
            or "//" in path
            or ".." in path.split("/")
        ):
            raise ValueError("approved Hub write action is invalid")
        return method, path

    def _validate_verification(
        self,
        value,
        write_body,
        write_path,
        write_method,
        write_query,
        write_headers,
    ):
        if not isinstance(value, dict):
            raise ValueError("hub_api write verification is required")
        expected = value.get("expected")
        if (
            not isinstance(expected, dict)
            or not expected
            or not isinstance(write_body, dict)
        ):
            raise ValueError("hub_api write verification is invalid")
        for key, expected_value in expected.items():
            if key not in write_body or write_body.get(key) != expected_value:
                raise ValueError(
                    "hub_api verification expected must be a write body subset"
                )
        policy_match = None
        policy_kind = None
        policy_target = None
        path_only = urlsplit(write_path).path
        if path_only == _WORKFLOW_RUN_CREATE_PATH:
            self._validate_workflow_run_create(
                write_method,
                write_body,
                expected,
                write_query,
                write_headers,
                approved=(
                    (write_method, path_only) == self._approved_write_action
                ),
            )
        for pattern, kind, target in _WRITE_VERIFY_POLICIES:
            match = pattern.fullmatch(path_only)
            if match is not None:
                policy_match = match
                policy_kind = kind
                policy_target = target
                break
        if policy_match is None:
            raise ValueError("hub_api write path has no verification policy")
        path_resource_id = policy_match.groupdict().get("id")
        trusted_resource_id = (
            int(path_resource_id) if path_resource_id is not None else None
        )
        resource_id = value.get("resource_id")
        field = value.get("resource_id_field")
        if resource_id is not None and (
            isinstance(resource_id, bool)
            or not isinstance(resource_id, int)
            or resource_id <= 0
        ):
            raise ValueError("hub_api verification resource_id is invalid")
        if field is not None and (
            not isinstance(field, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", field)
        ):
            raise ValueError(
                "hub_api verification resource_id_field is invalid"
            )
        read_path = value.get("read_path")
        resource_type = value.get("resource_type")
        if policy_kind == "read":
            if read_path != policy_target or resource_type is not None:
                raise ValueError("hub_api verification route is not allowed")
            self._validate_path(
                policy_target.replace(
                    "{resource_id}",
                    str(trusted_resource_id or 1),
                )
            )
            if resource_id is not None and resource_id != trusted_resource_id:
                raise ValueError("hub_api verification resource_id is not bound")
            resource_id = trusted_resource_id
            field = "id" if trusted_resource_id is None else None
        else:
            if (
                resource_type != policy_target
                or read_path is not None
                or resource_id is not None
            ):
                raise ValueError("hub_api verification route is not allowed")
            field = "id"
        return {
            "expected": dict(expected),
            "resource_id": resource_id,
            "resource_id_field": field,
            "read_path": policy_target if policy_kind == "read" else None,
            "resource_type": policy_target if policy_kind == "ops" else None,
            "step_id": (
                policy_match.groupdict().get("step_id")
                if policy_match is not None
                else None
            ),
            "compact_workflow_receipt": path_only == _WORKFLOW_RUN_CREATE_PATH,
        }

    def _validate_workflow_run_create(
        self,
        write_method,
        write_body,
        expected,
        write_query,
        write_headers,
        approved=False,
    ):
        """Authorize only explicitly granted definitions, without overrides."""

        if write_method != "POST":
            raise ValueError(
                "hub_api workflow run creation requires POST"
            )
        if write_query not in (None, {}):
            raise ValueError(
                "hub_api workflow run creation does not allow query overrides"
            )
        if write_headers:
            raise ValueError(
                "hub_api workflow run creation does not allow caller headers"
            )
        definition_id = write_body.get("definition_id")
        expected_definition_id = expected.get("definition_id")
        if (
            type(definition_id) is not int
            or type(expected_definition_id) is not int
            or definition_id != expected_definition_id
            or (
                definition_id not in self._workflow_create_definition_ids
                and not approved
            )
        ):
            raise ValueError(
                "hub_api workflow run creation is not authorized for this definition"
            )
        unsupported = sorted(
            set(write_body).difference(_WORKFLOW_RUN_CREATE_ALLOWED_FIELDS)
        )
        if unsupported:
            raise ValueError(
                "hub_api workflow run creation does not allow routing, executor, "
                f"variable, or scheduling overrides: {','.join(unsupported)}"
            )

    def _verify_write(
        self,
        write_response,
        raw_write_body,
        verification,
        operation_id,
    ):
        compact_workflow_receipt = (
            verification.get("compact_workflow_receipt") is True
        )
        resource_id = verification["resource_id"]
        if resource_id is None and verification["resource_id_field"]:
            if isinstance(raw_write_body, dict):
                resource_id = raw_write_body.get(
                    verification["resource_id_field"]
                )
        if (
            isinstance(resource_id, bool)
            or not isinstance(resource_id, int)
            or resource_id <= 0
        ):
            if compact_workflow_receipt:
                return {
                    "status": 409,
                    "body": {
                        "error": "verification_resource_id_missing",
                    },
                }
            return {
                "status": 409,
                "body": {
                    "verified": False,
                    "error": "verification_resource_id_missing",
                    "write": write_response,
                },
            }

        verify_view = None
        if verification["read_path"]:
            verify_path = verification["read_path"].replace(
                "{resource_id}",
                str(resource_id),
            )
            verify_path = self._validate_path(verify_path)
            verify_status, verify_body = self._http(
                "GET",
                verify_path,
                body=None,
                timeout=self._timeout,
                query=None,
                headers={},
            )
            verify_view = self._verification_view(verify_body, verification)
            verified = (
                200 <= int(verify_status) < 300
                and isinstance(verify_view, dict)
                and all(
                    str(verify_view.get(key, "")) == str(expected)
                    for key, expected in verification["expected"].items()
                )
            )
        else:
            verify_body_payload = {
                "resource_type": verification["resource_type"],
                "resource_id": resource_id,
                "expected": verification["expected"],
                "token": operation_id,
            }
            verify_headers = {
                "Idempotency-Key": self._idempotency_key(
                    "POST",
                    "/api/v1/ops/verify",
                    f"{operation_id}:verify",
                    body=verify_body_payload,
                )
            }
            verify_status, verify_body = self._http(
                "POST",
                "/api/v1/ops/verify",
                body=verify_body_payload,
                timeout=self._timeout,
                query=None,
                headers=verify_headers,
            )
            verified = (
                200 <= int(verify_status) < 300
                and isinstance(verify_body, dict)
                and verify_body.get("verified") is True
            )
        if compact_workflow_receipt:
            if not verified:
                return {
                    "status": 409,
                    "body": {
                        "run_id": resource_id,
                        "error": "write_verification_failed",
                        "verification_status": int(verify_status),
                    },
                }
            run_status = (
                verify_view.get("status")
                if isinstance(verify_view, dict)
                else None
            )
            if not isinstance(run_status, str) or not 1 <= len(run_status) <= 64:
                run_status = (
                    raw_write_body.get("status")
                    if isinstance(raw_write_body, dict)
                    else None
                )
            if not isinstance(run_status, str) or not 1 <= len(run_status) <= 64:
                run_status = "created"
            receipt = {
                "run_id": resource_id,
                "status": run_status,
            }
            callback = self._workflow_run_receipt_callback
            if callback is not None:
                try:
                    registered = callback(dict(receipt)) is True
                except Exception:
                    registered = False
                if not registered:
                    receipt["followup_status"] = "registration_failed"
            return {
                "status": int(write_response["status"]),
                "body": receipt,
            }
        verification_response = {
            "status": int(verify_status),
            "body": _redact_value(verify_body, self._redactor),
        }
        return {
            "status": int(write_response["status"]) if verified else 409,
            "body": {
                "verified": verified,
                "write": write_response,
                "verification": verification_response,
            },
        }

    @staticmethod
    def _verification_view(verify_body, verification):
        if not isinstance(verify_body, dict):
            return None
        step_id = verification.get("step_id")
        if not step_id:
            return verify_body
        steps = verify_body.get("steps")
        if not isinstance(steps, list):
            return None
        selected = None
        for item in steps:
            if isinstance(item, dict) and str(item.get("step_id")) == str(step_id):
                selected = item
                break
        if selected is None:
            return None
        view = dict(selected)
        for source, target in (
            ("phase", "progress_phase"),
            ("message", "progress_message"),
            ("percent", "progress_percent"),
            ("progress", "progress_json"),
        ):
            if target in selected:
                view[source] = selected[target]
        return view

    def _handle_raw(self, method, path, query, headers):
        parsed = urlsplit(path)
        if (
            self._raw_http is None
            or method != "GET"
            or query is not None
            or not _SKILL_DOWNLOAD_PATH.fullmatch(parsed.path)
        ):
            raise ValueError("hub_api raw response is not allowed")
        status, content, response_headers = self._raw_http(
            method,
            path,
            timeout=self._timeout,
            query=None,
            headers=headers,
        )
        if not isinstance(content, bytes):
            raise ValueError("hub_api raw response body is invalid")
        if len(content) > self._max_raw_response_bytes:
            raise ValueError("hub_api raw response exceeds limit")
        selected = {}
        for name, value in dict(response_headers or {}).items():
            folded = str(name).lower()
            if folded in _RAW_RESPONSE_HEADERS:
                selected[folded] = str(value)
        return {
            "status": int(status),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "headers": selected,
        }

    def _validate_path(self, value):
        path = str(value or "")
        parsed = urlsplit(path)
        if (
            not path.startswith("/api/")
            or path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or "\r" in path
            or "\n" in path
        ):
            raise ValueError("hub_api path must be a relative /api/ path")
        return path

    def _validate_headers(self, value):
        if not isinstance(value, dict):
            raise ValueError("hub_api headers must be an object")
        headers = {}
        for name, header_value in value.items():
            key = str(name)
            if key.lower() in DENIED_HEADERS:
                raise ValueError(f"hub_api header is not allowed: {key}")
            headers[key] = str(header_value)
        return headers

    def _idempotency_key(self, method, path, operation_id, *, query=None, body=None):
        material = _json_bytes({
            "method": method,
            "path": path,
            "operation_id": operation_id,
            "query_sha256": hashlib.sha256(
                _canonical_json_bytes(query if query is not None else {})
            ).hexdigest(),
            "body_sha256": hashlib.sha256(
                _canonical_json_bytes(body if body is not None else {})
            ).hexdigest(),
            "token": self.token,
        })
        return hashlib.sha256(material).hexdigest()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
        return False
