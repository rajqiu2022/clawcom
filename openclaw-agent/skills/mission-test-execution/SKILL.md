---
name: mission-test-execution
description: Execute a frozen set of game test cases through typed Runner operations and produce an execution_record v1 Artifact. Use for formal Mission execution stages; do not use for test design, manual claims of success, or uncontrolled device actions.
---

# Mission Test Execution

Execute exactly the cases selected in the Mission and report what typed Runner receipts and observations prove.

## Execution boundary

- Use only Runner/Adapter operations explicitly present in `effect_scope`.
- Every side-effecting action requires the task idempotency key, active lease and current fencing token.
- Confirm code commit, build, Runner, Adapter and device generation before the first case.
- Never replace a missing Runner observation with model text or a screenshot description.
- Do not publish a formal report, defect, release decision, or external notification unless a separate Hub gate authorizes it.

## Per-case recording

1. Execute each selected case once under the frozen environment baseline; retries are explicit and counted.
2. A passed case has at least one Runner action receipt and one observation reference.
3. A failed case has classification, Evidence and a stable failure fingerprint.
4. A blocked case records the environmental/tool/input blocker and is not a business failure.
5. A skipped case explains the governing constraint; omission is not skipping.
6. Separate business, environment, tool and case failures. Never improve a metric by relabeling failures.

Read [references/execution-artifact-v1.md](references/execution-artifact-v1.md) before constructing the Artifact.

## Result

Return only the schema v2 outer result contract. `case_results` must exactly cover the Mission-selected case IDs. A completed Artifact must match `execution_record` schema 1 and the trusted `context_digest`. Use `blocked` when the environment cannot produce valid execution evidence. Keep `experience_candidates` empty in P0.

