import sys
import types
import unittest
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))


def _stub(name, **attrs):
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    CaseReviewComment,
    CaseReviewNodeMark,
    CaseReviewRound,
    CollaborationSession,
    OpenClawInstance,
    Project,
    ShiftLeftFinding,
    ShiftLeftFindingEvent,
    TestCase,
    TestCaseLibrary,
    TestIteration as _IterationModel,
    TestReport as _ReportModel,
    User,
    Topic,
    hash_token,
)


class ShiftLeftApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
            SHIFT_LEFT_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        self.project = Project(name='RacingGO')
        db.session.add(self.project)
        db.session.flush()
        self.admin = User(username='qa_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add(self.admin)
        db.session.flush()
        self.agent_token = 'shift-left-agent-token'
        self.agent = OpenClawInstance(
            name='analysis-agent',
            safe_name='analysis-agent',
            claw_tag='claw-analysis-agent',
            owner=self.admin.username,
            role='module_owner',
            project_id=self.project.id,
            api_token_hash=hash_token(self.agent_token),
        )
        db.session.add(self.agent)
        db.session.flush()
        self.iteration = _IterationModel(
            name='RacingGO v1',
            version_name='v1',
            project_id=self.project.id,
            status='active',
            created_by=self.admin.username,
        )
        db.session.add(self.iteration)
        db.session.flush()
        self.report = _ReportModel(
            title='RacingGO 代码分析',
            report_type='workflow',
            project_id=self.project.id,
            iteration_id=self.iteration.id,
            content='<main>agent custom report</main>',
            format='html',
            status='published',
            submitter_type='user',
            submitter_user_id=self.admin.id,
            submitter_name=self.admin.username,
        )
        db.session.add(self.report)
        self.case_library = TestCaseLibrary(
            name='RacingGO 核心用例', project_name=self.project.name,
            owner=self.admin.username)
        db.session.add(self.case_library)
        db.session.flush()
        self.review_case = TestCase(
            library_id=self.case_library.id, case_id='RG-001',
            title='登录成功', module_path='登录/账号',
            content={'steps': [{'action': '输入账号', 'expected': '登录成功'}]})
        self.outside_case = TestCase(
            library_id=self.case_library.id, case_id='RG-002',
            title='支付成功', module_path='支付', content={'steps': []})
        db.session.add_all([self.review_case, self.outside_case])
        db.session.flush()
        self.review_topic = Topic(
            title='登录用例评审', content='请评审登录用例', board='case_review',
            author_user_id=self.admin.id, author_name=self.admin.username,
            project_name=self.project.name, review_library_id=self.case_library.id,
            review_case_ids=[self.review_case.id], review_status='reviewing')
        self.other_review_topic = Topic(
            title='其他评审', content='其他范围', board='case_review',
            author_user_id=self.admin.id, author_name=self.admin.username,
            project_name=self.project.name, review_library_id=self.case_library.id,
            review_case_ids=[self.outside_case.id], review_status='reviewing')
        db.session.add_all([self.review_topic, self.other_review_topic])
        db.session.flush()
        self.review_round = CaseReviewRound(
            topic_id=self.review_topic.id, round_number=1,
            description='首轮评审', case_content='cases: [RG-001]',
            status='pending', submitted_by=self.admin.username)
        db.session.add(self.review_round)
        db.session.commit()

    def _analysis_body(self):
        return {
            'project_id': self.project.id,
            'client_repo': 'client',
            'client_base_sha': 'a' * 40,
            'client_target_sha': 'b' * 40,
            'server_repo': 'server',
            'server_base_sha': 'c' * 40,
            'server_target_sha': 'd' * 40,
            'config_digest': 'cfg-v1',
            'proto_digest': 'proto-v1',
            'requirement_revision': 'req-10',
            'iteration_id': self.iteration.id,
            'report_id': self.report.id,
        }

    def _create_analysis(self, key='analysis-create-1'):
        return self.client.post(
            '/api/v1/shift-left/analysis-runs',
            json=self._analysis_body(),
            headers={'Idempotency-Key': key},
        )

    def _create_finding(self, analysis_run_id, key='finding-upsert-1'):
        return self.client.put(
            '/api/v1/shift-left/findings:upsert',
            json={
                'project_id': self.project.id,
                'analysis_run_id': analysis_run_id,
                'finding_key': 'racinggo:playerkv:guide-data:key-mismatch',
                'title': 'PlayerKV 客户端与服务端 Key 不一致',
                'module': 'PlayerData',
                'severity': 'high',
                'confidence': 0.96,
                'evidence_level': 'contract_confirmed',
                'source_type': 'contract_analysis',
                'evidence': {
                    'client_key': 64401,
                    'server_key': 64400,
                },
            },
            headers={'Idempotency-Key': key},
        )

    def _transition(self, finding_id, action, revision, key, **extra):
        body = {'action': action, 'from_revision': revision,
                'reason': extra.pop('reason', action)}
        body.update(extra)
        return self.client.post(
            f'/api/v1/shift-left/findings/{finding_id}/transitions',
            json=body,
            headers={'Idempotency-Key': key},
        )

    def test_analysis_create_is_idempotent_and_reuses_same_baseline(self):
        first = self._create_analysis()
        replay = self._create_analysis()
        reused = self._create_analysis(key='analysis-create-2')

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 201, replay.get_data(as_text=True))
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(reused.status_code, 200, reused.get_data(as_text=True))
        self.assertTrue(reused.get_json()['reused'])
        self.assertEqual(first.get_json()['id'], reused.get_json()['id'])

    def test_finding_is_structured_and_report_lists_same_finding(self):
        analysis = self._create_analysis().get_json()
        created = self._create_finding(analysis['id'])
        replay = self._create_finding(analysis['id'])
        report_findings = self.client.get(
            f'/api/v1/test-reports/{self.report.id}/findings')

        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(replay.status_code, 201, replay.get_data(as_text=True))
        self.assertEqual(created.get_json(), replay.get_json())
        self.assertEqual(report_findings.status_code, 200)
        self.assertEqual(report_findings.get_json()['total'], 1)
        self.assertEqual(
            report_findings.get_json()['items'][0]['finding_key'],
            created.get_json()['finding_key'])
        iteration_findings = self.client.get(
            '/api/v1/shift-left/findings'
            f'?project_id={self.project.id}&iteration_id={self.iteration.id}')
        self.assertEqual(iteration_findings.status_code, 200)
        self.assertEqual(iteration_findings.get_json()['total'], 1)

    def test_collaboration_token_reads_report_and_writes_review_api_only(self):
        analysis = self._create_analysis().get_json()
        finding = self._create_finding(analysis['id']).get_json()

        triaged = self._transition(
            finding['id'], 'triage', finding['revision'], 'triage-1')
        self.assertEqual(triaged.status_code, 200, triaged.get_data(as_text=True))
        confirmed = self._transition(
            finding['id'], 'confirm', triaged.get_json()['revision'], 'confirm-1')
        self.assertEqual(confirmed.status_code, 200,
                         confirmed.get_data(as_text=True))

        create = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'project_id': self.project.id,
                'subject_type': 'analysis_report',
                'subject_id': self.report.id,
                'agent_identity': 'developer-ai:racinggo-client',
            },
            headers={'Idempotency-Key': 'collaboration-create-1'},
        )
        self.assertEqual(create.status_code, 201, create.get_data(as_text=True))
        invitation = create.get_json()['invitation_code']
        replay_create = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'project_id': self.project.id,
                'subject_type': 'analysis_report',
                'subject_id': self.report.id,
                'agent_identity': 'developer-ai:racinggo-client',
            },
            headers={'Idempotency-Key': 'collaboration-create-1'},
        )
        self.assertEqual(replay_create.status_code, 200)
        self.assertIsNone(replay_create.get_json()['invitation_code'])

        with self.client.session_transaction() as sess:
            sess.clear()
        exchange = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation},
        )
        self.assertEqual(exchange.status_code, 200, exchange.get_data(as_text=True))
        token = exchange.get_json()['access_token']
        auth = {'Authorization': f'Bearer {token}'}

        context = self.client.get(
            f'/api/v1/test-reports/{self.report.id}/analysis-context',
            headers=auth,
        )
        self.assertEqual(context.status_code, 200, context.get_data(as_text=True))
        self.assertIn('agent custom report', context.get_json()['report']['content'])

        denied_path = self.client.get('/api/v1/projects', headers=auth)
        self.assertEqual(denied_path.status_code, 403)
        self.assertEqual(denied_path.get_json()['code'],
                         'COLLABORATION_PATH_DENIED')

        denied_evidence = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/evidence",
            json={'summary': 'attempted self-elevation',
                  'evidence_level': 'human_confirmed'},
            headers=dict(auth, **{'Idempotency-Key': 'dev-ai-evidence-1'}),
        )
        self.assertEqual(denied_evidence.status_code, 403)
        self.assertEqual(denied_evidence.get_json()['code'],
                         'COLLABORATION_PATH_DENIED')

        comment_headers = dict(auth, **{'Idempotency-Key': 'dev-ai-comment-1'})
        comment = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/comments",
            json={'content': '确认双端常量不一致',
                  'comment_type': 'developer_ai_review'},
            headers=comment_headers,
        )
        comment_replay = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/comments",
            json={'content': '确认双端常量不一致',
                  'comment_type': 'developer_ai_review'},
            headers=comment_headers,
        )
        self.assertEqual(comment.status_code, 201, comment.get_data(as_text=True))
        self.assertEqual(comment.get_json(), comment_replay.get_json())
        self.assertEqual(ShiftLeftFindingEvent.query.filter_by(
            finding_id=finding['id'], event_type='comment').count(), 1)

        decision = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/review-decisions",
            json={
                'decision': 'confirmed',
                'root_cause': '双端常量不一致',
                'suggested_fix': '统一协议生成常量',
                'confidence': 0.96,
            },
            headers=dict(auth, **{'Idempotency-Key': 'dev-ai-decision-1'}),
        )
        self.assertEqual(decision.status_code, 201,
                         decision.get_data(as_text=True))

        wrong_revision = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'acknowledge', 'from_revision': 1,
                  'reason': '已确认'},
            headers=dict(auth, **{'Idempotency-Key': 'dev-ai-ack-wrong'}),
        )
        self.assertEqual(wrong_revision.status_code, 409)
        current = self.client.get(
            f"/api/v1/shift-left/findings/{finding['id']}", headers=auth)
        acknowledge = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'acknowledge',
                  'from_revision': current.get_json()['revision'],
                  'reason': 'Developer AI 已确认'},
            headers=dict(auth, **{'Idempotency-Key': 'dev-ai-ack-1'}),
        )
        self.assertEqual(acknowledge.status_code, 200,
                         acknowledge.get_data(as_text=True))
        self.assertEqual(acknowledge.get_json()['status'], 'dev_acknowledged')

        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id
        session_id = create.get_json()['id']
        revoked = self.client.post(
            f'/api/v1/collaboration-sessions/{session_id}/revoke',
            json={'reason': 'review completed'},
            headers={'Idempotency-Key': 'collaboration-revoke-1'},
        )
        self.assertEqual(revoked.status_code, 200,
                         revoked.get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess.clear()
        expired = self.client.get(
            f'/api/v1/test-reports/{self.report.id}/analysis-context',
            headers=auth,
        )
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.get_json()['code'],
                         'COLLABORATION_TOKEN_INVALID')
        self.assertEqual(db.session.get(CollaborationSession, session_id).status,
                         'revoked')

    def test_case_review_collaboration_is_scoped_commentable_and_markable(self):
        created = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'case_review',
                'subject_id': self.review_topic.id,
                'agent_identity': 'developer-ai:racinggo-case-review',
            },
            headers={'Idempotency-Key': 'case-review-session-1'},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(created.get_json()['project_id'], self.project.id)
        self.assertEqual(
            created.get_json()['scopes'],
            ['case_review:read', 'case_review:comment', 'case_review:mark'])
        invitation = created.get_json()['invitation_code']

        with self.client.session_transaction() as sess:
            sess.clear()
        exchange = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation},
        )
        token = exchange.get_json()['access_token']
        auth = {'Authorization': f'Bearer {token}'}

        context = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/context',
            headers=auth)
        self.assertEqual(context.status_code, 200, context.get_data(as_text=True))
        self.assertEqual(context.get_json()['open_round_id'], self.review_round.id)
        cases = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/cases',
            headers=auth)
        self.assertEqual(cases.status_code, 200, cases.get_data(as_text=True))
        self.assertEqual(cases.get_json()['total'], 1)
        self.assertEqual(cases.get_json()['items'][0]['id'], self.review_case.id)

        wrong_subject = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.other_review_topic.id}/context',
            headers=auth)
        self.assertEqual(wrong_subject.status_code, 403)
        self.assertEqual(wrong_subject.get_json()['code'],
                         'COLLABORATION_SUBJECT_DENIED')

        comment_headers = dict(
            auth, **{'Idempotency-Key': 'case-review-comment-1'})
        comment_body = {'content': '登录步骤缺少失败态覆盖', 'score': 7}
        comment = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/comments',
            json=comment_body, headers=comment_headers)
        replay = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/comments',
            json=comment_body, headers=comment_headers)
        self.assertEqual(comment.status_code, 201, comment.get_data(as_text=True))
        self.assertEqual(comment.get_json(), replay.get_json())
        self.assertEqual(CaseReviewComment.query.filter_by(
            round_id=self.review_round.id).count(), 1)

        decision_denied = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/comments',
            json={'content': '尝试直接通过', 'verdict': 'approve'},
            headers=dict(auth, **{'Idempotency-Key': 'case-review-decision-1'}))
        self.assertEqual(decision_denied.status_code, 403)
        self.assertEqual(decision_denied.get_json()['code'],
                         'COLLABORATION_SCOPE_DENIED')

        mark_headers = dict(auth, **{'Idempotency-Key': 'case-review-mark-1'})
        mark_body = {'marks': [{
            'node_id': f'case:{self.review_case.id}',
            'mark': 'question', 'note': '补失败态',
        }]}
        marked = self.client.put(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/marks',
            json=mark_body, headers=mark_headers)
        mark_replay = self.client.put(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/marks',
            json=mark_body, headers=mark_headers)
        self.assertEqual(marked.status_code, 200, marked.get_data(as_text=True))
        self.assertEqual(marked.get_json(), mark_replay.get_json())
        self.assertEqual(CaseReviewNodeMark.query.filter_by(
            topic_id=self.review_topic.id).count(), 1)

    def test_agent_cannot_confirm_its_own_finding_or_verify_its_own_fix(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        auth = {'Authorization': f'Bearer {self.agent_token}'}
        analysis = self.client.post(
            '/api/v1/shift-left/analysis-runs',
            json=self._analysis_body(),
            headers=dict(auth, **{'Idempotency-Key': 'agent-analysis-1'}),
        ).get_json()
        created = self.client.put(
            '/api/v1/shift-left/findings:upsert',
            json={
                'project_id': self.project.id,
                'analysis_run_id': analysis['id'],
                'finding_key': 'agent-owned-finding',
                'title': 'Agent generated finding',
                'severity': 'high',
                'confidence': 0.9,
                'evidence_level': 'contract_confirmed',
            },
            headers=dict(auth, **{'Idempotency-Key': 'agent-finding-1'}),
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        finding = created.get_json()
        triaged = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'triage', 'from_revision': finding['revision']},
            headers=dict(auth, **{'Idempotency-Key': 'agent-triage-1'}),
        )
        self.assertEqual(triaged.status_code, 200, triaged.get_data(as_text=True))
        self_confirm = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'confirm',
                  'from_revision': triaged.get_json()['revision']},
            headers=dict(auth, **{'Idempotency-Key': 'agent-confirm-1'}),
        )
        self.assertEqual(self_confirm.status_code, 403)
        self.assertEqual(self_confirm.get_json()['code'],
                         'INDEPENDENT_CONFIRMATION_REQUIRED')

        # Human review may confirm it; the same Agent may then fix it, but not close it.
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id
        confirmed = self._transition(
            finding['id'], 'confirm', triaged.get_json()['revision'],
            'human-confirm-1')
        self.assertEqual(confirmed.status_code, 200, confirmed.get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess.clear()
        acknowledged = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'acknowledge',
                  'from_revision': confirmed.get_json()['revision']},
            headers=dict(auth, **{'Idempotency-Key': 'agent-ack-1'}),
        )
        fixing = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'start_fix',
                  'from_revision': acknowledged.get_json()['revision']},
            headers=dict(auth, **{'Idempotency-Key': 'agent-start-fix-1'}),
        )
        ready = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'submit_fix',
                  'from_revision': fixing.get_json()['revision'],
                  'fix_ref': 'commit:abc123'},
            headers=dict(auth, **{'Idempotency-Key': 'agent-submit-fix-1'}),
        )
        self.assertEqual(ready.status_code, 200, ready.get_data(as_text=True))
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id
        reverifying = self._transition(
            finding['id'], 'start_reverify', ready.get_json()['revision'],
            'human-start-reverify-1')
        with self.client.session_transaction() as sess:
            sess.clear()
        self_close = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/transitions",
            json={'action': 'close',
                  'from_revision': reverifying.get_json()['revision'],
                  'fix_ref': 'commit:abc123',
                  'verification_ref': 'run:test-1'},
            headers=dict(auth, **{'Idempotency-Key': 'agent-close-1'}),
        )
        self.assertEqual(self_close.status_code, 403)
        self.assertEqual(self_close.get_json()['code'],
                         'INDEPENDENT_VERIFICATION_REQUIRED')
        self.assertEqual(db.session.get(ShiftLeftFinding, finding['id']).status,
                         'reverifying')

    def test_feature_flag_hides_new_api_without_touching_existing_routes(self):
        self.app.config['SHIFT_LEFT_ENABLED'] = False
        response = self._create_analysis()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['code'], 'SHIFT_LEFT_DISABLED')


if __name__ == '__main__':
    unittest.main()
