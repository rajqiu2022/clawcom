#!/usr/bin/env python3
"""Deploy the three full-chain Hub runtime fixes without overwriting remote hotfixes."""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import shlex
import subprocess
import sys
from datetime import datetime

from deploy_multiflow_p0_compat import exec_remote, merge_python, remote_read, remote_write


BASE_COMMIT = "3e4e0f2"
REMOTE_ROOT = "/opt/openclaw-web"
FILES = (
    "app/api/workflows.py",
    "app/services/workflow_result_ingestion.py",
)
REQUIRED_MARKERS = {
    "app/api/workflows.py": (
        "def _step_no_response_reminder_threshold(step):",
        "def _step_no_response_fallback_enabled(step):",
        ">= _step_no_response_reminder_threshold(step)",
        "progress_updated = _apply_step_progress(step, data, worker_id, now)",
    ),
    "app/services/workflow_result_ingestion.py": (
        "def _evidence_profile(run):",
        "def _apply_profile_coverage_defaults(run, coverage, explicit_coverage=None):",
        "coverage = _apply_profile_coverage_defaults(",
    ),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_file(workspace: str, revision: str, path: str) -> str:
    value = subprocess.check_output(
        ["git", "show", f"{revision}:web/{path}"], cwd=workspace
    )
    return value.decode("utf-8")


def _local_file(workspace: str, path: str) -> str:
    with open(os.path.join(workspace, "web", *path.split("/")), encoding="utf-8") as handle:
        return handle.read()


def _verify_markers(path: str, source: str) -> None:
    missing = [marker for marker in REQUIRED_MARKERS[path] if marker not in source]
    if missing:
        raise RuntimeError(f"{path} missing deployment markers: {missing}")


def main() -> int:
    import paramiko

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="9.134.11.169")
    parser.add_argument("--port", type=int, default=36000)
    parser.add_argument("--user", default="root")
    parser.add_argument("--key", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = f"/tmp/fullchain_hub_runtime_{stamp}"
    backup = f"{REMOTE_ROOT}/backups/fullchain_hub_runtime_{stamp}"
    switched = False

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host,
        port=args.port,
        username=args.user,
        key_filename=os.path.abspath(os.path.expanduser(args.key)),
        timeout=20,
    )
    sftp = client.open_sftp()
    try:
        merged: dict[str, bytes] = {}
        for path in FILES:
            remote_source = remote_read(sftp, f"{REMOTE_ROOT}/{path}").decode("utf-8")
            base_source = _git_file(workspace, BASE_COMMIT, path)
            local_source = _local_file(workspace, path)
            candidate = merge_python(remote_source, base_source, local_source)
            _verify_markers(path, candidate)
            merged[path] = candidate.encode("utf-8")

        exec_remote(client, f"mkdir -p {shlex.quote(stage)}")
        for path, data in merged.items():
            remote_write(sftp, f"{stage}/{path}", data)
        staged_python = " ".join(shlex.quote(f"{stage}/{path}") for path in FILES)
        exec_remote(
            client,
            f"{REMOTE_ROOT}/venv/bin/python -m py_compile {staged_python}",
            timeout=180,
        )
        print("DRY_RUN_OK", stage)
        for path, data in merged.items():
            print("STAGED", path, _sha256(data))
        if not args.apply:
            return 0

        exec_remote(client, f"mkdir -p {shlex.quote(backup)}")
        for path in FILES:
            remote_write(
                sftp,
                f"{backup}/{path}",
                remote_read(sftp, f"{REMOTE_ROOT}/{path}"),
            )
        for path, data in merged.items():
            remote_write(sftp, f"{REMOTE_ROOT}/{path}", data)
        switched = True

        live_python = " ".join(shlex.quote(f"{REMOTE_ROOT}/{path}") for path in FILES)
        exec_remote(
            client,
            f"{REMOTE_ROOT}/venv/bin/python -m py_compile {live_python}",
            timeout=180,
        )
        exec_remote(client, "systemctl restart openclaw-web", timeout=120)
        _, active, _ = exec_remote(client, "systemctl is-active openclaw-web", timeout=60)
        code, http_code, http_error = exec_remote(
            client,
            "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18800/",
            timeout=60,
            check=False,
        )
        if code or http_code.strip() not in ("200", "302"):
            raise RuntimeError(
                f"Hub health failed rc={code} http={http_code.strip()} {http_error}"
            )
        for path in FILES:
            source = remote_read(sftp, f"{REMOTE_ROOT}/{path}").decode("utf-8")
            _verify_markers(path, source)
        print("DEPLOY_OK", stamp, backup, active.strip(), http_code.strip())
        return 0
    except Exception:
        if switched:
            for path in FILES:
                remote_write(
                    sftp,
                    f"{REMOTE_ROOT}/{path}",
                    remote_read(sftp, f"{backup}/{path}"),
                )
            exec_remote(client, "systemctl restart openclaw-web", timeout=120)
            exec_remote(client, "systemctl is-active openclaw-web", timeout=60)
            print("ROLLBACK_OK", backup, file=sys.stderr)
        raise
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
