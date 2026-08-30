# execution_record v1

Required Artifact fields:

```json
{
  "schema": 1,
  "artifact_type": "execution_record",
  "environment_baseline": {
    "code_commit": "",
    "build_id": "",
    "runner_release": "",
    "adapter_release": "",
    "device_generation": ""
  },
  "case_results": [{
    "case_id": 0,
    "status": "passed|failed|blocked|skipped",
    "classification": "business|environment|tool|case",
    "started_at": "",
    "finished_at": "",
    "action_receipts": [],
    "observation_refs": [],
    "evidence_refs": [],
    "failure_fingerprint": "",
    "retry_count": 0
  }],
  "environment_failures": [],
  "tool_failures": [],
  "business_failures": [],
  "summary": "",
  "evidence_manifest_id": 0
}
```

Gates:

- Case IDs exactly equal the Mission-selected IDs and are unique.
- Environment fields equal the Mission-confirmed baseline.
- Passed cases have action receipts and observations.
- Failed cases have classification, Evidence and fingerprint.
- Blocked cases are not listed as business failures.
- A side-effect receipt explicitly declares the effect and contains idempotency key, lease ID and positive fencing token.

