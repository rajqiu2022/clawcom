"""Validated, resource-scoped Codex permission grants.

The model may describe a missing permission, but only the Sidecar can turn a
validated request into a one-invocation Codex permission profile.  No approval
ever becomes ``danger-full-access``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


PERMISSION_REQUEST_TYPE = "permission_request"
PERMISSION_CAPABILITIES = frozenset(
    {"network_domain", "filesystem_read", "filesystem_write", "hub_action"}
)
PERMISSION_REASON_MAX_BYTES = 1024
PERMISSION_OPERATION_MAX_BYTES = 512
PERMISSION_RESOURCE_MAX_BYTES = 2048
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_HUB_ACTION_RE = re.compile(r"(POST|PUT|PATCH) (/api/[A-Za-z0-9._~!$&'()*+,;=:@/-]+)")
_SENSITIVE_PATH_PARTS = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".codex"}
)


def _bounded_text(value: Any, maximum: int, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text.encode("utf-8", "replace")) > maximum:
        raise ValueError(f"invalid Codex permission {field}")
    if "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError(f"invalid Codex permission {field}")
    return text


def _same_or_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalized_path(value: Any, *, require_directory: bool = False) -> Path:
    raw = _bounded_text(value, PERMISSION_RESOURCE_MAX_BYTES, "resource")
    candidate = Path(raw)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("Codex filesystem permission requires an absolute path")
    resolved = candidate.resolve(strict=True)
    if resolved.is_symlink() or not (resolved.is_dir() or resolved.is_file()):
        raise ValueError("Codex filesystem permission requires a regular path")
    if require_directory and not resolved.is_dir():
        raise ValueError("Codex workspace requires a regular directory")
    anchor = Path(resolved.anchor)
    if resolved == anchor:
        raise ValueError("Codex filesystem permission cannot target a drive root")
    user_profile = str(os.environ.get("USERPROFILE") or "").strip()
    if user_profile and resolved == Path(user_profile).resolve(strict=False):
        raise ValueError("Codex filesystem permission cannot target the user root")
    if any(part.casefold() in _SENSITIVE_PATH_PARTS for part in resolved.parts):
        raise ValueError("Codex filesystem permission cannot target a credential directory")
    return resolved


def _default_protected_roots() -> tuple[Path, ...]:
    """Roots that an owner may never add as an extra filesystem grant."""

    roots: list[Path] = []
    for name in (
        "SystemRoot",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "CODEX_HOME",
        "SIDECAR_INSTANCE_DIR",
    ):
        value = str(os.environ.get(name) or "").strip()
        if value:
            try:
                roots.append(Path(value).resolve(strict=False))
            except OSError:
                continue
    return tuple(roots)


def _profile_denied_roots() -> tuple[Path, ...]:
    """Credential/Worker roots denied even to the normal workspace profile.

    Windows and Program Files intentionally stay under ``:minimal`` read
    access so Codex can launch installed Git, Python and PowerShell binaries.
    They are still rejected as explicit extra filesystem grants by
    ``_default_protected_roots``.
    """

    roots: list[Path] = []
    for name in ("CODEX_HOME", "SIDECAR_INSTANCE_DIR"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            roots.append(Path(value).resolve(strict=False))
    user_profile = str(os.environ.get("USERPROFILE") or "").strip()
    if user_profile:
        profile_root = Path(user_profile).resolve(strict=False)
        roots.extend(
            profile_root / name
            for name in _SENSITIVE_PATH_PARTS
        )
    return tuple(roots)


def normalize_permission_request(
    value: Any,
    *,
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, str]:
    """Return the canonical grantable subset of a model permission request."""

    if not isinstance(value, Mapping) or set(value) - {
        "type",
        "capability",
        "resource",
        "reason",
        "operation",
    }:
        raise ValueError("invalid Codex permission request")
    if value.get("type") != PERMISSION_REQUEST_TYPE:
        raise ValueError("invalid Codex permission request type")
    capability = str(value.get("capability") or "").strip().lower()
    if capability not in PERMISSION_CAPABILITIES:
        raise ValueError("Codex permission capability is not grantable")
    reason = _bounded_text(
        value.get("reason"), PERMISSION_REASON_MAX_BYTES, "reason"
    )
    operation = _bounded_text(
        value.get("operation"), PERMISSION_OPERATION_MAX_BYTES, "operation"
    )
    if capability == "network_domain":
        resource = _bounded_text(
            value.get("resource"), PERMISSION_RESOURCE_MAX_BYTES, "resource"
        ).rstrip(".").casefold()
        if (
            not _DOMAIN_RE.fullmatch(resource)
            or "*" in resource
        ):
            try:
                ipaddress.ip_address(resource)
            except ValueError as exc:
                raise ValueError(
                    "Codex network permission requires one exact hostname or IP"
                ) from exc
    elif capability == "hub_action":
        resource = _bounded_text(
            value.get("resource"), PERMISSION_RESOURCE_MAX_BYTES, "resource"
        )
        match = _HUB_ACTION_RE.fullmatch(resource)
        if (
            match is None
            or ".." in match.group(2).split("/")
            or "//" in match.group(2)
        ):
            raise ValueError(
                "Codex Hub permission requires one exact write method and API path"
            )
        resource = f"{match.group(1)} {match.group(2)}"
    else:
        resource_path = _normalized_path(value.get("resource"))
        blocked = list(_default_protected_roots())
        for root in protected_roots:
            raw = str(root or "").strip()
            if raw:
                blocked.append(Path(raw).resolve(strict=False))
        if any(
            _same_or_child(resource_path, root)
            for root in blocked
        ):
            raise ValueError("Codex filesystem permission targets a protected root")
        resource = str(resource_path)
    return {
        "type": PERMISSION_REQUEST_TYPE,
        "capability": capability,
        "resource": resource,
        "reason": reason,
        "operation": operation,
    }


def permission_fingerprint(value: Mapping[str, Any]) -> str:
    canonical = normalize_permission_request(value)
    material = {
        key: canonical[key]
        for key in ("type", "capability", "resource", "operation")
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile_filesystem(
    workspace: str,
    *,
    workspace_access: str = "write",
    additional_workspaces: Iterable[str | os.PathLike[str]] = (),
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> tuple[Path, dict[str, Any]]:
    workspace_path = _normalized_path(workspace, require_directory=True)
    if workspace_access not in {"read", "write"}:
        raise ValueError("invalid base workspace access")
    root_rule = {
        ".": workspace_access,
        ".codex": "deny",
        ".agents": "read",
        "*.env": "deny",
        "*/*.env": "deny",
        "*/*/*.env": "deny",
    }
    filesystem: dict[str, Any] = {
        ":minimal": "read",
        str(workspace_path): dict(root_rule),
    }
    blocked_roots = (*_default_protected_roots(), *protected_roots)
    for value in additional_workspaces:
        raw = str(value or "").strip()
        if not raw:
            continue
        path = _normalized_path(raw, require_directory=True)
        if any(
            _same_or_child(path, Path(root).resolve(strict=False))
            for root in blocked_roots
            if str(root or "").strip()
        ):
            raise ValueError("Codex configured workspace targets a protected root")
        filesystem.setdefault(str(path), dict(root_rule))
    for root in (*_profile_denied_roots(), *protected_roots):
        raw = str(root or "").strip()
        if raw:
            path = Path(raw).resolve(strict=False)
            if not _same_or_child(workspace_path, path):
                filesystem[str(path)] = "deny"
    return workspace_path, filesystem


def workspace_permission_profile_config(
    *,
    workspace: str,
    workspace_access: str = "write",
    additional_workspaces: Iterable[str | os.PathLike[str]] = (),
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Build the normal Worker-owned Codex workspace permission profile."""

    _, filesystem = _profile_filesystem(
        workspace,
        workspace_access=workspace_access,
        additional_workspaces=additional_workspaces,
        protected_roots=protected_roots,
    )
    profile = {
        "description": "Worker-owned access to the configured project workspace.",
        "filesystem": filesystem,
        "network": {"enabled": False},
    }
    return {
        "approval_policy": "never",
        "default_permissions": "claw-workspace",
        "permissions": {"claw-workspace": profile},
    }


def permission_profile_config(
    grant: Mapping[str, Any],
    *,
    workspace: str,
    workspace_access: str = "write",
    additional_workspaces: Iterable[str | os.PathLike[str]] = (),
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Build a one-invocation beta permission profile for an approved grant."""

    request = normalize_permission_request(
        grant, protected_roots=protected_roots
    )
    workspace_path, filesystem = _profile_filesystem(
        workspace,
        workspace_access=workspace_access,
        additional_workspaces=additional_workspaces,
        protected_roots=protected_roots,
    )

    profile: dict[str, Any] = {
        "description": "One-invocation grant approved by the bound WeCom owner.",
        "filesystem": filesystem,
        "network": {"enabled": False},
    }
    config: dict[str, Any] = {
        "approval_policy": "never",
        "default_permissions": "claw-approved-once",
        "permissions": {"claw-approved-once": profile},
    }
    capability = request["capability"]
    if capability == "network_domain":
        config["features"] = {"network_proxy": True}
        resource = request["resource"]
        is_local = resource == "localhost"
        try:
            address = ipaddress.ip_address(resource)
            is_local = is_local or address.is_loopback or address.is_private
        except ValueError:
            pass
        profile["network"] = {
            "enabled": True,
            "allow_local_binding": is_local,
            "domains": {resource: "allow"},
        }
    elif capability != "hub_action":
        resource = request["resource"]
        # The normal workspace profile already grants this root while keeping
        # its credential/config carve-outs.  Do not replace the scoped map
        # with a broader scalar entry when a model redundantly requests it.
        if Path(resource) != workspace_path:
            filesystem[resource] = (
                "read" if capability == "filesystem_read" else "write"
            )
    return config
