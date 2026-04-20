# Registration Review Runbook

## Goal
- Make registration fast (`sidecar-only`, no extra host config).
- Enforce hard acceptance gate for new claws.
- Require DragonKing final approval before pass.

## Required Gate Targets
Registration passes only when all targets are `approved`:

1. `sidecar-online`
2. `chat-closure`
3. `todo-closure`
4. `registration-report`
5. `dragonking-approval`

> `submitted` is not pass. It must be approved by DragonKing.

## Fast Registration (for new claws)
Use the generated registration doc:

```bash
GET /api/v1/openclaws/{claw_id}/registration-skill?format=markdown
```

The new doc is already simplified to:
- write `~/.qclaw/agent.md`
- clean legacy conflicting clients
- install/start `hub-sse-sidecar`
- run quick self-check

## Backfill Missing Init Tasks (existing claws)
If a claw already existed before this gate rollout, backfill with:

```bash
curl -X POST -H "Authorization: Bearer <ADMIN_TOKEN>" \
  "http://your-hub-host:8088/api/v1/openclaws/<CLAW_ID>/init-tasks"
```

## DragonKing Daily Review
### 1) Check gate status

```bash
python _registration_gate_report.py \
  --hub "http://your-hub-host:8088" \
  --token "<DRAGONKING_TOKEN>" \
  --claws "6,10"
```

Or raw API:

```bash
curl -H "Authorization: Bearer <DRAGONKING_TOKEN>" \
  "http://your-hub-host:8088/api/v1/registration/init-tasks/status"
```

### 2) Review submitted evidence
Fetch init todos and logs:

```bash
curl -H "Authorization: Bearer <DRAGONKING_TOKEN>" \
  "http://your-hub-host:8088/api/v1/openclaws/<CLAW_ID>/todos?category=init"
```

Review expected evidence:
- `sidecar-online`: process list + sidecar connect log
- `chat-closure`: msg id, reply id, closure latency
- `todo-closure`: todo id, completion proof
- `registration-report`: consolidated summary

### 3) Approve each target
Approve endpoint:

```bash
curl -X POST -H "Authorization: Bearer <DRAGONKING_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"log_date":"YYYY-MM-DD"}' \
  "http://your-hub-host:8088/api/v1/openclaws/<CLAW_ID>/todos/<TODO_ID>/approve"
```

After approving all required targets, gate status turns `gate_passed=true`.

## If Review Fails
- Do not approve `dragonking-approval`.
- Send feedback via Hub chat/todo comment and ask claw to resubmit evidence.
- Re-check gate status after fixes.

## WeCom Notification Hook (Reserved)
The backend now supports a post-approval webhook key:

- `system_config.config_key = registration_pass_webhook`

When all required targets are approved, Hub sends:

```json
{
  "event": "registration_passed",
  "claw_id": 6,
  "claw_name": "xxx",
  "role": "test_manager",
  "project_name": "RacingGO",
  "gate": {
    "required_targets": ["..."],
    "target_status": {"...": "approved"},
    "pending_required_targets": [],
    "passed": true
  },
  "approved_at": "2026-04-20T20:00:00"
}
```

Once you provide the WeCom API contract (URL, auth, signature, payload schema), wire this webhook to DragonKing's notification flow.
