"""Linux Codex SDK provider for autonomous workspace and Hub workflows."""

from __future__ import annotations

from collections import OrderedDict
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable, Mapping
from weakref import WeakValueDictionary

try:
    from .provider_runtime import (
        CancellationToken,
        ProviderCapabilities,
        ProviderEvent,
        ProviderInvocation,
        ProviderResult,
        strict_permission_request,
        strict_workflow_result,
    )
except ImportError:
    from provider_runtime import (
        CancellationToken,
        ProviderCapabilities,
        ProviderEvent,
        ProviderInvocation,
        ProviderResult,
        strict_permission_request,
        strict_workflow_result,
    )


def _installed_version() -> str:
    try:
        return metadata.version("openai-codex")
    except metadata.PackageNotFoundError:
        return "unavailable"


def _default_sdk_factory():
    from openai_codex import Codex

    return Codex()


def _default_sandboxes() -> dict[str, Any]:
    from openai_codex import Sandbox

    return {
        "read_only": Sandbox.read_only,
        "workspace_write": Sandbox.workspace_write,
    }


def _bounded_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        if (
            isinstance(key, str)
            and len(key) <= 64
            and isinstance(item, (int, float, bool, str))
        ):
            result[key] = item if not isinstance(item, str) else item[:256]
    return result


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
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return "provider_config_invalid", False
    return "provider_error", True


def _trusted_developer_instructions(
    context: Mapping[str, Any],
    *,
    workflow_write_enabled: bool = False,
) -> str | None:
    try:
        from .codex_sdk_worker import _developer_instructions
    except ImportError:
        from codex_sdk_worker import _developer_instructions
    return _developer_instructions(
        context,
        workflow_write_enabled=workflow_write_enabled,
    )


def _permission_config(invocation: ProviderInvocation) -> dict[str, Any] | None:
    """Return a single permission-profile system for workspace-bound turns."""

    workspace = str(invocation.workspace or "").strip()
    if not workspace:
        return None
    try:
        from .codex_permissions import (
            permission_profile_config,
            workspace_permission_profile_config,
        )
    except ImportError:
        from codex_permissions import (
            permission_profile_config,
            workspace_permission_profile_config,
        )
    access = "write" if invocation.execution_scope == "repo_write" else "read"
    additional_workspaces = tuple(
        item.strip()
        for item in str(
            invocation.environment.get("PROVIDER_ALLOWED_DIRS", "")
        ).split(os.pathsep)
        if item.strip()
    )
    grant = invocation.context.get("permission_grant")
    if isinstance(grant, Mapping):
        return permission_profile_config(
            grant,
            workspace=workspace,
            workspace_access=access,
            additional_workspaces=additional_workspaces,
        )
    return workspace_permission_profile_config(
        workspace=workspace,
        workspace_access=access,
        additional_workspaces=additional_workspaces,
    )


SESSION_MAP_MAX_ENTRIES = 1024


class CodexSdkProvider:
    """Stable SDK adapter; does not perform login or external notification."""

    def __init__(
        self,
        *,
        sdk_factory: Callable[[], Any] = _default_sdk_factory,
        sandbox_factory: Callable[[], Mapping[str, Any]] = _default_sandboxes,
        sandbox_values: Mapping[str, Any] | None = None,
        provider_version: str | None = None,
        model: str | None = None,
        process_runner: Callable[..., Any] | None = None,
        python_executable: str | None = None,
        worker_path: str | None = None,
        hub_plugin_factory: Callable[[], Any] | None = None,
        hub_mcp_entry: str | None = None,
        session_loader: Callable[[str], str | None] | None = None,
        session_saver: Callable[[str, str], None] | None = None,
        session_clearer: Callable[[str], None] | None = None,
    ) -> None:
        self._sdk_factory = sdk_factory
        self._sandbox_factory = sandbox_factory
        self._sandbox_values = (
            dict(sandbox_values) if sandbox_values is not None else None
        )
        self._provider_version = str(provider_version or _installed_version())
        self._model = str(model or "").strip() or None
        self._process_runner = process_runner
        self._python_executable = str(python_executable or sys.executable)
        self._worker_path = str(
            worker_path or Path(__file__).with_name("codex_sdk_worker.py")
        )
        self._hub_plugin_factory = hub_plugin_factory
        self._hub_mcp_entry = str(hub_mcp_entry or "").strip() or None
        self._session_loader = session_loader
        self._session_saver = session_saver
        self._session_clearer = session_clearer
        self._session_ids: OrderedDict[str, str] = OrderedDict()
        self._session_locks: WeakValueDictionary[str, threading.Lock] = (
            WeakValueDictionary()
        )
        self._session_guard = threading.Lock()

    def _session_lock(self, session_key: str) -> threading.Lock:
        with self._session_guard:
            lock = self._session_locks.get(session_key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_key] = lock
            return lock

    def _load_persisted_session(self, session_key: str) -> str | None:
        if self._session_loader is None:
            return None
        value = str(self._session_loader(session_key) or "").strip()
        if len(value.encode("utf-8", "replace")) > 4096:
            raise ValueError("persisted Codex thread id is invalid")
        return value or None

    def _clear_session(self, session_key: str) -> None:
        with self._session_guard:
            self._session_ids.pop(session_key, None)
        if self._session_clearer is not None:
            self._session_clearer(session_key)

    @staticmethod
    def _resume_thread(
        codex: Any,
        thread_id: str,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        for name in ("thread_resume", "resume_thread"):
            method = getattr(codex, name, None)
            if callable(method):
                return method(thread_id, **dict(options or {}))
        raise LookupError("Codex SDK does not expose thread resume")

    def _start_hub_mcp(self) -> tuple[Any | None, dict[str, Any] | None]:
        if self._hub_plugin_factory is None:
            return None, None
        entry = Path(str(self._hub_mcp_entry or ""))
        if not entry.is_absolute() or entry.is_symlink() or not entry.is_file():
            raise ValueError("Codex Hub MCP entry is invalid")
        plugin = self._hub_plugin_factory()
        try:
            plugin = plugin.start()
            plugin_environment = dict(plugin.environment() or {})
            try:
                from .claw_hub_mcp import validate_proxy_credentials
            except ImportError:
                from claw_hub_mcp import validate_proxy_credentials
            proxy_url, proxy_token = validate_proxy_credentials(
                plugin_environment.get("HUB_PROXY_URL", ""),
                plugin_environment.get("HUB_PROXY_TOKEN", ""),
            )
            raw_definition_ids = str(
                plugin_environment.get(
                    "HUB_WORKFLOW_CREATE_DEFINITION_IDS",
                    "",
                )
                or ""
            ).strip()
            definition_ids = []
            if raw_definition_ids:
                parts = raw_definition_ids.split(",")
                if any(
                    not part.isdecimal() or int(part) <= 0
                    for part in parts
                ):
                    raise ValueError(
                        "Codex Hub MCP workflow create allowlist is invalid"
                    )
                definition_ids = sorted({int(part) for part in parts})
            return plugin, {
                "entry": str(entry),
                "proxy_url": proxy_url,
                "proxy_token": proxy_token,
                "workflow_create_definition_ids": definition_ids,
                "wecom_reply_enabled": (
                    plugin_environment.get("CLAW_WECOM_REPLY_ENABLED") == "1"
                ),
            }
        except Exception:
            try:
                plugin.close()
            except Exception:
                pass
            raise

    def capabilities(self) -> ProviderCapabilities:
        readiness = "canary" if self._provider_version != "unavailable" else "unavailable"
        return ProviderCapabilities(
            provider="codex",
            provider_version=self._provider_version,
            # WeCom and todo are message-like cognitive turns. Transport,
            # authorization, dedupe, persistence and delivery stay in Sidecar.
            task_kinds=frozenset({"message", "todo", "workflow", "wecom"}),
            execution_scopes=frozenset(
                {"cognitive_only", "repo_read", "repo_write"}
            ),
            structured_results=True,
            sessions=True,
            # The adapter only observes cancellation before and after run().
            # Do not advertise active cancellation until the SDK call can be
            # interrupted and its process tree is proven to converge.
            cancellation=False,
            readiness=readiness,
        )

    def _failure(
        self,
        code: str,
        retryable: bool = False,
        *,
        permission_request: Mapping[str, Any] | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider="codex",
            provider_version=self._provider_version,
            error_code=code,
            retryable=retryable,
            permission_request=permission_request,
        )

    def _run_isolated(
        self,
        invocation: ProviderInvocation,
        mapped_thread_id: str | None,
    ) -> tuple[Any, str | None] | ProviderResult:
        """Run the SDK/app-server in a killable process-tree boundary."""

        if self._process_runner is None:
            raise RuntimeError("isolated Codex process runner is not configured")
        request = {
            "prompt": invocation.prompt,
            "thread_id": mapped_thread_id,
            "workspace": invocation.workspace,
            "model": self._model,
            "context": dict(invocation.context),
            "execution_scope": invocation.execution_scope,
        }
        permission_grant = invocation.context.get("permission_grant")
        if isinstance(permission_grant, Mapping):
            request["permission_grant"] = dict(permission_grant)
        plugin = None
        try:
            plugin, hub_mcp = self._start_hub_mcp()
            if hub_mcp is not None:
                request["hub_mcp"] = hub_mcp
            completed = self._process_runner(
                [self._python_executable, self._worker_path],
                prompt=json.dumps(request, ensure_ascii=False),
                timeout=invocation.timeout_seconds,
                env=dict(invocation.environment),
                cwd=None,
                stdout_mode="raw",
            )
        finally:
            if plugin is not None:
                plugin.close()
        if completed.returncode != 0:
            return self._failure("provider_process_failed", retryable=True)
        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            return self._failure("provider_protocol_invalid")
        if not isinstance(response, dict):
            return self._failure("provider_protocol_invalid")
        if response.get("ok") is not True:
            code = str(response.get("error_code") or "provider_error")
            retryable = bool(response.get("retryable", False))
            return self._failure(code, retryable)
        final_response = response.get("final_response", "")
        usage = response.get("usage", {})
        thread_id = str(response.get("thread_id") or "") or None
        if not isinstance(final_response, str) or not isinstance(usage, Mapping):
            return self._failure("provider_protocol_invalid")
        result = type(
            "IsolatedCodexResult",
            (),
            {"final_response": final_response, "usage": dict(usage)},
        )()
        return result, thread_id

    def invoke(
        self,
        invocation: ProviderInvocation,
        on_event: Callable[[ProviderEvent], None],
        cancel: CancellationToken,
    ) -> ProviderResult:
        if cancel.cancelled:
            return self._failure("cancelled")
        if invocation.execution_scope not in {
            "cognitive_only",
            "repo_read",
            "repo_write",
        }:
            return self._failure("scope_not_allowed")
        if invocation.execution_scope == "repo_write":
            workspace = Path(str(invocation.workspace or ""))
            if (
                not workspace.is_absolute()
                or workspace.is_symlink()
                or not workspace.is_dir()
            ):
                return self._failure("provider_config_invalid")
        if self._provider_version == "unavailable":
            return self._failure("provider_unavailable")
        try:
            sandboxes = self._sandbox_values or self._sandbox_factory()
            read_only = sandboxes.get("read_only")
            workspace_write = sandboxes.get("workspace_write")
        except (ImportError, ModuleNotFoundError):
            return self._failure("provider_unavailable")
        except Exception:
            return self._failure("provider_config_invalid")
        sandbox = (
            workspace_write
            if invocation.execution_scope == "repo_write"
            else read_only
        )
        if sandbox is None:
            return self._failure("provider_config_invalid")

        on_event(ProviderEvent("started", "Codex invocation started"))
        session_lock = self._session_lock(invocation.session_key)
        with session_lock:
            with self._session_guard:
                mapped_thread_id = self._session_ids.get(invocation.session_key)
                if mapped_thread_id:
                    self._session_ids.move_to_end(invocation.session_key)
            if mapped_thread_id is None:
                try:
                    mapped_thread_id = self._load_persisted_session(
                        invocation.session_key
                    )
                except Exception:
                    return self._failure("provider_config_invalid")
                if mapped_thread_id:
                    with self._session_guard:
                        self._session_ids[invocation.session_key] = mapped_thread_id
            try:
                if self._process_runner is not None:
                    isolated = self._run_isolated(invocation, mapped_thread_id)
                    if (
                        isinstance(isolated, ProviderResult)
                        and isolated.error_code == "session_unavailable"
                        and mapped_thread_id is not None
                    ):
                        self._clear_session(invocation.session_key)
                    if isinstance(isolated, ProviderResult):
                        return isolated
                    sdk_result, thread_id = isolated
                else:
                    # Kept only as an injectable unit-test seam. Production
                    # always supplies the Sidecar's process-tree runner.
                    with self._sdk_factory() as codex:
                        permission_config = _permission_config(invocation)
                        if mapped_thread_id:
                            try:
                                resume_options = {}
                                if permission_config is None:
                                    resume_options["sandbox"] = sandbox
                                else:
                                    resume_options["config"] = permission_config
                                developer_instructions = (
                                    _trusted_developer_instructions(
                                        invocation.context,
                                        workflow_write_enabled=(
                                            invocation.execution_scope
                                            == "repo_write"
                                        ),
                                    )
                                )
                                if developer_instructions:
                                    resume_options["developer_instructions"] = (
                                        developer_instructions
                                    )
                                thread = self._resume_thread(
                                    codex,
                                    mapped_thread_id,
                                    resume_options,
                                )
                            except (LookupError, ValueError, KeyError):
                                self._clear_session(invocation.session_key)
                                return self._failure("session_unavailable")
                        else:
                            start_options = {}
                            if permission_config is None:
                                start_options["sandbox"] = sandbox
                            else:
                                start_options["config"] = permission_config
                            if self._model:
                                start_options["model"] = self._model
                            if invocation.workspace:
                                start_options["cwd"] = invocation.workspace
                            developer_instructions = (
                                _trusted_developer_instructions(
                                    invocation.context,
                                    workflow_write_enabled=(
                                        invocation.execution_scope == "repo_write"
                                    ),
                                )
                            )
                            if developer_instructions:
                                start_options["developer_instructions"] = (
                                    developer_instructions
                                )
                            thread = codex.thread_start(**start_options)
                        if cancel.cancelled:
                            return self._failure("cancelled")
                        run_options = (
                            {"sandbox": sandbox}
                            if permission_config is None else {}
                        )
                        sdk_result = thread.run(invocation.prompt, **run_options)
                        thread_id = str(getattr(thread, "id", "") or "") or None
            except Exception as exc:
                code, retryable = _error_code(exc)
                return self._failure(code, retryable)

        if cancel.cancelled:
            return self._failure("cancelled")
        final_response = str(getattr(sdk_result, "final_response", "") or "")
        permission_request = strict_permission_request(final_response)
        if permission_request is not None:
            if thread_id:
                if self._session_saver is not None:
                    try:
                        self._session_saver(invocation.session_key, thread_id)
                    except Exception:
                        return self._failure("provider_config_invalid")
                with self._session_guard:
                    self._session_ids[invocation.session_key] = thread_id
                    self._session_ids.move_to_end(invocation.session_key)
                    while len(self._session_ids) > SESSION_MAP_MAX_ENTRIES:
                        self._session_ids.popitem(last=False)
            return self._failure(
                "permission_required",
                permission_request=permission_request,
            )
        structured = (
            strict_workflow_result(final_response)
            if invocation.task_kind == "workflow"
            else None
        )
        if invocation.task_kind == "workflow" and structured is None:
            # A formatting mistake is recoverable in the same persisted
            # Workflow session. Sidecar asks Codex to emit the required
            # result or permission envelope without repeating external work.
            return self._failure("result_invalid", retryable=True)
        usage = _bounded_usage(getattr(sdk_result, "usage", {}))
        if thread_id:
            if self._session_saver is not None:
                try:
                    self._session_saver(invocation.session_key, thread_id)
                except Exception:
                    return self._failure("provider_config_invalid")
            with self._session_guard:
                self._session_ids[invocation.session_key] = thread_id
                self._session_ids.move_to_end(invocation.session_key)
                while len(self._session_ids) > SESSION_MAP_MAX_ENTRIES:
                    self._session_ids.popitem(last=False)
        on_event(ProviderEvent("completed", "Codex invocation completed"))
        return ProviderResult(
            ok=True,
            provider="codex",
            provider_version=self._provider_version,
            session_id=thread_id,
            final_response=final_response,
            structured_result=structured,
            usage=usage,
        )
