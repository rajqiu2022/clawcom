"""Skill assignment is desired state; only a fenced receipt verifies installation."""
import unittest
from datetime import datetime

import test_skill_delivery_api as fixtures
from app import db
from app.models import OpenClawSkill, SkillFile, SkillInstallationReceipt, ClawTodo, ClawTodoLog, User
from app.services.skill_delivery import skill_bundle_descriptor
from app.services.skill_installation import reset_installation, installation_view


class SkillInstallationTest(unittest.TestCase):
    setUp = fixtures.SkillDeliveryApiTest.setUp
    tearDown = fixtures.SkillDeliveryApiTest.tearDown
    _seed = fixtures.SkillDeliveryApiTest._seed

    def link(self):
        return OpenClawSkill.query.filter_by(openclaw_id=self.claw.id, skill_id=self.skill.id).one()

    def body(self, state='verified', event='install-1'):
        descriptor = skill_bundle_descriptor(self.skill)
        body = {'generation': self.link().installation_generation, 'event_id': event, 'state': state,
                'bundle_sha256': descriptor['sha256'], 'content_version': descriptor['content_version']}
        if state == 'verified':
            body['files'] = {f['path']: f['sha256'] for f in descriptor['files']}
            body['runtime'] = {'claw_id': self.claw.id, 'instance_id': 'worker-self', 'skill_scope': 'instance',
                'manifest_sha256': descriptor['sha256'], 'loaded_sha256': descriptor['sha256'], 'reload_succeeded': True}
        return body

    def post(self, body=None, token=None):
        return self.client.post('/api/v1/openclaws/%s/skills/%s/installation-receipts' % (self.claw.id, self.skill.id),
            headers={'Authorization': 'Bearer ' + (token or self.token)}, json=body if body is not None else self.body())

    def test_legacy_assignment_time_is_not_installation_proof(self):
        self.link().installed_at = datetime(2099, 1, 1)
        db.session.commit()
        self.assertFalse(installation_view(self.link())['verified'])
        response = self.client.get('/api/v1/openclaws/%s/skill-manifest' % self.claw.id,
            headers={'Authorization': 'Bearer ' + self.token})
        skill = next(s for s in response.json['skills'] if s['id'] == self.skill.id)
        self.assertEqual(skill['installation']['state'], 'pending')
        self.assertIn('installation-receipts', skill['installation']['receipt_api'])

    def test_success_duplicate_and_no_downgrade(self):
        body = self.body()
        self.assertEqual(self.post(body).status_code, 200)
        timestamp = self.link().installation_verified_at
        replay = self.post(body)
        self.assertTrue(replay.json['replayed'])
        self.assertEqual(self.link().installation_verified_at, timestamp)
        self.assertEqual(SkillInstallationReceipt.query.count(), 1)
        self.assertEqual(self.post(self.body('syncing', 'late')).status_code, 409)
        self.assertTrue(installation_view(self.link())['verified'])

    def test_wrong_actor_and_unauthenticated_are_denied(self):
        self.assertEqual(self.post(token=self.other_token).status_code, 403)
        response = self.client.post('/api/v1/openclaws/%s/skills/%s/installation-receipts' % (self.claw.id, self.skill.id), json=self.body())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(SkillInstallationReceipt.query.count(), 0)

    def test_assignment_requires_admin_for_session_and_token(self):
        paths = [('/api/v1/openclaws/%s/skills' % self.claw.id, {'skill_id': self.skill.id}),
                 ('/api/v1/skills/%s/assign' % self.skill.id, {'openclaw_ids': [self.claw.id]})]
        for path, body in paths:
            self.assertEqual(self.client.post(path, json=body,
                headers={'Authorization': 'Bearer ' + self.token}).status_code, 403)
        user = User(username='ordinary-member', role='user', password_hash='unused')
        db.session.add(user)
        db.session.commit()
        with self.client.session_transaction() as session:
            session['user_id'] = user.id
        for path, body in paths:
            self.assertEqual(self.client.post(path, json=body).status_code, 403)

    def test_files_runtime_and_version_are_strict(self):
        for mutate in (
            lambda b: b.update(bundle_sha256='0' * 64),
            lambda b: b.update(content_version='old'),
            lambda b: b.update(files={}),
            lambda b: b['files'].update({'extra': '0' * 64}),
            lambda b: b['runtime'].update(claw_id=self.other_claw.id),
            lambda b: b['runtime'].update(skill_scope='global'),
            lambda b: b['runtime'].update(reload_succeeded=False),
            lambda b: b['runtime'].update(loaded_sha256='0' * 64),
            lambda b: b['runtime'].update(manifest_sha256='0' * 64),
        ):
            with self.subTest(mutate=mutate):
                body = self.body()
                mutate(body)
                self.assertEqual(self.post(body).status_code, 409)
                self.assertFalse(installation_view(self.link())['verified'])
        self.assertEqual(SkillInstallationReceipt.query.count(), 0)

    def test_reassign_and_unassign_fence_old_receipts(self):
        old = self.body()
        reset_installation(self.link())
        db.session.commit()
        self.assertEqual(self.post(old).json['code'], 'SKILL_INSTALLATION_STALE')
        self.link().enabled = False
        db.session.commit()
        self.assertEqual(self.post().status_code, 403)

    def test_content_edit_invalidates_verified_status_and_old_replay(self):
        old = self.body()
        self.assertEqual(self.post(old).status_code, 200)
        file = SkillFile.query.filter_by(skill_id=self.skill.id, filename='SKILL.md').one()
        file.content = 'changed'
        db.session.commit()
        self.assertFalse(installation_view(self.link())['verified'])
        self.assertEqual(self.post(old).json['code'], 'SKILL_CONTENT_STALE')

    def test_sync_failure_retry_and_conflicting_event(self):
        syncing = self.body('syncing', 'sync-1')
        self.assertEqual(self.post(syncing).status_code, 200)
        changed = dict(syncing, state='failed', error_code='RELOAD_FAILED')
        self.assertEqual(self.post(changed).json['code'], 'SKILL_RECEIPT_CONFLICT')
        changed['event_id'] = 'failed-1'
        self.assertEqual(self.post(changed).status_code, 200)
        self.assertEqual(installation_view(self.link())['state'], 'failed')
        self.assertEqual(self.post().status_code, 200)

    def test_todo_cannot_complete_without_verified_receipt(self):
        todo = ClawTodo(openclaw_id=self.claw.id, title='install', schedule_type='once',
                       verification_target='install-skill:' + self.skill.name, enabled=True)
        db.session.add(todo)
        db.session.flush()
        self.link().installation_todo_id = todo.id
        db.session.commit()
        path = '/api/v1/openclaws/%s/todos/%s/complete' % (self.claw.id, todo.id)
        headers = {'Authorization': 'Bearer ' + self.token}
        response = self.client.post(path, json={'result_summary': '安装成功'}, headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ClawTodoLog.query.filter_by(todo_id=todo.id).count(), 0)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.client.post(path, json={}, headers=headers).status_code, 200)
        self.assertFalse(todo.enabled)

    def test_admin_assignment_and_batch_do_not_verify(self):
        admin = User(username='skill-admin', role='super_admin', password_hash='unused')
        db.session.add(admin)
        db.session.commit()
        with self.client.session_transaction() as session:
            session['user_id'] = admin.id
        for path, body in (
            ('/api/v1/openclaws/%s/skills' % self.claw.id, {'skill_id': self.skill.id}),
            ('/api/v1/skills/%s/assign' % self.skill.id, {'openclaw_ids': [self.claw.id]}),
        ):
            old_generation = self.link().installation_generation
            old_todo_id = self.link().installation_todo_id
            response = self.client.post(path, json=body)
            self.assertIn(response.status_code, (200, 201), response.json)
            self.assertFalse(installation_view(self.link())['verified'])
            self.assertNotEqual(self.link().installation_generation, old_generation)
            todo = db.session.get(ClawTodo, self.link().installation_todo_id)
            self.assertIn('控制面任务', todo.description)
            self.assertNotIn('API_TOKEN=', todo.description)
            if old_todo_id:
                self.assertFalse(db.session.get(ClawTodo, old_todo_id).enabled)
                stale_path = '/api/v1/openclaws/%s/todos/%s/complete' % (self.claw.id, old_todo_id)
                self.assertEqual(self.client.post(stale_path, json={}).status_code, 409)

    def test_pending_assignment_and_revoked_review_never_count_as_verified(self):
        # No attempt to hash malformed files is needed for pending assignments.
        file = SkillFile.query.filter_by(skill_id=self.skill.id, filename='SKILL.md').one()
        file.filename = '../bad'
        db.session.commit()
        self.assertFalse(installation_view(self.link())['verified'])
        file.filename = 'SKILL.md'
        db.session.commit()
        self.assertEqual(self.post().status_code, 200)
        self.skill.review_status = 'pending'
        db.session.commit()
        self.assertFalse(installation_view(self.link())['verified'])
        self.assertEqual(self.post().status_code, 403)
