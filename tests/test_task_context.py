import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / 'web'
SERVICES_ROOT = WEB_ROOT / 'app' / 'services'
for p in (str(REPO_ROOT), str(WEB_ROOT), str(SERVICES_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name: str, rel_path: str):
    module_path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Pre-load experience_trigger_service under its bare name so task_context's
# fallback import (from experience_trigger_service import ...) resolves.
_load('experience_trigger_service',
      'web/app/services/experience_trigger_service.py')
task_context = _load('task_context', 'web/app/services/task_context.py')


class TaskContextTest(unittest.TestCase):
    def test_build_task_context_shape(self):
        ctx = task_context.build_task_context_payload(
            title='TAPD 提单', description='上传缺陷截图', project='RacingGO',
            pitfall_source=lambda *a, **k: [],
        )
        for key in ('project', 'required_skills', 'primary_skill',
                    'top_pitfalls', 'preflight_checklist', 'references'):
            self.assertIn(key, ctx)
        self.assertTrue(ctx['preflight_checklist'])
        self.assertIn('basic-operations-preflight', ctx['required_skills'])

    def test_top_pitfalls_truncated(self):
        fake = [
            {'id': i, 'title': f'坑{i}', 'content': 'Hub ' * 100,
             'solution': 'x' * 300, 'keywords': ['Hub'], 'status': 'active'}
            for i in range(1, 6)
        ]
        ctx = task_context.build_task_context_payload(
            title='调用 Hub API', description='同步', project='RacingGO',
            pitfall_source=lambda project, module, terms, limit: fake[:limit],
        )
        self.assertLessEqual(len(ctx['top_pitfalls']), 3)
        for p in ctx['top_pitfalls']:
            self.assertLessEqual(len(p['solution']), 120)

    def test_heartbeat_memory_inject_dedups_and_limits(self):
        fake = [
            {'id': 1, 'title': 'Hub 坑A', 'content': 'Hub', 'solution': 'a',
             'keywords': ['Hub'], 'status': 'active'},
            {'id': 2, 'title': 'Hub 坑B', 'content': 'Hub', 'solution': 'b',
             'keywords': ['Hub'], 'status': 'active'},
        ]
        out = task_context.build_heartbeat_memory_inject(
            ['调用 Hub API', '再次 Hub 操作'],  # 同源命中，应去重
            project='RacingGO',
            pitfall_source=lambda project, module, terms, limit: fake,
            limit=5,
        )
        ids = [a['id'] for a in out['pitfall_alerts']]
        self.assertEqual(sorted(ids), [1, 2])
        self.assertEqual(out['unread_pitfall_count'], 2)

    def test_heartbeat_memory_inject_empty_titles(self):
        out = task_context.build_heartbeat_memory_inject(
            [], project='RacingGO',
            pitfall_source=lambda *a, **k: [])
        self.assertEqual(out['pitfall_alerts'], [])
        self.assertEqual(out['unread_pitfall_count'], 0)

    def test_references_from_pitfalls(self):
        fake = [{'id': 42, 'title': 'Hub 坑', 'content': 'Hub',
                 'solution': 's', 'keywords': ['Hub'], 'status': 'active'}]
        ctx = task_context.build_task_context_payload(
            title='调用 Hub API', description='', project='RacingGO',
            pitfall_source=lambda project, module, terms, limit: fake,
        )
        self.assertTrue(any(r['type'] == 'knowledge' and r['id'] == 42
                            for r in ctx['references']))


if __name__ == '__main__':
    unittest.main()
