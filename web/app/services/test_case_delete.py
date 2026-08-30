"""Remove FK dependents before deleting test_cases rows."""

from app import db
from app.models import (
    AutomationCaseCandidate,
    EngineeringTestImpactItem,
    TestCaseChangeLog,
    TestCaseLibraryReview,
    TestCaseLibraryShare,
    TestCaseSnapshot,
    TestCaseLibraryPromotion,
    TestCaseLibraryRevision,
    TestTask,
    Topic,
    TestCasePanoramaLink,
    TestTaskCase,
    EngineeringTestCaseLink,
    RequirementTestcaseLink,
)


def cleanup_test_case_dependencies(case_ids):
    """
    Delete rows referencing test_cases.id so case/library deletion won't 500.

    Returns module_ids affected by removed per-case panorama links (for metrics recalc).
    """
    ids = sorted({int(x) for x in case_ids if x is not None})
    if not ids:
        return []

    case_links = TestCasePanoramaLink.query.filter(
        TestCasePanoramaLink.case_pk.in_(ids),
        TestCasePanoramaLink.link_level == 'case',
    ).all()
    affected_module_ids = [link.module_id for link in case_links]

    TestCasePanoramaLink.query.filter(
        TestCasePanoramaLink.case_pk.in_(ids),
        TestCasePanoramaLink.link_level == 'case',
    ).delete(synchronize_session=False)

    TestTaskCase.query.filter(TestTaskCase.case_id.in_(ids)).delete(
        synchronize_session=False,
    )
    EngineeringTestCaseLink.query.filter(
        EngineeringTestCaseLink.test_case_id.in_(ids),
    ).delete(synchronize_session=False)
    RequirementTestcaseLink.query.filter(
        RequirementTestcaseLink.test_case_id.in_(ids),
    ).delete(synchronize_session=False)
    AutomationCaseCandidate.query.filter(
        AutomationCaseCandidate.production_case_id.in_(ids),
    ).update(
        {'production_case_id': None},
        synchronize_session=False,
    )

    db.session.flush()
    return affected_module_ids


def cleanup_test_case_library_dependencies(library_id, case_ids):
    """
    清理用例库删除时的强依赖，并解除应保留历史记录中的库关联。

    快照、变更日志、共享和评审记录依附于用例库，随库删除；
    测试任务、评审话题和工程影响项是独立历史记录，仅将外键置空。
    """
    affected_module_ids = set(cleanup_test_case_dependencies(case_ids))

    library_links = TestCasePanoramaLink.query.filter_by(
        library_id=library_id,
    ).all()
    affected_module_ids.update(link.module_id for link in library_links)
    TestCasePanoramaLink.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )

    TestCaseSnapshot.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )
    TestCaseChangeLog.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )
    TestCaseLibraryShare.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )
    TestCaseLibraryReview.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )
    # Formal promotion rows reference revisions only through audit strings, so
    # remove audit operations first and immutable snapshots second.
    TestCaseLibraryPromotion.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )
    TestCaseLibraryRevision.query.filter_by(library_id=library_id).delete(
        synchronize_session=False,
    )
    AutomationCaseCandidate.query.filter_by(
        production_library_id=library_id,
    ).update(
        {'production_library_id': None, 'production_case_id': None},
        synchronize_session=False,
    )

    TestTask.query.filter_by(library_id=library_id).update(
        {'library_id': None},
        synchronize_session=False,
    )
    Topic.query.filter_by(review_library_id=library_id).update(
        {'review_library_id': None},
        synchronize_session=False,
    )
    EngineeringTestImpactItem.query.filter_by(library_id=library_id).update(
        {'library_id': None},
        synchronize_session=False,
    )

    db.session.flush()
    return sorted(affected_module_ids)
