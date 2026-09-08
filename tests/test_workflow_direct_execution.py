import sys
import types
import unittest
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


for name, attributes in (
    ('flask_cors', {'CORS': _Noop}),
    ('flask_socketio', {
        'SocketIO': _Noop,
        'emit': _Noop(),
        'join_room': _Noop(),
        'leave_room': _Noop(),
    }),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module

from app.services.workflow_direct_execution import (  # noqa: E402
    direct_execution_claim_allowed,
    direct_execution_lease,
    normalize_direct_execution_lease,
    workflow_step_claim_required,
    workflow_step_fencing_required,
)
from app.services.workflows import normalize_step  # noqa: E402


class WorkflowDirectExecutionTest(unittest.TestCase):
    def setUp(self):
        self.policy = {
            'schema': 1,
            'required': True,
            'scope': 'runner_operation',
            'lease_seconds': 240,
        }
        self.config = {'direct_execution_lease': self.policy}

    def test_normalizes_explicit_direct_runner_lease(self):
        self.assertEqual(
            self.policy, normalize_direct_execution_lease(self.policy))
        self.assertEqual(
            self.policy, direct_execution_lease('agent_task', self.config))
        self.assertTrue(workflow_step_claim_required('agent_task', self.config))
        self.assertTrue(workflow_step_fencing_required('agent_task', self.config))
        self.assertTrue(direct_execution_claim_allowed(
            'agent_task', self.config, 'runner_operation'))

    def test_legacy_agent_task_does_not_gain_claim_or_fencing(self):
        self.assertIsNone(direct_execution_lease('agent_task', {}))
        self.assertFalse(workflow_step_claim_required('agent_task', {}))
        self.assertFalse(workflow_step_fencing_required('agent_task', {}))
        self.assertFalse(direct_execution_claim_allowed(
            'agent_task', {}, 'runner_operation'))

    def test_worker_task_keeps_existing_claim_behavior(self):
        self.assertTrue(workflow_step_claim_required('worker_task', {}))
        self.assertFalse(workflow_step_fencing_required('worker_task', {}))
        self.assertTrue(workflow_step_fencing_required(
            'worker_task', {'require_fencing_token': True}))

    def test_invalid_policy_fails_definition_validation_and_runtime_enablement(self):
        for invalid in (
            {},
            {'schema': 2, 'required': True, 'scope': 'runner_operation'},
            {'schema': 1, 'required': False, 'scope': 'runner_operation'},
            {'schema': 1, 'required': True, 'scope': 'all_agent_actions'},
            {
                'schema': 1,
                'required': True,
                'scope': 'runner_operation',
                'lease_seconds': 10,
            },
            {
                'schema': 1,
                'required': True,
                'scope': 'runner_operation',
                'unexpected': True,
            },
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_direct_execution_lease(invalid)
                self.assertIsNone(direct_execution_lease(
                    'agent_task', {'direct_execution_lease': invalid}))

    def test_workflow_definition_normalization_preserves_valid_policy(self):
        normalized = normalize_step({
            'id': 'health',
            'name': 'Health',
            'type': 'agent_task',
            'direct_execution_lease': self.policy,
        }, 0)
        self.assertEqual(self.policy, normalized['direct_execution_lease'])
        with self.assertRaisesRegex(
                ValueError, r'steps\[0\]\.direct_execution_lease'):
            normalize_step({
                'id': 'health',
                'name': 'Health',
                'type': 'agent_task',
                'direct_execution_lease': {'schema': 1},
            }, 0)


if __name__ == '__main__':
    unittest.main()
