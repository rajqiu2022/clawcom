"""Cross-platform Codex SDK provider using the current OS user's CLI auth."""

from __future__ import annotations

from collections import OrderedDict
from importlib import metadata
import json
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Mapping
from weakref import WeakValueDictionary

from provider_runtime import (
    CancellationToken,
    ProviderCapabilities,
    ProviderEvent,
    ProviderInvocation,
    ProviderResult,
    strict_workflow_result,
)


def _installed_version() -> str:
    try:
        return metadata.version("openai-codex")
    except metadata.PackageNotFoundError:
        return "unavailable"


def _sdk_factory():
    from openai_codex import Codex
    return Codex()


def _sandboxes() -> dict[str, Any]:
    from openai_codex import Sandbox
    return {"read_only": Sandbox.read_only}


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
    return "provider_error", True


class CodexSdkProvider:
    """SDK adapter; login and Hub publication stay outside the provider."""

    def __init__(
        self,
        *,
        sdk_factory: Callable[[], Any] = _sdk_factory,
        sandbox_factory: Callable[[], Mapping[str, Any]] = _sandboxes,
        provider_version: str | None = None,
        model: str | None = None,
        process_runner: Callable[..., Any] | None = None,
        python_executable: str | None = None,
        worker_path: str | None = None,
    ) -> None:
        self._sdk_factory = sdk_factory
        self._sandbox_factory = sandbox_factory
        self._provider_version = str(provider_version or _installed_version())
        self._model = str(model or "").strip() or None
        self._process_runner = process_runner
        self._python_executable = str(python_executable or sys.executable)
        self._worker_path = str(
            worker_path or Path(__file__).with_name("codex_sdk_worker.py")
        )
        self._session_ids: OrderedDict[str, str] = OrderedDict()
        self._session_locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
        self._guard = threading.Lock()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="codex",
            provider_version=self._provider_version,
            task_kinds=frozenset({"message", "todo", "workflow", "wecom"}),
            execution_scopes=frozenset({"cognitive_only", "repo_read"}),
            structured_results=True,
            sessions=True,
            cancellation=False,
            readiness="canary" if self._provider_version != "unavailable" else "unavailable",
        )

    def _failure(self, code: str, retryable: bool = False) -> ProviderResult:
        return ProviderResult(False, "codex", self._provider_version,
                              error_code=code, retryable=retryable)

    def _run_isolated(self, invocation, thread_id):
        request = {
            "prompt": invocation.prompt,
            "thread_id": thread_id,
            "workspace": invocation.workspace,
            "model": self._model,
        }
        completed = self._process_runner(
            [self._python_executable, self._worker_path],
            prompt=json.dumps(request, ensure_ascii=False),
            timeout=invocation.timeout_seconds,
            env=dict(invocation.environment),
            cwd=None,
            stdout_mode="raw",
        )
        if completed.returncode != 0:
            return self._failure("provider_process_failed", True)
        try:
            response = json.loads(completed.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._failure("provider_protocol_invalid")
        if not isinstance(response, dict):
            return self._failure("provider_protocol_invalid")
        if response.get("ok") is not True:
            return self._failure(
                str(response.get("error_code") or "provider_error"),
                bool(response.get("retryable", False)),
            )
        final_response = response.get("final_response", "")
        usage = response.get("usage", {})
        if not isinstance(final_response, str) or not isinstance(usage, Mapping):
            return self._failure("provider_protocol_invalid")
        return final_response, dict(usage), (
            str(response.get("thread_id") or "") or None
        )

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

    @staticmethod
    def _resume(codex: Any, thread_id: str) -> Any:
        for name in ("thread_resume", "resume_thread"):
            method = getattr(codex, name, None)
            if callable(method):
                return method(thread_id)
        raise LookupError("thread resume unavailable")

    def invoke(self, invocation, on_event, cancel) -> ProviderResult:
        if cancel.cancelled:
            return self._failure("cancelled")
        if invocation.execution_scope not in {"cognitive_only", "repo_read"}:
            return self._failure("scope_not_allowed")
        if self._provider_version == "unavailable":
            return self._failure("provider_unavailable")
        try:
            read_only = self._sandbox_factory().get("read_only")
        except Exception:
            return self._failure("provider_unavailable")
        if read_only is None:
            return self._failure("provider_config_invalid")

        on_event(ProviderEvent("started", "Codex invocation started"))
        with self._lock(invocation.session_key):
            with self._guard:
                thread_id = self._session_ids.get(invocation.session_key)
            try:
                if self._process_runner is not None:
                    isolated = self._run_isolated(invocation, thread_id)
                    if isinstance(isolated, ProviderResult):
                        return isolated
                    final_response, usage, actual_thread_id = isolated
                else:
                    with self._sdk_factory() as codex:
                        if thread_id:
                            thread = self._resume(codex, thread_id)
                        else:
                            options = {"sandbox": read_only}
                            if self._model:
                                options["model"] = self._model
                            if invocation.workspace:
                                options["working_directory"] = invocation.workspace
                            thread = codex.thread_start(**options)
                        result = thread.run(invocation.prompt, sandbox=read_only)
                        final_response = str(
                            getattr(result, "final_response", "") or ""
                        )
                        usage = getattr(result, "usage", {})
                        actual_thread_id = (
                            str(getattr(thread, "id", "") or "") or None
                        )
            except Exception as exc:
                code, retryable = _error_code(exc)
                return self._failure(code, retryable)

        structured = strict_workflow_result(final_response) if invocation.task_kind == "workflow" else None
        if invocation.task_kind == "workflow" and structured is None:
            return self._failure("result_invalid")
        if actual_thread_id:
            with self._guard:
                self._session_ids[invocation.session_key] = actual_thread_id
                while len(self._session_ids) > 1024:
                    self._session_ids.popitem(last=False)
        on_event(ProviderEvent("completed", "Codex invocation completed"))
        return ProviderResult(True, "codex", self._provider_version,
                              session_id=actual_thread_id,
                              final_response=final_response,
                              structured_result=structured,
                              usage=usage)
