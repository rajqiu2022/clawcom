"""Project FK writes must persist, fail explicitly and preserve journal contracts."""
import unittest
from unittest.mock import patch

import test_knowledge_features_api as fixtures
from app import db
from app.models import KnowledgeEntry, Project


class KnowledgeProjectAssignmentTest(unittest.TestCase):
    setUp = fixtures.KnowledgeFeaturesApiTest.setUp
    tearDown = fixtures.KnowledgeFeaturesApiTest.tearDown
    _seed = fixtures.KnowledgeFeaturesApiTest._seed
    _login = fixtures.KnowledgeFeaturesApiTest._login
    _logout = fixtures.KnowledgeFeaturesApiTest._logout

    def setup_project(self):
        self._login(self.author)
        self.project_id = self.claw.project_id
        self.foreign = Project(name='Beta')
        db.session.add(self.foreign)
        db.session.commit()
        self.url = '/api/v1/knowledge/%s' % self.entry.id

    def test_update_roundtrip_and_database_not_only_display_name(self):
        self.setup_project()
        self.assertIsNone(self.entry.project_id)
        result = self.client.put(self.url, json={'project_id': self.project_id})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['project_id'], self.project_id)
        db.session.expire_all()
        self.assertEqual(db.session.get(KnowledgeEntry, self.entry.id).project_id, self.project_id)
        self.assertEqual(self.client.get(self.url).get_json()['project_name'], 'Alpha')
        self.assertEqual(self.client.get(self.url).get_json()['project_id'], self.project_id)

    def test_agent_can_bind_same_project_without_admin(self):
        self.setup_project()
        self._logout()
        result = self.client.put(self.url, json={'project_id': self.project_id, 'project_name': 'Alpha'},
                                 headers={'Authorization': 'Bearer ' + self.agent_token})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['project_id'], self.project_id)

    def test_invalid_ids_and_mismatching_name_do_not_partially_write(self):
        self.setup_project()
        original_title = self.entry.title
        for value in (True, 0, -1, '1', 1.5, [], {}, 2147483648):
            response = self.client.put(self.url, json={'project_id': value, 'title': 'must not save'})
            self.assertEqual(response.status_code, 400, value)
            self.assertEqual(response.get_json()['code'], 'KNOWLEDGE_PROJECT_INVALID')
        response = self.client.put(self.url, json={'project_id': 999999})
        self.assertEqual(response.status_code, 404)
        response = self.client.put(self.url, json={'project_id': self.project_id, 'project_name': 'Beta'})
        self.assertEqual(response.get_json()['code'], 'KNOWLEDGE_PROJECT_MISMATCH')
        db.session.refresh(self.entry)
        self.assertIsNone(self.entry.project_id)
        self.assertEqual(self.entry.title, original_title)

    def test_target_and_original_project_access_checked(self):
        self.setup_project()
        result = self.client.put(self.url, json={'project_id': self.foreign.id})
        self.assertEqual(result.status_code, 403)
        self.entry.project_id = self.foreign.id
        self.entry.project_name = self.foreign.name
        db.session.commit()
        for payload in ({'project_id': self.project_id}, {'project_id': None}):
            self.assertEqual(self.client.put(self.url, json=payload).status_code, 403)
        # Legacy name-only source is also checked when attaching a project ID.
        self.entry.project_id = None
        db.session.commit()
        self.assertEqual(self.client.put(self.url, json={'project_id': self.project_id}).status_code, 403)

    def test_admin_can_move_and_clear_binding_explicitly(self):
        self.setup_project()
        self.author.role = 'super_admin'
        db.session.commit()
        moved = self.client.put(self.url, json={'project_id': self.foreign.id})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.get_json()['project_name'], 'Beta')
        cleared = self.client.put(self.url, json={'project_id': None})
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.get_json()['project_id'])
        self.assertIsNone(cleared.get_json()['project_name'])

    def test_omitted_id_preserves_binding_and_name_cannot_diverge(self):
        self.setup_project()
        self.client.put(self.url, json={'project_id': self.project_id})
        result = self.client.put(self.url, json={'title': '新标题'})
        self.assertEqual(result.get_json()['project_id'], self.project_id)
        self.assertEqual(self.client.put(self.url, json={'project_name':'Beta'}).status_code, 400)
        self.assertEqual(self.client.put(self.url, json={'project_name':'Alpha'}).status_code, 200)
        self.assertEqual(self.client.put(self.url, json={'project_id':None,'project_name':'Alpha'}).status_code, 400)

    def test_legacy_name_only_remains_compatible_without_inventing_fk(self):
        self.setup_project()
        response = self.client.put(self.url, json={'project_name': 'Alpha'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()['project_id'])

    def test_create_with_id_persists_and_rejects_foreign(self):
        self.setup_project()
        payload = {'title':'new knowledge', 'content':'body','status':'draft','project_id':self.project_id}
        response = self.client.post('/api/v1/knowledge', json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['project_id'], self.project_id)
        self.assertEqual(response.get_json()['project_name'], 'Alpha')
        self.assertEqual(self.client.post('/api/v1/knowledge', json=dict(payload,project_id=self.foreign.id)).status_code, 403)

    def test_batch_prevalidates_all_entries_then_saves_project_fk(self):
        self.setup_project()
        item = {'title':'imported','content':'body','project_id':self.project_id}
        before = KnowledgeEntry.query.count()
        with patch('app.api.skills._notify_admin_claws', return_value=[]) as notify, \
                patch('app.api.skills._create_review_todo_for_admin_claws'):
            invalid = self.client.post('/api/v1/knowledge/batch-import', json={'entries':[
                item, dict(item,project_id=self.foreign.id)]})
            self.assertEqual(invalid.status_code, 403)
            self.assertEqual(KnowledgeEntry.query.count(), before)
            notify.assert_not_called()
            response = self.client.post('/api/v1/knowledge/batch-import', json={'entries':[item]})
            self.assertEqual(response.status_code, 200, response.get_json())
        row = KnowledgeEntry.query.filter_by(title='imported').one()
        self.assertEqual(row.project_id, self.project_id)
        self.assertEqual(row.project_name, 'Alpha')
        self.assertEqual(row.created_by, 'alice')

    def test_journal_still_requires_revision_and_anonymous_denied(self):
        self.setup_project()
        self.entry.entry_type = 'test_journal'
        self.entry.project_id = self.project_id
        db.session.commit()
        response = self.client.put(self.url, json={'project_id':self.project_id})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'VERSIONED_KNOWLEDGE_REQUIRES_REVISION')
        self._logout()
        self.assertEqual(self.client.put(self.url, json={'project_id':self.project_id}).status_code, 401)


if __name__ == '__main__':
    unittest.main()
