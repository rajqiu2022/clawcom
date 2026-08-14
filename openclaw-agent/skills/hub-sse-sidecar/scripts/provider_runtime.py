"""Side-effect-free provider contract shared with cross-platform Sidecars."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
from typing import Any, Callable, Mapping, Protocol


ALLOWED_TASK_KINDS = frozenset({"message", "todo", "workflow", "wecom"})
ALLOWED_EXECUTION_SCOPES = frozenset(
    {"cognitive_only", "repo_read", "repo_write", "full_access"}
)
WORKFLOW_STATUSES = frozenset({"passed", "blocked", "failed", "skipped"})


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    provider_version: str
    task_kinds: frozenset[str]
    execution_scopes: frozenset[str]
    structured_results: bool
    sessions: bool
    cancellation: bool
    readiness: str


@dataclass(frozen=True)
class ProviderInvocation:
    invocation_id: str
    task_kind: str
    prompt: str
    context: Mapping[str, Any]
    session_key: str
    timeout_seconds: int
    workspace: str | None
    execution_scope: str
    result_schema: Mapping[str, Any] | None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.invocation_id or "").strip():
            raise ValueError("invocation id is required")
        if self.task_kind not in ALLOWED_TASK_KINDS:
            raise ValueError("task kind is invalid")
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be text")
        if not str(self.session_key or "").strip():
            raise ValueError("session key is required")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout is invalid")
        if self.execution_scope not in ALLOWED_EXECUTION_SCOPES:
            raise ValueError("execution scope is invalid")


@dataclass(frozen=True)
class ProviderEvent:
    kind: str
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    provider: str
    provider_version: str
    session_id: str | None = None
    final_response: str = ""
    structured_result: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False
    events: tuple[ProviderEvent, ...] = ()

    @property
    def error(self) -> str:
        return str(self.error_code or "")


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()


def strict_workflow_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(value, dict) or value.get("status") not in WORKFLOW_STATUSES:
        return None
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        return None
    return dict(value)


class AgentProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def invoke(
        self,
        invocation: ProviderInvocation,
        on_event: Callable[[ProviderEvent], None],
        cancel: CancellationToken,
    ) -> ProviderResult: ...
