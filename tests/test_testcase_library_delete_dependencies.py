from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "web" / "app" / "services" / "test_case_delete.py"
API = ROOT / "web" / "app" / "api" / "testcases.py"


def test_library_delete_cleans_restrict_dependents_and_preserves_history_records():
    source = SERVICE.read_text(encoding="utf-8")

    for model in (
        "TestCaseSnapshot",
        "TestCaseChangeLog",
        "TestCaseLibraryShare",
        "TestCaseLibraryReview",
    ):
        assert f"{model}.query.filter_by(library_id=library_id).delete(" in source

    for model, column in (
        ("TestTask", "library_id"),
        ("Topic", "review_library_id"),
        ("EngineeringTestImpactItem", "library_id"),
    ):
        assert f"{model}.query.filter_by({column}=library_id).update(" in source


def test_library_delete_route_uses_dependency_cleanup_without_orphan_snapshot():
    source = API.read_text(encoding="utf-8")
    route = source.split("def delete_testcase_library(library_id):", 1)[1].split(
        "@api_bp.route('/testcase-libraries/<int:library_id>/modules'", 1
    )[0]
    compact_route = " ".join(route.split())

    assert "cleanup_test_case_library_dependencies( library_id, case_ids, )" in compact_route
    assert "auto_snapshot(" not in route
    assert "db.session.delete(library)" in route
