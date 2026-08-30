# requirement_analysis v1

Required Artifact fields:

```json
{
  "schema": 1,
  "artifact_type": "requirement_analysis",
  "input_baseline_sha256": "sha256:...",
  "summary": "",
  "actors": [],
  "functional_flows": [],
  "acceptance_criteria": [{
    "id": "AC-001",
    "statement": "",
    "priority": "P0|P1|P2",
    "source_refs": [],
    "testable": true
  }],
  "risks": [{
    "id": "RISK-001",
    "description": "",
    "impact": "high|medium|low",
    "probability": "high|medium|low",
    "source_refs": [],
    "recommended_coverage": []
  }],
  "ambiguities": [],
  "open_questions": [],
  "out_of_scope": [],
  "evidence_refs": [],
  "confidence": "high|medium|low"
}
```

Gates:

- `summary` and `acceptance_criteria` are non-empty.
- Every acceptance criterion has at least one source reference.
- Criterion IDs and risk IDs are unique.
- A `testable=false` criterion is referenced by an ambiguity or open question.
- The content baseline equals both the envelope baseline and Mission frozen baseline.
- Evidence verification is a Hub/Runner fact; never invent `verified=true`.

