import sys
import types
import unittest
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))


class _Noop:
    def __init__(self, *args, **kwargs): pass
    def __call__(self, *args, **kwargs): return self
    def __getattr__(self, _name): return _Noop()


for name, attrs in (
    ('flask_cors', {'CORS': _Noop}),
    ('flask_socketio', {
        'SocketIO': _Noop, 'emit': _Noop(),
        'join_room': _Noop(), 'leave_room': _Noop(),
    }),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items(): setattr(module, key, value)
        sys.modules[name] = module


from flask import Flask  # noqa: E402
from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (AuditLog, ClawSidecarConfig, OpenClawInstance, Project,  # noqa: E402
                        WorkflowDefinition, hash_token)


class WorkflowEditorAclApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret', TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        project = Project(name='Alpha'); db.session.add(project); db.session.flush()
        self.tokens = {'owner': 'hub_tk_owner', 'editor': 'hub_tk_editor',
                       'executor': 'hub_tk_executor'}
        self.owner = OpenClawInstance(
            name='Owner', safe_name='owner', claw_tag='owner', owner='a',
            project_id=project.id, project_name='Alpha',
            api_token_hash=hash_token(self.tokens['owner']))
        self.editor = OpenClawInstance(
            name='Editor', safe_name='editor', claw_tag='editor', owner='b',
            project_id=project.id, project_name='Alpha',
            api_token_hash=hash_token(self.tokens['editor']))
        self.executor = OpenClawInstance(
            name='Executor', safe_name='executor', claw_tag='executor', owner='c',
            project_id=project.id, project_name='Alpha',
            api_token_hash=hash_token(self.tokens['executor']))
        db.session.add_all([self.owner, self.editor, self.executor]); db.session.flush()
        self.editor_config = ClawSidecarConfig(
            claw_id=self.editor.id,
            agent_type='codex',
            config_version=3,
            system_context_policy_json={
                'allowed_workflow_create_definition_ids': [99],
                'codex_orchestrator': {
                    'enabled': True,
                    'session_key': 'test:editor',
                    'resume_on': ['blocked'],
                    'allowed_next_flows': [99],
                    'max_retries': 1,
                    'review_success': True,
                },
            },
        )
        self.executor_config = ClawSidecarConfig(
            claw_id=self.executor.id,
            agent_type='codex',
            config_version=5,
            system_context_policy_json={
                'allowed_workflow_create_definition_ids': [99],
            },
        )
        db.session.add_all([self.editor_config, self.executor_config])
        self.definition = WorkflowDefinition(
            workflow_key='editable-flow', name='Editable Flow',
            project_id=project.id, definition_json={
                'key': 'editable-flow', 'name': 'Editable Flow',
                'version': 1, 'steps': [{
                    'id': 'inspect', 'name': 'Inspect',
                    'type': 'agent_task', 'runner': 'agent.inspect',
                }],
            }, version=1, owner_type='claw', owner_id=self.owner.id,
            executor_acl_json={'claw_ids': [self.executor.id]},
            editor_acl_json={}, visibility_scope='project')
        db.session.add(self.definition); db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def headers(self, actor):
        return {'Authorization': 'Bearer ' + self.tokens[actor]}

    def test_owner_can_replace_editors_and_editor_can_patch_only(self):
        response = self.client.put(
            f'/api/v1/workflow-definitions/{self.definition.id}/editors',
            json={'claw_ids': [self.editor.id], 'user_ids': []},
            headers=self.headers('owner'))
        self.assertEqual(200, response.status_code, response.get_json())
        payload = response.get_json()
        self.assertEqual([self.editor.id], payload['editor_acl']['claw_ids'])
        self.assertEqual(2, payload['version'])
        self.assertEqual(1, AuditLog.query.filter_by(
            resource_type='workflow_definition_editors').count())

        self.definition.visibility_scope = 'private'
        db.session.commit()
        visible = self.client.get(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            headers=self.headers('editor'))
        self.assertEqual(200, visible.status_code, visible.get_json())
        self.assertTrue(visible.get_json()['can_execute'])

        config = ClawSidecarConfig.query.get(self.editor.id)
        self.assertEqual(
            [self.definition.id, 99],
            config.system_context_policy_json[
                'allowed_workflow_create_definition_ids'],
        )
        self.assertEqual(4, config.config_version)
        self.assertEqual(
            [self.definition.id, 99],
            config.system_context_policy_json['codex_orchestrator'][
                'allowed_next_flows'],
        )

        started = self.client.post(
            '/api/v1/workflow-runs',
            json={'workflow_definition_id': self.definition.id},
            headers=self.headers('editor'))
        self.assertEqual(201, started.status_code, started.get_json())

        response = self.client.patch(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            json={'description': 'edited by agent'},
            headers=self.headers('editor'))
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual('edited by agent', response.get_json()['description'])

        denied = self.client.patch(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            json={'visibility_scope': 'private'},
            headers=self.headers('editor'))
        self.assertEqual(403, denied.status_code)

        denied = self.client.put(
            f'/api/v1/workflow-definitions/{self.definition.id}/editors',
            json={'claw_ids': [], 'user_ids': []},
            headers=self.headers('editor'))
        self.assertEqual(403, denied.status_code)
        denied = self.client.delete(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            headers=self.headers('editor'))
        self.assertEqual(403, denied.status_code)

    def test_executor_does_not_implicitly_gain_edit_permission(self):
        response = self.client.patch(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            json={'description': 'must fail'},
            headers=self.headers('executor'))
        self.assertEqual(403, response.status_code)

    def test_adding_executor_syncs_sidecar_without_granting_edit(self):
        response = self.client.post(
            f'/api/v1/workflow-definitions/{self.definition.id}/executors',
            json={'claw_ids': [self.executor.id], 'user_ids': []},
            headers=self.headers('owner'))
        self.assertEqual(200, response.status_code, response.get_json())

        config = ClawSidecarConfig.query.get(self.executor.id)
        self.assertEqual(
            [self.definition.id, 99],
            config.system_context_policy_json[
                'allowed_workflow_create_definition_ids'],
        )
        self.assertEqual(6, config.config_version)
        self.assertTrue(response.get_json()['can_execute'])

        denied = self.client.patch(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            json={'description': 'executor cannot edit'},
            headers=self.headers('executor'))
        self.assertEqual(403, denied.status_code)

    def test_frontend_exposes_separate_editor_controls(self):
        template = (Path(__file__).resolve().parents[1] / 'web' / 'templates' /
                    'workflows.html').read_text(encoding='utf-8')
        self.assertIn('manageDefinitionEditors', template)
        self.assertIn('d.can_edit', template)
        self.assertIn('/editors', template)
        self.assertIn('管理编辑者（可编辑+执行）', template)
        self.assertIn('添加仅执行者', template)
        self.assertIn('workflowTemplateLabel', template)
        self.assertIn('模板：${esc(workflowTemplateLabel(run))}', template)
        self.assertIn('id="wf-catalog-filter"', template)
        self.assertIn("params.catalog_kind = selectedWorkflowCatalogKind", template)
        self.assertIn('<b>Evidence</b>', template)
        self.assertIn('<b>Report</b>', template)
        self.assertIn('<b>Delivery / Review</b>', template)
        self.assertIn('workflowCatalogChip(run)', template)


if __name__ == '__main__': unittest.main()
