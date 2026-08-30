---
name: mission-requirement-analysis
description: Analyze a frozen game requirement inside a schema v2 Mission Stage and produce a requirement_analysis v1 Artifact. Use for formal requirement-analysis stages; do not use for TAPD synchronization or editing requirements.
---

# Mission Requirement Analysis

Convert the accepted Mission inputs into an auditable `requirement_analysis` Artifact. The output is analysis, not a rewrite of TAPD or a product decision.

## Required inputs

Proceed only when the task provides a schema v2 Mission envelope with:

- Mission and Stage versions;
- role/profile version accepted by Worker;
- frozen input baseline;
- accepted input Artifact/Handoff references when this is not the first Stage;
- result contract `requirement_analysis` schema 1;
- effect scope, fencing token, deadline, and trusted `context_digest`.

If any trusted input is absent or contradictory, return `blocked`; do not fill gaps from memory or an old snapshot.

## Analysis

1. Read every supplied source reference and keep claims traceable to those references.
2. Identify actors and end-to-end functional flows before listing isolated test points.
3. Turn explicit product statements into uniquely identified acceptance criteria.
4. Mark each criterion P0/P1/P2 and whether it is testable.
5. Put every untestable criterion into `ambiguities` or `open_questions` using its criterion ID.
6. Record risks separately from requirements. Give each risk a unique ID, impact, probability, source references, and recommended coverage.
7. Keep assumptions out of facts. Missing product decisions remain open questions.

Read [references/requirement-artifact-v1.md](references/requirement-artifact-v1.md) before constructing the Artifact.

## Result

Return only the schema v2 outer result contract. For `completed`, `artifact` must be a complete `requirement_analysis` v1 object and `context_digest` must exactly equal the trusted digest. Keep `experience_candidates` empty in P0.

Use `blocked` when sources, baseline, or product decisions prevent a valid Artifact. Include the unresolved items in `open_questions`; do not call incomplete analysis completed.

Do not place credentials, raw chat history, binary data, or long copied documents in the result. Use Hub Evidence references.

