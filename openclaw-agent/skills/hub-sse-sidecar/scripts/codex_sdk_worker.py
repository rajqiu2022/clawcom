"""One-shot Codex SDK worker owned by the Sidecar process-tree boundary."""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping


def _error_code(exc: BaseException) -> tuple[str, bool]:
    name = type(exc).__name__.casefold()
    if "auth" in name or "login" in name or "credential" in name:
        return "authentication_required", False
    if "ratelimit" in name or "rate_limit" in name:
        return "rate_limited", True
    if "quota" in name or "usage" in name:
        return "quota_exhausted", False
    if "timeout" in name:
        return "timeout", True
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "provider_unavailable", False
    if isinstance(exc, (LookupError, ValueError, KeyError)):
        return "session_unavailable", False
    return "provider_error", True


def _usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item if not isinstance(item, str) else item[:256]
        for key, item in value.items()
        if isinstance(key, str) and len(key) <= 64
        and isinstance(item, (int, float, bool, str))
    }


def _resume(codex: Any, thread_id: str) -> Any:
    for name in ("thread_resume", "resume_thread"):
        method = getattr(codex, name, None)
        if callable(method):
            return method(thread_id)
    raise LookupError("thread resume unavailable")


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    from openai_codex import Codex, Sandbox

    prompt = request.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("prompt is required")
    thread_id = str(request.get("thread_id") or "") or None
    workspace = str(request.get("workspace") or "") or None
    model = str(request.get("model") or "") or None
    with Codex() as codex:
        if thread_id:
            thread = _resume(codex, thread_id)
        else:
            options = {"sandbox": Sandbox.read_only}
            if workspace:
                options["working_directory"] = workspace
            if model:
                options["model"] = model
            thread = codex.thread_start(**options)
        result = thread.run(prompt, sandbox=Sandbox.read_only)
        return {
            "ok": True,
            "thread_id": str(getattr(thread, "id", "") or "") or None,
            "final_response": str(getattr(result, "final_response", "") or ""),
            "usage": _usage(getattr(result, "usage", {})),
        }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        response = run(request)
    except BaseException as exc:
        code, retryable = _error_code(exc)
        response = {"ok": False, "error_code": code, "retryable": retryable}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
