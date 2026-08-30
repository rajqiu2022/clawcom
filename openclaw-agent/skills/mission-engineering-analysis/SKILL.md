---
name: mission-engineering-analysis
description: Analyze approved remote source snapshots for a schema v2 Mission Stage and produce an engineering_analysis v1 Artifact. Use for formal read-only engineering-impact analysis; do not use for code modification or unrestricted SSH.
---

# Mission Engineering Analysis

Produce a traceable engineering-impact Artifact from the exact Mission baseline and accepted requirement handoff.

## Source boundary

- Read source only through Worker-provided `remote_source_read` or another typed, approved reader in `effect_scope`.
- Use only `remote_source_id` values allowed by Hub.
- Bind every conclusion to the returned commit/tree digest.
- Never fall back to naked SSH, an unverified local checkout, an old prompt snapshot, or a broader path when the broker fails.
- This role is read-only. Do not edit, merge, commit, deploy, restart, or widen permissions.

## Analysis

1. Confirm repository IDs, commit and tree digests before interpreting code.
2. Map each affected module to concrete paths or symbols and Evidence references.
3. Identify dependencies, runtime entrypoints, protocols and resource dependencies that change test behavior.
4. Link acceptance criteria to code with `implements`, `guards`, `configures`, or `unknown`.
5. Keep observed facts, inferences and unknowns in separate fields.
6. Put an unresolvable requirement-to-code link in `unknowns`; do not manufacture a symbol.
7. Recommend tests from verified impact, not from directory names alone.

Read [references/engineering-artifact-v1.md](references/engineering-artifact-v1.md) before constructing the Artifact.

## Result

Return only the schema v2 outer result contract. A completed Artifact must match `engineering_analysis` schema 1 and the trusted `context_digest`. Use `blocked` when the approved reader, baseline, or accepted handoff is unavailable. Keep `experience_candidates` empty in P0.

