"""Registration gate status report.

Usage:
  python _registration_gate_report.py --hub http://your-hub-host:8088 --token oc_tk_xxx --claws 6,10
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import requests


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check registration init-gate status")
    p.add_argument("--hub", required=True, help="Hub base URL")
    p.add_argument("--token", required=True, help="Admin claw token (e.g. DragonKing)")
    p.add_argument("--claws", default="", help="Comma-separated claw ids to filter, e.g. 6,10")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    headers = {"Authorization": f"Bearer {args.token}"}
    url = f"{args.hub.rstrip('/')}/api/v1/registration/init-tasks/status"
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()

    wanted = set()
    if args.claws.strip():
        wanted = {int(x.strip()) for x in args.claws.split(",") if x.strip()}

    print("=== Registration Gate Summary ===")
    print(json.dumps(data.get("summary", {}), ensure_ascii=False))
    print()

    claws = data.get("claws", {})
    for claw_name, info in claws.items():
        cid = int(info.get("claw_id", 0))
        if wanted and cid not in wanted:
            continue
        print(f"[{cid}] {claw_name}")
        print(f"  gate_passed={info.get('gate_passed')}")
        print(f"  pending_required_targets={info.get('pending_required_targets', [])}")
        target_status = info.get("target_status", {})
        for target in info.get("required_targets", []):
            print(f"    - {target}: {target_status.get(target, 'pending')}")

        submitted_waiting = [
            t for t in info.get("tasks", [])
            if t.get("verification_target") in info.get("required_targets", [])
            and (t.get("result_summary") or "")
            and t.get("status") == "pending"
        ]
        if submitted_waiting:
            print("  notes: found tasks with result but still pending; check submit/approve flow")
        print()

    if wanted and not claws:
        print("No claw data returned.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
