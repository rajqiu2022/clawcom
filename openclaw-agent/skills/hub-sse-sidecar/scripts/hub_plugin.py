"""Hub capability lifecycle boundary for Agent runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, runtime_checkable

try:
    from src.hub_proxy import EphemeralHubProxy
except ModuleNotFoundError:
    from hub_proxy import EphemeralHubProxy


@dataclass(frozen=True)
class HubPluginConfig:
    """Current defaults keep Hub enabled and required."""

    enabled: bool = True
    required: bool = True
    workflow_create_definition_ids: tuple[int, ...] = ()
    workflow_run_receipt_callback: Callable[[Mapping], bool] | None = None
    wecom_reply_callback: Callable[[Mapping], Mapping | bool] | None = None
    allow_writes: bool = True
    approved_write_action: str | None = None


@runtime_checkable
class HubPlugin(Protocol):
    def start(self) -> "HubPlugin":
        """Start resources required by this invocation."""

    def environment(self) -> Mapping[str, str]:
        """Return non-persistent environment values for the Agent."""

    def close(self) -> None:
        """Release invocation-scoped resources."""


class HubProxyPlugin:
    """Adapt the existing ephemeral proxy to the Hub plugin contract."""

    def __init__(self, proxy):
        self._proxy = proxy

    def start(self) -> "HubProxyPlugin":
        self._proxy.start()
        return self

    def environment(self) -> Mapping[str, str]:
        return self._proxy.environment()

    def close(self) -> None:
        self._proxy.close()


class DisabledHubPlugin:
    """No-op boundary reserved for a later optional-Hub task."""

    def start(self) -> "DisabledHubPlugin":
        return self

    def environment(self) -> Mapping[str, str]:
        return {}

    def close(self) -> None:
        return None


def create_hub_plugin(
    config: HubPluginConfig,
    *,
    http_func,
    raw_http_func=None,
    redactor,
    proxy_factory=EphemeralHubProxy,
) -> HubPlugin:
    """Create the configured plugin without changing current defaults."""
    if not config.enabled:
        return DisabledHubPlugin()
    options = {
        "redactor": redactor,
        "workflow_create_definition_ids": (
            config.workflow_create_definition_ids
        ),
    }
    if not config.allow_writes:
        options["allow_writes"] = False
    if config.approved_write_action is not None:
        options["approved_write_action"] = config.approved_write_action
    if config.workflow_run_receipt_callback is not None:
        options["workflow_run_receipt_callback"] = (
            config.workflow_run_receipt_callback
        )
    if config.wecom_reply_callback is not None:
        options["wecom_reply_callback"] = config.wecom_reply_callback
    if raw_http_func is not None:
        options["raw_http_func"] = raw_http_func
    return HubProxyPlugin(proxy_factory(http_func, **options))
