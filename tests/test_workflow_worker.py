import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = (
    Path(__file__).resolve().parents[1] /
    'openclaw-agent' / 'skills' / 'workflow-manager' / 'scripts' / 'workflow_worker.py'
)
_SPEC = importlib.util.spec_from_file_location('workflow_worker', _MODULE_PATH)
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


class WorkflowWorkerTest(unittest.TestCase):
    def test_heartbeat_path_uses_run_and_step_ids(self):
        task = {'run_id': 12, 'step_id': 'downloadandinstall'}
        self.assertEqual(
            worker.heartbeat_path(task),
            '/api/v1/workflow-runs/12/steps/downloadandinstall/heartbeat',
        )

    def test_progress_path_prefers_payload_api(self):
        task = {
            'run_id': 12,
            'step_id': 'downloadandinstall',
            'progress_api': '/api/v1/workflow-runs/12/steps/downloadandinstall/progress',
        }
        self.assertEqual(
            worker.progress_path(task),
            '/api/v1/workflow-runs/12/steps/downloadandinstall/progress',
        )

    def test_heartbeat_loop_posts_once_and_exits_when_stopped(self):
        calls = []

        def fake_http(method, path, body=None, timeout=30):
            calls.append((method, path, body, timeout))
            return 200, {'ok': True}

        class StopAfterFirstWait:
            def wait(self, interval):
                return True

        worker.heartbeat_loop(
            {'run_id': 12, 'step_id': 'precheck'},
            StopAfterFirstWait(),
            interval_sec=30,
            http_func=fake_http,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'POST')
        self.assertEqual(
            calls[0][1],
            '/api/v1/workflow-runs/12/steps/precheck/heartbeat',
        )

    def test_parse_result_uses_json_stdout(self):
        result = worker.parse_result(
            '{"status":"passed","summary":"ok","metrics":{"failed":0}}',
            0,
            123,
        )
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['summary'], 'ok')
        self.assertEqual(result['metrics']['failed'], 0)
        self.assertEqual(result['logs']['worker_elapsed_ms'], 123)

    def test_parse_result_blocks_nonzero_plain_output(self):
        result = worker.parse_result('boom', 2, 123)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['metrics']['returncode'], 2)
        self.assertIn('runner_command_failed', result['blocker']['type'])


if __name__ == '__main__':
    unittest.main()
