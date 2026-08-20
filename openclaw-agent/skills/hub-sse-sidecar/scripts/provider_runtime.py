"""Side-effect-free contract shared by every cognitive Agent provider."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


ALLOWED_PROVIDERS = frozenset({"hermes", "codex"})
RETIRED_PROVIDERS = frozenset({"pi"})
ALLOWED_TASK_KINDS = frozenset({"message", "todo", "workflow", "wecom"})
ALLOWED_EXECUTION_SCOPES = frozenset(
    {"cognitive_only", "repo_read", "repo_write", "full_access"}
)
WORKFLOW_STATUSES = frozenset({"passed", "blocked", "failed", "skipped"})
FINAL_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
PROVIDER_KEY_MAX_BYTES = 512


class RetiredProviderError(LookupError):
    """A historical provider was selected after its runtime retirement."""


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

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        if provider not in ALLOWED_PROVIDERS:
            if provider in RETIRED_PROVIDERS:
                raise RetiredProviderError(f"provider retired: {provider}")
            raise ValueError(f"provider not allowed: {provider or '<empty>'}")
        if not str(self.provider_version or "").strip():
            raise ValueError("provider version is required")
        if not self.task_kinds or not self.task_kinds <= ALLOWED_TASK_KINDS:
            raise ValueError("provider task kinds are invalid")
        if (
            not self.execution_scopes
            or not self.execution_scopes <= ALLOWED_EXECUTION_SCOPES
        ):
            raise ValueError("provider execution scopes are invalid")
        if self.readiness not in {"unavailable", "canary", "production"}:
            raise ValueError("provider readiness is invalid")


@dataclass(frozen=True)
class ProviderInvocation:
    """Trusted, invocation-scoped inputs supplied by the Sidecar."""

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
        invocation_id = str(self.invocation_id or "").strip()
        if not invocation_id:
            raise ValueError("invocation id is required")
        if len(invocation_id.encode("utf-8", "replace")) > PROVIDER_KEY_MAX_BYTES:
            raise ValueError("invocation id is too large")
        if self.task_kind not in ALLOWED_TASK_KINDS:
            raise ValueError("task kind is invalid")
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be text")
        if not isinstance(self.context, Mapping):
            raise TypeError("context must be a mapping")
        session_key = str(self.session_key or "").strip()
        if not session_key:
            raise ValueError("session key is required")
        if len(session_key.encode("utf-8", "replace")) > PROVIDER_KEY_MAX_BYTES:
            raise ValueError("session key is too large")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout is invalid")
        if self.execution_scope not in ALLOWED_EXECUTION_SCOPES:
            raise ValueError("execution scope is invalid")


@dataclass(frozen=True)
class ProviderEvent:
    kind: str
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {
            "started",
            "progress",
            "tool",
            "usage",
            "completed",
            "warning",
        }:
            raise ValueError("provider event kind is invalid")
        if len(self.message.encode("utf-8", "replace")) > 4096:
            raise ValueError("provider event message is too large")


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
    stdout: str = ""
    stderr: str = ""
    events: tuple[ProviderEvent, ...] = ()
    permission_request: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.provider not in ALLOWED_PROVIDERS:
            raise ValueError("provider result identity is invalid")
        if not str(self.provider_version or "").strip():
            raise ValueError("provider result version is required")
        if len(self.final_response.encode("utf-8", "replace")) > FINAL_RESPONSE_MAX_BYTES:
            raise ValueError("provider final response is too large")
        if self.ok and self.error_code is not None:
            raise ValueError("successful provider result cannot contain an error code")
        if not self.ok and not self.error_code:
            raise ValueError("failed provider result requires an error code")
        if self.permission_request is not None and (
            self.ok or self.error_code != "permission_required"
        ):
            raise ValueError("permission request requires permission_required failure")

    @property
    def error(self) -> str:
        """Compatibility-safe error text; never exposes native exceptions."""

        return str(self.error_code or "")

    def as_tuple(self) -> tuple[bool, str, str]:
        return self.ok, self.final_response, self.error


class CancellationToken:
    """Thread-safe monotonic cancellation used for lease loss and shutdown."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "cancelled")[:128]
            self._event.set()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def strict_json_object(value: Any) -> dict[str, Any] | None:
    """Parse one complete JSON object; never scan prose or tool output."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def strict_workflow_result(value: Any) -> dict[str, Any] | None:
    parsed = strict_json_object(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict) or parsed.get("status") not in WORKFLOW_STATUSES:
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return dict(parsed)


def strict_permission_request(value: Any) -> dict[str, str] | None:
    parsed = strict_json_object(value) if isinstance(value, str) else value
    if (
        isinstance(parsed, dict)
        and parsed.get("status") in {"blocked", "failed"}
        and isinstance(parsed.get("permission_request"), dict)
    ):
        # A Workflow may preserve its ordinary result envelope while asking
        # the Worker to pause and broker one missing permission.  The nested
        # request is still normalized with the exact same strict contract.
        parsed = parsed["permission_request"]
    try:
        from .codex_permissions import normalize_permission_request
    except ImportError:
        from codex_permissions import normalize_permission_request
    try:
        return normalize_permission_request(parsed)
    except (OSError, ValueError):
        return None


@runtime_checkable
class AgentProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities:
        """Return immutable, locally attested provider capabilities."""

    def invoke(
        self,
        invocation: ProviderInvocation,
        on_event: Callable[[ProviderEvent], None],
        cancel: CancellationToken,
    ) -> ProviderResult:
        """Execute one invocation without publishing Hub or external effects."""


class FunctionAgentProvider:
    """Small adapter for tests and legacy provider migration."""

    def __init__(
        self,
        capabilities: ProviderCapabilities,
        executor: Callable[
            [ProviderInvocation, Callable[[ProviderEvent], None], CancellationToken],
            ProviderResult,
        ],
    ) -> None:
        self._capabilities = capabilities
        self._executor = executor

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def invoke(
        self,
        invocation: ProviderInvocation,
        on_event: Callable[[ProviderEvent], None],
        cancel: CancellationToken,
    ) -> ProviderResult:
        result = self._executor(invocation, on_event, cancel)
        if not isinstance(result, ProviderResult):
            raise TypeError("provider returned an invalid result")
        capabilities = self.capabilities()
        if (
            result.provider != capabilities.provider
            or result.provider_version != capabilities.provider_version
        ):
            raise ValueError("provider result identity mismatch")
        return result


class ProviderRegistry:
    """Local-only registry. Hub payloads cannot add or replace providers."""

    def __init__(self) -> None:
        self._providers: dict[str, AgentProvider] = {}

    def register(self, provider: AgentProvider) -> None:
        capabilities = provider.capabilities()
        name = capabilities.provider
        if name in self._providers:
            raise ValueError(f"duplicate provider: {name}")
        self._providers[name] = provider

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def get(self, name: str) -> AgentProvider:
        normalized = str(name or "").strip().lower()
        if normalized in RETIRED_PROVIDERS:
            raise RetiredProviderError(f"provider retired: {normalized}")
        if normalized not in ALLOWED_PROVIDERS:
            raise LookupError(f"unsupported provider: {normalized or '<empty>'}")
        try:
            return self._providers[normalized]
        except KeyError:
            raise LookupError(f"provider unavailable: {normalized}") from None

    def invoke(
        self,
        name: str,
        invocation: ProviderInvocation,
        on_event: Callable[[ProviderEvent], None],
        cancel: CancellationToken,
    ) -> ProviderResult:
        provider = self.get(name)
        capabilities = provider.capabilities()
        if invocation.task_kind not in capabilities.task_kinds:
            raise ValueError("provider does not support task kind")
        if invocation.execution_scope not in capabilities.execution_scopes:
            raise ValueError("provider does not support execution scope")
        return provider.invoke(invocation, on_event, cancel)
