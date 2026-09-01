import sys
import types
import unittest
from datetime import date
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return _Noop()


for name, attrs in {
    'flask_cors': {'CORS': _Noop},
    'flask_socketio': {
        'SocketIO': _Noop, 'emit': _Noop(),
        'join_room': _Noop(), 'leave_room': _Noop(),
    },
}.items():
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

from flask import Flask  # noqa: E402
from app import db  # noqa: E402
from app.models import (  # noqa: E402
    TestCase, TestCaseLibrary, TestPlan, TestTask, TestTaskCase,
)
from app.services.test_task_case_sync import (  # noqa: E402
    apply_sync, build_sync_plan, case_snapshot, enrich_case_filter,
    restore_backup, select_library_cases,
)


class TestTaskCaseSyncTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.library = TestCaseLibrary(name='同步测试库', status='active')
        self.plan = TestPlan(
            name='计划', start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 10))
        db.session.add_all([self.library, self.plan])
        db.session.flush()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _case(self, key, title, path, content=None, placeholder=False):
        row = TestCase(
            library_id=self.library.id, case_id=key, title=title,
            module_path=path, priority='P1', type='functional',
            content=content or {'steps': ['step'], 'expected_results': ['ok']},
            mindmap_node_id='node-' + key + '-' + title,
            is_placeholder=placeholder,
        )
        db.session.add(row)
        db.session.flush()
        return row

    def test_node_selection_is_directory_and_case_union(self):
        a = self._case('A', '目录用例', '主流程/登录')
        b = self._case('B', '独立用例', '活动')
        self._case('C', '未选择', '商城')
        selected = select_library_cases(self.library.id, {
            'selection_mode': 'nodes',
            'module_paths': ['主流程'],
            'case_ids': [b.id],
        })
        self.assertEqual({a.id, b.id}, {row.id for row in selected})

    def test_sync_preserves_matches_backs_up_and_restores_once(self):
        a = self._case('A', '登录', '主流程', {'steps': ['old']})
        b = self._case('B', '排行榜', '玩法')
        removed = self._case('X', '旧活动', '活动')
        task = TestTask(
            plan_id=self.plan.id, name='任务', library_id=self.library.id,
            case_filter={'selection_mode': 'nodes'}, status='in_progress')
        db.session.add(task)
        db.session.flush()
        for case, status in ((a, 'passed'), (b, 'blocked'), (removed, 'failed')):
            db.session.add(TestTaskCase(
                task_id=task.id, case_id=case.id, status=status,
                executed_by='tester', case_source_snapshot=case_snapshot(case)))
        db.session.commit()

        a.content = {'steps': ['new']}
        b.is_placeholder = True
        removed.is_placeholder = True
        b2 = self._case('B', '排行榜', '玩法')
        added = self._case('C', '新增商城', '商城')
        db.session.commit()

        sync_plan = build_sync_plan(task)
        self.assertEqual(sync_plan['summary'], {
            'current_total': 3,
            'latest_total': 3,
            'preserved': 2,
            'changed': 1,
            'relinked': 1,
            'added': 1,
            'removed': 1,
            'executed_results_affected': 2,
        })
        apply_sync(task, self.plan)
        db.session.commit()
        current = {row.case_id: row for row in task.task_cases.all()}
        self.assertEqual(current[a.id].status, 'passed')
        self.assertEqual(current[b2.id].status, 'blocked')
        self.assertEqual(current[added.id].status, 'pending')
        self.assertTrue(task.case_sync_backup_json)
        self.assertIsNone(task.case_sync_backup_restored_at)

        restored = restore_backup(task)
        db.session.commit()
        self.assertEqual(restored['restored_cases'], 3)
        old = {row.case_id: row for row in task.task_cases.all()}
        self.assertEqual(set(old), {a.id, b.id, removed.id})
        self.assertEqual(old[a.id].status, 'passed')
        self.assertEqual(old[b.id].status, 'blocked')
        self.assertEqual(old[removed.id].status, 'failed')
        with self.assertRaisesRegex(ValueError, '还原机会已使用'):
            restore_backup(task)

    def test_enriched_selection_can_relink_recreated_case(self):
        old = self._case('LOGIN-01', '登录成功', '主流程/登录')
        case_filter = enrich_case_filter(
            self.library.id,
            {'selection_mode': 'nodes', 'case_ids': [old.id]})
        old.is_placeholder = True
        replacement = self._case('LOGIN-01', '登录成功', '主流程/登录')
        db.session.commit()
        selected = select_library_cases(self.library.id, case_filter)
        self.assertEqual([replacement.id], [row.id for row in selected])


if __name__ == '__main__':
    unittest.main()
