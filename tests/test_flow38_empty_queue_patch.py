import os
import sys
import types


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
WEB = os.path.join(ROOT, 'web')
for path in (ROOT, WEB):
    if path not in sys.path:
        sys.path.insert(0, path)


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return _Noop()


def _stub(name, **attrs):
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from ops.patch_flow38_empty_queue_contract import (  # noqa: E402
    DOWNSTREAM_STEPS,
    TEMPLATE_REVISION,
    patch_definition,
)


def test_flow38_empty_queue_patch_is_branch_safe_and_idempotent():
    definition = {
        'key': 'racinggo_candidate_editor_qualification',
        'name': 'Flow 38',
        'version': 5,
        'context': {'evidence_profile': {}},
        'steps': [{
            'id': 'acquire_qualification_resources',
            'name': 'Acquire',
            'type': 'agent_task',
            'outputs': [
                'lease_ids', 'validation_allowed', 'defer_reason',
                'step_result', 'candidate_manifest_uri',
                'candidate_manifest_sha256', 'ready_candidate_keys',
                'resource_lease_ids',
            ],
        }] + [{
            'id': step_id, 'name': step_id, 'type': 'agent_task',
            'depends_on': ([
                'acquire_qualification_resources'
                if index == 0 else DOWNSTREAM_STEPS[index - 1]
            ]),
        } for index, step_id in enumerate(DOWNSTREAM_STEPS)],
    }

    patched = patch_definition(definition)
    replay = patch_definition(patched)
    acquire = patched['steps'][0]

    assert patched['version'] == 6
    assert replay == patched
    assert patched['context']['template_revision'] == TEMPLATE_REVISION
    assert patched['context']['evidence_profile']['not_applicable_is_complete'] is True
    assert acquire['contract_on_fail'] == 'warn'
    assert acquire['branches'] == [{
        'if': 'metrics.ready_candidate_count == 0',
        'then': [],
        'else': DOWNSTREAM_STEPS,
    }]
    assert 'lease_ids' not in acquire['required_outputs']
