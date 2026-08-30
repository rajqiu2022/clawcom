param(
    [switch]$FullRegression
)

$ErrorActionPreference = 'Stop'
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$acceptanceTests = @(
    'tests/test_workflow_run_control_plane_api.py::WorkflowRunControlPlaneApiTest::test_at01_create_run_is_idempotent_per_definition',
    'tests/test_automation_case_candidates_api.py::AutomationCaseCandidatesApiTest::test_at02_stale_expected_version_is_rejected',
    'tests/test_automation_case_candidates_api.py::AutomationCaseCandidatesApiTest::test_at03_capability_gap_moves_candidate_and_creates_link',
    'tests/test_testcase_library_promotions_api.py::TestcaseLibraryPromotionsApiTest::test_at03_capability_gap_candidate_cannot_enter_library',
    'tests/test_testcase_library_promotions_api.py::TestcaseLibraryPromotionsApiTest::test_at04_idempotent_replay_publishes_once',
    'tests/test_testcase_library_promotions_api.py::TestcaseLibraryPromotionsApiTest::test_at05_stale_publisher_loses_and_can_dry_run_again',
    'tests/test_workflow_run_library_snapshots_api.py::WorkflowRunLibrarySnapshotsApiTest::test_at06_run_keeps_frozen_revision_after_library_upgrade',
    'tests/test_multiflow_closed_loop_acceptance.py::MultiFlowClosedLoopAcceptanceTest::test_at07_peripheral_failures_do_not_block_or_mutate_flow12',
    'tests/test_resource_leases_api.py::ResourceLeasesApiTest::test_at08_flow12_lease_blocks_qualification_without_changing_run',
    'tests/test_resource_leases_api.py::ResourceLeasesApiTest::test_at09_expired_group_is_reclaimed_and_history_is_preserved',
    'tests/test_workflow_evidence_manifests_api.py::WorkflowEvidenceManifestApiTest::test_at10_incomplete_evidence_rejects_no_risk_and_accepts_incomplete',
    'tests/test_entity_relations_api.py::EntityRelationsApiTest::test_at11_full_lineage_from_production_case_and_reverse',
    'tests/test_testcase_library_promotions_api.py::TestcaseLibraryPromotionsApiTest::test_at12_rollback_creates_new_revision_and_preserves_history'
)

Push-Location $repoRoot
try {
    Write-Host 'Running Hub multi-Flow AT-01 through AT-12 acceptance suite...'
    & python -m pytest @acceptanceTests -q
    if ($LASTEXITCODE -ne 0) {
        throw "Acceptance suite failed with exit code $LASTEXITCODE"
    }
    if ($FullRegression) {
        Write-Host 'Acceptance passed. Running full regression...'
        & python -m pytest tests -q
        if ($LASTEXITCODE -ne 0) {
            throw "Full regression failed with exit code $LASTEXITCODE"
        }
    }
}
finally {
    Pop-Location
}
