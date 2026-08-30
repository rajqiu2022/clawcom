# engineering_analysis v1

Required Artifact fields:

```json
{
  "schema": 1,
  "artifact_type": "engineering_analysis",
  "input_baseline_sha256": "sha256:...",
  "repositories": [{
    "source_id": "approved-source-id",
    "commit": "",
    "tree_digest": "sha256:..."
  }],
  "affected_modules": [],
  "changed_paths": [],
  "dependency_edges": [],
  "runtime_entrypoints": [],
  "protocols": [],
  "resource_dependencies": [],
  "testability_findings": [],
  "code_requirement_links": [{
    "acceptance_id": "AC-001",
    "path": "",
    "symbol": "",
    "relation": "implements|guards|configures|unknown",
    "evidence_refs": []
  }],
  "test_recommendations": [],
  "inferences": [],
  "unknowns": [],
  "evidence_refs": [],
  "confidence": "high|medium|low"
}
```

Gates:

- Every repository is Hub-approved and has a commit and SHA-256 tree digest.
- The content baseline equals the envelope and Mission baseline.
- Every affected module has path or symbol Evidence.
- Code links have an acceptance ID, location, relation and source Evidence.
- Facts, inferences and unknowns remain distinct.
- Broker failure is `blocked`; it never authorizes fallback access.

