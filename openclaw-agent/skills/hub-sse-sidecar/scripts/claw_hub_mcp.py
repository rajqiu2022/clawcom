"""Minimal local MCP facade for Agent Provider-to-Hub calls.

The real Hub credential remains in the sidecar. Hermes and Codex can call this
local server, which forwards requests to the ephemeral loopback proxy using
only the invocation-scoped proxy token.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


_DENIED_HEADER_NAMES = {"authorization", "host", "content-length"}
_MAX_CREDENTIAL_FILE_BYTES = 4096
_MAX_PROXY_RESPONSE_BYTES = 1024 * 1024


def _workflow_create_definition_ids(value) -> tuple[int, ...]:
    if value is None or value == "":
        raw = ()
    elif isinstance(value, str):
        parts = value.split(",")
        if any(not part.isdecimal() or int(part) <= 0 for part in parts):
            raise ValueError("Hub MCP workflow create allowlist is invalid")
        raw = tuple(int(part) for part in parts)
    else:
        raw = value
    try:
        from src.hub_proxy import normalize_workflow_create_definition_ids
    except ModuleNotFoundError:
        from hub_proxy import normalize_workflow_create_definition_ids
    return normalize_workflow_create_definition_ids(raw)


def _server_instructions(
    definition_ids: tuple[int, ...], *, wecom_reply_enabled: bool = False
) -> str:
    base = (
        "Sidecar exposes hub_api. Deferred clients must discover it before "
        "declaring it unavailable. Results are JSON in content[0].text; Hub "
        "payload is body. error_origin=worker_hub_proxy means local rejection: "
        "report body.error, not attributed to Hub. "
    )
    if wecom_reply_enabled:
        base += (
            "Worker also exposes wecom_reply for bounded messages to the "
            "bound owner; the model cannot select another recipient. "
        )
    if not definition_ids:
        return base + "Workflow Run collection creation is not authorized."
    allowed = ",".join(str(item) for item in definition_ids)
    definition_clause = (
        f"body.definition_id={definition_ids[0]}"
        if len(definition_ids) == 1
        else f"body.definition_id in [{allowed}]"
    )
    return (
        base
        + "Run create only: POST /api/v1/workflow-runs "
        + f"with {definition_clause} and matching verify.expected; verify via "
        + "response id at GET /api/v1/workflow-runs/{resource_id}. "
        + "Executor overrides and alternate start contracts are forbidden."
    )


def validate_proxy_credentials(proxy_url: str, proxy_token: str) -> tuple[str, str]:
    parsed = urlsplit(str(proxy_url or "").rstrip("/"))
    try:
        proxy_port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "Hub MCP proxy URL must be an http://127.0.0.1 loopback URL"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not proxy_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Hub MCP proxy URL must be an http://127.0.0.1 loopback URL")
    token = str(proxy_token or "")
    if not token.strip():
        raise ValueError("Hub MCP proxy token is required")
    return f"http://127.0.0.1:{proxy_port}", token


def load_proxy_credentials(path: str) -> tuple[str, str]:
    credential_path = Path(str(path or ""))
    if not credential_path.is_absolute() or credential_path.is_symlink():
        raise ValueError("Hub MCP credential file must be an absolute regular file")
    stat = credential_path.stat()
    if not credential_path.is_file() or stat.st_size > _MAX_CREDENTIAL_FILE_BYTES:
        raise ValueError("Hub MCP credential file must be an absolute regular file")
    payload = json.loads(credential_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Hub MCP credential file must contain an object")
    if set(payload) != {"proxy_url", "proxy_token"}:
        raise ValueError("Hub MCP credential file has unexpected fields")
    return validate_proxy_credentials(
        payload.get("proxy_url", ""), payload.get("proxy_token", "")
    )


def tool_definitions(
    workflow_create_definition_ids=(), *, wecom_reply_enabled: bool = False
) -> list[dict]:
    definition_ids = _workflow_create_definition_ids(
        workflow_create_definition_ids
    )
    if definition_ids:
        allowed = ",".join(str(item) for item in definition_ids)
        create_description = (
            "Workflow Run collection creation is restricted to definition "
            f"IDs [{allowed}] and cannot override node executors."
        )
        verify_description = (
            "Required for writes. Workflow Run creation must use the created "
            "response id and verify "
            + (
                f"definition_id={definition_ids[0]}"
                if len(definition_ids) == 1
                else f"definition_id in [{allowed}]"
            )
            + " at "
            "/api/v1/workflow-runs/{resource_id}."
        )
    else:
        create_description = (
            "Workflow Run collection creation is not authorized for this instance."
        )
        verify_description = "Required for authorized writes."
    tools = [
        {
            "name": "hub_api",
            "description": (
                "Call the configured Hub API through the local sidecar proxy. "
                "Only relative /api/... paths are allowed by the proxy. "
                + create_description
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "path": {"type": "string"},
                    "query": {"type": "object"},
                    "body": {},
                    "operation_id": {"type": "string"},
                    "verify": {
                        "type": "object",
                        "description": verify_description,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
    if wecom_reply_enabled:
        tools.append({
            "name": "wecom_reply",
            "description": (
                "Queue one concise text update or conclusion through the "
                "Worker to the already-bound WeCom owner. The recipient is "
                "fixed by Worker and cannot be supplied by the model."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "message": {"type": "string", "maxLength": 4000},
                },
                "required": ["operation_id", "message"],
                "additionalProperties": False,
            },
        })
    return tools


def _default_transport(url: str, token: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(_MAX_PROXY_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_PROXY_RESPONSE_BYTES + 1)
        result = _decode_proxy_response(raw)
        if result.get("status") != int(exc.code):
            raise ValueError(
                "Hub MCP proxy returned a mismatched HTTP error envelope"
            ) from None
        return result
    return _decode_proxy_response(raw)


def _decode_proxy_response(raw: bytes) -> dict:
    if len(raw) > _MAX_PROXY_RESPONSE_BYTES:
        raise ValueError("Hub MCP proxy response exceeds limit")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Hub MCP proxy returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Hub MCP proxy response must be an object")
    return result


class ClawHubMcpServer:
    def __init__(
        self,
        *,
        proxy_url: str,
        proxy_token: str,
        transport=None,
        workflow_create_definition_ids=(),
        wecom_reply_enabled: bool = False,
    ):
        self.proxy_url, self.proxy_token = validate_proxy_credentials(
            proxy_url, proxy_token
        )
        self.transport = transport or _default_transport
        self.workflow_create_definition_ids = _workflow_create_definition_ids(
            workflow_create_definition_ids
        )
        self.wecom_reply_enabled = bool(wecom_reply_enabled)

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in ({"hub_api", "wecom_reply"} if self.wecom_reply_enabled else {"hub_api"}):
            raise ValueError("MCP tool is not allowed")
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be an object")
        if name == "wecom_reply":
            if set(arguments) != {"operation_id", "message"}:
                raise ValueError("wecom_reply arguments are invalid")
            return self.transport(
                f"{self.proxy_url}/wecom-reply",
                self.proxy_token,
                {
                    "operation_id": arguments.get("operation_id"),
                    "message": arguments.get("message"),
                },
            )
        payload = {
            key: value
            for key, value in arguments.items()
            if key in {
                "method",
                "path",
                "query",
                "body",
                "operation_id",
                "verify",
                "response_mode",
            }
        }
        headers = arguments.get("headers")
        if isinstance(headers, dict):
            filtered = {
                str(key): str(value)
                for key, value in headers.items()
                if str(key).lower() not in _DENIED_HEADER_NAMES
            }
            if filtered:
                payload["headers"] = filtered
        return self.transport(
            f"{self.proxy_url}/hub-api",
            self.proxy_token,
            payload,
        )

    def handle_jsonrpc(self, message: dict) -> dict:
        method = message.get("method")
        request_id = message.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "claw-hub", "version": "1"},
                    "instructions": _server_instructions(
                        self.workflow_create_definition_ids,
                        wecom_reply_enabled=self.wecom_reply_enabled,
                    ),
                }
            elif method == "tools/list":
                result = {
                    "tools": tool_definitions(
                        self.workflow_create_definition_ids,
                        wecom_reply_enabled=self.wecom_reply_enabled,
                    )
                }
            elif method == "tools/call":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                self.call_tool(
                                    str(params.get("name") or ""),
                                    params.get("arguments") or {},
                                ),
                                ensure_ascii=False,
                            ),
                        }
                    ]
                }
            else:
                raise ValueError("method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)[:500]},
            }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--proxy-token", default="")
    parser.add_argument("--credential-file", default="")
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("--stdio is required")
    if args.credential_file:
        if args.proxy_url or args.proxy_token:
            parser.error("--credential-file cannot be combined with proxy arguments")
        proxy_url, proxy_token = load_proxy_credentials(args.credential_file)
    else:
        proxy_url = args.proxy_url or _env("HUB_PROXY_URL")
        proxy_token = args.proxy_token or _env("HUB_PROXY_TOKEN")
    server = ClawHubMcpServer(
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        workflow_create_definition_ids=_env(
            "HUB_WORKFLOW_CREATE_DEFINITION_IDS"
        ),
        wecom_reply_enabled=_env("CLAW_WECOM_REPLY_ENABLED") == "1",
    )
    for line in sys.stdin:
        if not line.strip():
            continue
        response = server.handle_jsonrpc(json.loads(line))
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def _env(name: str) -> str:
    return os.getenv(name, "")


if __name__ == "__main__":
    raise SystemExit(main())
