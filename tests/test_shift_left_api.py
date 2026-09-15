import sys
import types
import unittest
from datetime import datetime, timedelta
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
    CollaborationSessionEvent,
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
    TopicReply,
    hash_token,
)
from app.services.shift_left import collaboration_submission_identity  # noqa: E402


class ShiftLeftApiTest(unittest.TestCase):
    def test_submission_identity_never_falls_back_to_invite_or_legacy_token(self):
        legacy = types.SimpleNamespace(
            parent_invite_id=None,
            agent_identity='developer-ai:racinggo:topic48-guest-01',
        )
        with self.assertRaisesRegex(ValueError, 'identity 必填'):
            collaboration_submission_identity(legacy, {})
        self.assertEqual(
            collaboration_submission_identity(
                legacy, {'identity': 'Codex-自动化执行'}),
            'Codex-自动化执行',
        )

    def test_topics_api_imports_collaboration_request_context(self):
        source = (_WEB / 'app' / 'api' / 'topics.py').read_text(
            encoding='utf-8')
        self.assertIn(
            'from flask import current_app, g, request, jsonify, session as flask_session',
            source)

    def test_topic_payload_exposes_runtime_collaboration_gate_and_server_permission(self):
        response = self.client.get(
            f'/api/v1/topics/{self.discussion_topic.id}')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['external_collaboration_enabled'])
        self.assertTrue(
            response.get_json()['can_manage_external_collaboration'])

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
        self.discussion_topic = Topic(
            title='登录恢复方案讨论', content='请讨论登录恢复边界',
            board='test_methods', author_user_id=self.admin.id,
            author_name=self.admin.username, project_name=self.project.name,
            status='open', visibility='assigned')
        self.other_discussion_topic = Topic(
            title='支付方案讨论', content='其他课题', board='test_methods',
            author_user_id=self.admin.id, author_name=self.admin.username,
            project_name=self.project.name, status='open')
        self.projectless_topic = Topic(
            title='跨项目方法讨论', content='不绑定项目的课题',
            board='test_methods', author_user_id=self.admin.id,
            author_name=self.admin.username, status='open')
        db.session.add_all([
            self.review_topic, self.other_review_topic,
            self.discussion_topic, self.other_discussion_topic,
            self.projectless_topic,
        ])
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

        feedback_current = self.client.get(
            f"/api/v1/shift-left/findings/{finding['id']}", headers=auth)
        feedback = self.client.post(
            f"/api/v1/shift-left/findings/{finding['id']}/feedback",
            json={
                'label': 'true_positive',
                'from_revision': feedback_current.get_json()['revision'],
                'note': 'Developer AI 根据双端常量和报告证据确认命中',
                'evidence_refs': ['hub-artifact://analysis/constants-diff'],
            },
            headers=dict(auth, **{'Idempotency-Key': 'dev-ai-feedback-1'}),
        )
        self.assertEqual(feedback.status_code, 201,
                         feedback.get_data(as_text=True))
        self.assertEqual(feedback.get_json()['source_type'], 'developer_ai')

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
        listed = self.client.get(
            '/api/v1/collaboration-sessions'
            f'?subject_type=case_review&subject_id={self.review_topic.id}')
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertEqual(listed.get_json()['total'], 1)
        self.assertEqual(
            listed.get_json()['items'][0]['agent_identity'],
            'developer-ai:racinggo-case-review')
        invitation = created.get_json()['invitation_code']

        with self.client.session_transaction() as sess:
            sess.clear()
        exchange = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation},
        )
        self.assertEqual(exchange.status_code, 200, exchange.get_data(as_text=True))
        exchange_payload = exchange.get_json()
        token = exchange_payload['access_token']
        bootstrap = exchange_payload['bootstrap']
        self.assertEqual(bootstrap['schema_version'], 'case-review-bootstrap.v1')
        self.assertEqual(bootstrap['authorization']['access_token'], token)
        self.assertIn(
            f'/case-reviews/{self.review_topic.id}/context',
            bootstrap['endpoints']['context'])
        self.assertIn(
            f'/case-reviews/{self.review_topic.id}/reviews',
            bootstrap['endpoints']['reviews'])
        self.assertIn(
            f'/case-reviews/{self.review_topic.id}/mindmap',
            bootstrap['endpoints']['mindmap'])
        self.assertEqual(bootstrap['mirror_path'],
                         '/developer-ai/case-review')
        self.assertIn('review_contract', bootstrap)
        self.assertNotIn('case_review:decision', bootstrap['scopes'])
        auth = {'Authorization': f'Bearer {token}'}

        context = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/context',
            headers=auth)
        self.assertEqual(context.status_code, 200, context.get_data(as_text=True))
        self.assertEqual(context.get_json()['open_round_id'], self.review_round.id)
        self.assertEqual(
            context.get_json()['review_summary']['submitted_review_count'], 0)
        self.assertIn('reviews', context.get_json()['links'])
        cases = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/cases',
            headers=auth)
        self.assertEqual(cases.status_code, 200, cases.get_data(as_text=True))
        self.assertEqual(cases.get_json()['total'], 1)
        self.assertEqual(cases.get_json()['items'][0]['id'], self.review_case.id)

        mindmap = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/mindmap',
            headers=auth)
        self.assertEqual(mindmap.status_code, 200,
                         mindmap.get_data(as_text=True))
        self.assertEqual(mindmap.get_json()['total_case_count'], 1)
        self.assertTrue(mindmap.get_json()['can_mark'])

        case_detail = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}'
            f'/cases/{self.review_case.id}', headers=auth)
        self.assertEqual(case_detail.status_code, 200,
                         case_detail.get_data(as_text=True))
        self.assertEqual(case_detail.get_json()['case_id'], 'RG-001')
        out_of_scope = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}'
            f'/cases/{self.outside_case.id}', headers=auth)
        self.assertEqual(out_of_scope.status_code, 404,
                         out_of_scope.get_data(as_text=True))

        wrong_subject = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.other_review_topic.id}/context',
            headers=auth)
        self.assertEqual(wrong_subject.status_code, 403)
        self.assertEqual(wrong_subject.get_json()['code'],
                         'COLLABORATION_SUBJECT_DENIED')

        comment_headers = dict(
            auth, **{'Idempotency-Key': 'case-review-comment-1'})
        comment_body = {
            'identity': '外部用例评审 Agent',
            'content': '登录步骤缺少失败态覆盖',
            'score': 7,
        }
        comment = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/reviews',
            json=comment_body, headers=comment_headers)
        replay = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/reviews',
            json=comment_body, headers=comment_headers)
        self.assertEqual(comment.status_code, 201, comment.get_data(as_text=True))
        self.assertEqual(comment.get_json(), replay.get_json())
        self.assertEqual(CaseReviewComment.query.filter_by(
            round_id=self.review_round.id).count(), 1)
        review_id = comment.get_json()['id']
        self.assertTrue(comment.get_json()['owned_by_me'])

        records = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/reviews',
            headers=auth)
        self.assertEqual(records.status_code, 200,
                         records.get_data(as_text=True))
        self.assertEqual(records.get_json()['total'], 1)
        self.assertTrue(records.get_json()['items'][0]['owned_by_me'])

        updated = self.client.patch(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}'
            f'/reviews/{review_id}',
            json={'content': '补充失败态覆盖和可观测证据', 'score': 9},
            headers=dict(auth, **{
                'Idempotency-Key': 'case-review-record-update-1'}))
        self.assertEqual(updated.status_code, 200,
                         updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()['score'], 9)
        self.assertTrue(updated.get_json()['is_edited'])
        self.assertTrue(updated.get_json()['can_modify'])

        decision_denied = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/comments',
            json={'identity': '外部用例评审 Agent',
                  'content': '尝试直接通过', 'verdict': 'approve'},
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

        deleted = self.client.delete(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}'
            f'/reviews/{review_id}',
            headers=dict(auth, **{
                'Idempotency-Key': 'case-review-record-delete-1'}))
        self.assertEqual(deleted.status_code, 200,
                         deleted.get_data(as_text=True))
        self.assertTrue(deleted.get_json()['deleted'])
        records_after_delete = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/reviews',
            headers=auth)
        self.assertEqual(records_after_delete.get_json()['total'], 0)
        context_after_delete = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/context',
            headers=auth)
        self.assertEqual(
            context_after_delete.get_json()['review_summary']
            ['submitted_review_count'], 0)
        self.assertEqual(
            context_after_delete.get_json()['rounds'][0]['comments'], [])

    def test_topic_collaboration_is_single_subject_idempotent_and_owned(self):
        created = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'topic',
                'subject_id': self.discussion_topic.id,
                'agent_identity': 'developer-ai:racinggo:topic-alice',
                'invitation_ttl_minutes': 4320,
                'token_ttl_minutes': 4320,
            },
            headers={'Idempotency-Key': 'topic-collaboration-create-1'},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(created.get_json()['project_id'], self.project.id)
        self.assertEqual(created.get_json()['scopes'],
                         ['topic:read', 'topic:reply'])
        session_id = created.get_json()['id']
        invitation = created.get_json()['invitation_code']

        pending_deadline = datetime.now() + timedelta(hours=60)
        extended = self.client.patch(
            f'/api/v1/collaboration-sessions/{session_id}/deadline',
            json={'expires_at': pending_deadline.isoformat(timespec='seconds')},
            headers={'Idempotency-Key': 'topic-collaboration-deadline-1'},
        )
        self.assertEqual(extended.status_code, 200,
                         extended.get_data(as_text=True))
        self.assertFalse(extended.get_json()['link_rotated'])

        with self.client.session_transaction() as sess:
            sess.clear()
        exchanged = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation})
        self.assertEqual(exchanged.status_code, 200,
                         exchanged.get_data(as_text=True))
        package = exchanged.get_json()['bootstrap']
        self.assertEqual(package['schema_version'],
                         'topic-discussion-bootstrap.v1')
        self.assertIn(f'/topics/{self.discussion_topic.id}',
                      package['endpoints']['topic'])
        token = exchanged.get_json()['access_token']
        auth = {'Authorization': f'Bearer {token}'}

        topic = self.client.get(
            f'/api/v1/topics/{self.discussion_topic.id}', headers=auth)
        self.assertEqual(topic.status_code, 200, topic.get_data(as_text=True))
        self.assertTrue(topic.get_json()['external_collaboration'])
        other = self.client.get(
            f'/api/v1/topics/{self.other_discussion_topic.id}', headers=auth)
        self.assertEqual(other.status_code, 403, other.get_data(as_text=True))
        listing = self.client.get('/api/v1/topics', headers=auth)
        self.assertEqual(listing.status_code, 403, listing.get_data(as_text=True))
        self.assertEqual(listing.get_json()['code'], 'COLLABORATION_PATH_DENIED')

        missing_key = self.client.post(
            f'/api/v1/topics/{self.discussion_topic.id}/replies',
            json={'content': '先补充登录态恢复的失败路径'}, headers=auth)
        self.assertEqual(missing_key.status_code, 400,
                         missing_key.get_data(as_text=True))
        reply_headers = dict(auth, **{
            'Idempotency-Key': 'topic-collaboration-reply-1'})
        reply_body = {
            'identity': '外部课题讨论 Agent',
            'content': '需要覆盖 Token 过期后的静默恢复与重新登录。',
        }
        replied = self.client.post(
            f'/api/v1/topics/{self.discussion_topic.id}/replies',
            json=reply_body, headers=reply_headers)
        replay = self.client.post(
            f'/api/v1/topics/{self.discussion_topic.id}/replies',
            json=reply_body, headers=reply_headers)
        self.assertEqual(replied.status_code, 201, replied.get_data(as_text=True))
        self.assertEqual(replied.get_json(), replay.get_json())
        self.assertTrue(replied.get_json()['owned_by_me'])
        self.assertTrue(replied.get_json()['external_collaboration'])
        self.assertEqual(TopicReply.query.filter_by(
            topic_id=self.discussion_topic.id).count(), 1)
        reply_id = replied.get_json()['id']

        changed = self.client.patch(
            f'/api/v1/topics/{self.discussion_topic.id}/replies/{reply_id}',
            json={'content': '补充：覆盖 Token 过期、顶号和断网恢复。'},
            headers=dict(auth, **{
                'Idempotency-Key': 'topic-collaboration-reply-update-1'}))
        self.assertEqual(changed.status_code, 200,
                         changed.get_data(as_text=True))
        self.assertTrue(changed.get_json()['owned_by_me'])

        readback = self.client.get(
            f'/api/v1/topics/{self.discussion_topic.id}', headers=auth)
        self.assertTrue(readback.get_json()['replies'][0]['owned_by_me'])
        self.assertIn('顶号', readback.get_json()['replies'][0]['content'])

        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id
        second = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'topic',
                'subject_id': self.discussion_topic.id,
                'agent_identity': 'developer-ai:racinggo:topic-bob',
            },
            headers={'Idempotency-Key': 'topic-collaboration-create-2'})
        with self.client.session_transaction() as sess:
            sess.clear()
        second_exchange = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': second.get_json()['invitation_code']})
        second_auth = {
            'Authorization': 'Bearer ' + second_exchange.get_json()['access_token'],
            'Idempotency-Key': 'topic-cross-session-update-1',
        }
        denied_update = self.client.patch(
            f'/api/v1/topics/{self.discussion_topic.id}/replies/{reply_id}',
            json={'content': 'Bob overwrite attempt'}, headers=second_auth)
        self.assertEqual(denied_update.status_code, 403,
                         denied_update.get_data(as_text=True))
        self.assertEqual(denied_update.get_json()['code'],
                         'TOPIC_REPLY_NOT_OWNER')

        deleted = self.client.delete(
            f'/api/v1/topics/{self.discussion_topic.id}/replies/{reply_id}',
            headers=dict(auth, **{
                'Idempotency-Key': 'topic-collaboration-reply-delete-1'}))
        self.assertEqual(deleted.status_code, 200,
                         deleted.get_data(as_text=True))
        self.assertTrue(deleted.get_json()['deleted'])

    def test_topic_collaboration_link_issues_independent_token_sessions(self):
        created = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'topic',
                'subject_id': self.discussion_topic.id,
                'invitation_ttl_minutes': 4320,
                'token_ttl_minutes': 120,
            },
            headers={'Idempotency-Key': 'topic-collaboration-renewal-create'},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        invitation = created.get_json()['invitation_code']
        session_id = created.get_json()['id']

        with self.client.session_transaction() as sess:
            sess.clear()
        preview = self.client.post(
            '/api/v1/collaboration-sessions/preview',
            json={'invitation_code': invitation},
        )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertEqual(preview.get_json()['web_path'],
                         f'/topics/{self.discussion_topic.id}')
        self.assertEqual(preview.get_json()['subject']['id'],
                         self.discussion_topic.id)
        self.assertEqual(preview.get_json()['subject']['title'],
                         self.discussion_topic.title)
        self.assertEqual(
            CollaborationSession.query.filter_by(
                parent_invite_id=session_id).count(), 0)
        self.assertEqual(
            db.session.get(CollaborationSession, session_id).status, 'pending')

        first = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation},
        )
        second = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation},
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        first_token = first.get_json()['access_token']
        second_token = second.get_json()['access_token']
        self.assertNotEqual(first_token, second_token)
        self.assertNotEqual(
            first.get_json()['participant_session_id'],
            second.get_json()['participant_session_id'],
        )
        self.assertTrue(first.get_json()['independent_participant'])
        self.assertTrue(second.get_json()['independent_participant'])
        self.assertFalse(
            first.get_json()['bootstrap']['authorization']['browser_required'])
        self.assertEqual(
            first.get_json()['bootstrap']['web_path'],
            f'/topics/{self.discussion_topic.id}',
        )
        self.assertEqual(
            first.get_json()['bootstrap']['web_url'],
            f'https://clawteam.woa.com/topics/{self.discussion_topic.id}')
        self.assertIn(
            'Reuse this token',
            first.get_json()['bootstrap']['authorization']['token_reuse'])
        self.assertFalse(first.get_json()['previous_token_invalidated'])
        self.assertFalse(second.get_json()['previous_token_invalidated'])

        first_read = self.client.get(
            f'/api/v1/topics/{self.discussion_topic.id}',
            headers={'Authorization': f'Bearer {first_token}'},
        )
        second_read = self.client.get(
            f'/api/v1/topics/{self.discussion_topic.id}',
            headers={'Authorization': f'Bearer {second_token}'},
        )
        self.assertEqual(first_read.status_code, 200, first_read.get_data(as_text=True))
        self.assertEqual(second_read.status_code, 200, second_read.get_data(as_text=True))

        shared_content = '相同显示身份与相同内容也必须分别归属各自 Token。'
        first_reply = self.client.post(
            f'/api/v1/topics/{self.discussion_topic.id}/replies',
            json={'identity': '外部测试 Agent', 'content': shared_content},
            headers={
                'Authorization': f'Bearer {first_token}',
                'Idempotency-Key': 'topic-independent-first-reply',
            },
        )
        second_reply = self.client.post(
            f'/api/v1/topics/{self.discussion_topic.id}/replies',
            json={'identity': '外部测试 Agent', 'content': shared_content},
            headers={
                'Authorization': f'Bearer {second_token}',
                'Idempotency-Key': 'topic-independent-second-reply',
            },
        )
        self.assertEqual(first_reply.status_code, 201, first_reply.get_data(as_text=True))
        self.assertEqual(second_reply.status_code, 201, second_reply.get_data(as_text=True))
        self.assertEqual(first_reply.get_json()['author_name'], '外部测试 Agent')
        self.assertEqual(second_reply.get_json()['author_name'], '外部测试 Agent')

        first_reply_id = first_reply.get_json()['id']
        changed = self.client.patch(
            f'/api/v1/topics/{self.discussion_topic.id}/replies/{first_reply_id}',
            json={'identity': '外部测试 Agent A', 'content': '由第一个 Token 更新。'},
            headers={
                'Authorization': f'Bearer {first_token}',
                'Idempotency-Key': 'topic-independent-first-update',
            },
        )
        denied = self.client.patch(
            f'/api/v1/topics/{self.discussion_topic.id}/replies/{first_reply_id}',
            json={'identity': '外部测试 Agent', 'content': '第二个 Token 越权修改。'},
            headers={
                'Authorization': f'Bearer {second_token}',
                'Idempotency-Key': 'topic-independent-cross-update',
            },
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        self.assertEqual(changed.get_json()['author_name'], '外部测试 Agent A')
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        self.assertEqual(denied.get_json()['code'], 'TOPIC_REPLY_NOT_OWNER')

        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id
        managed = self.client.get(
            '/api/v1/collaboration-sessions'
            f'?subject_type=topic&subject_id={self.discussion_topic.id}')
        self.assertEqual(managed.status_code, 200, managed.get_data(as_text=True))
        self.assertEqual(managed.get_json()['total'], 1)
        self.assertEqual(managed.get_json()['items'][0]['participant_count'], 2)
        with self.client.session_transaction() as sess:
            sess.clear()

        row = db.session.get(CollaborationSession, session_id)
        row.invitation_expires_at = datetime.now() - timedelta(minutes=1)
        db.session.commit()
        link_expired = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation},
        )
        self.assertEqual(link_expired.status_code, 410)
        self.assertEqual(
            link_expired.get_json()['code'], 'INVITATION_EXPIRED')
        event_types = [
            item.event_type
            for item in CollaborationSessionEvent.query.filter_by(
                session_id=session_id).order_by(
                    CollaborationSessionEvent.id.asc()).all()
        ]
        self.assertEqual(event_types.count('participant_issued'), 2)
        children = CollaborationSession.query.filter_by(
            parent_invite_id=session_id).all()
        self.assertEqual(len(children), 2)
        self.assertEqual({child.agent_identity for child in children}, {''})

    def test_projectless_topic_can_issue_scoped_external_invite(self):
        created = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'topic',
                'subject_id': self.projectless_topic.id,
                'agent_identity': 'developer-ai:shared-method:codex',
            },
            headers={'Idempotency-Key': 'projectless-topic-invite-1'},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertIsNone(created.get_json()['project_id'])

        review_as_topic = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'topic',
                'subject_id': self.review_topic.id,
                'agent_identity': 'developer-ai:wrong-contract:codex',
            },
            headers={'Idempotency-Key': 'review-as-topic-denied-1'},
        )
        self.assertEqual(review_as_topic.status_code, 400,
                         review_as_topic.get_data(as_text=True))
        self.assertEqual(review_as_topic.get_json()['code'],
                         'TOPIC_SUBJECT_TYPE_MISMATCH')

    def test_case_review_records_cannot_be_changed_by_another_session(self):
        tokens = []
        for index, identity in enumerate((
                'developer-ai:racinggo:alice-codex',
                'developer-ai:racinggo:bob-claude')):
            created = self.client.post(
                '/api/v1/collaboration-sessions',
                json={
                    'subject_type': 'case_review',
                    'subject_id': self.review_topic.id,
                    'agent_identity': identity,
                },
                headers={'Idempotency-Key':
                         'case-review-owner-session-%d' % index})
            invitation = created.get_json()['invitation_code']
            with self.client.session_transaction() as sess:
                sess.clear()
            exchanged = self.client.post(
                '/api/v1/collaboration-sessions/exchange',
                json={'invitation_code': invitation})
            tokens.append(exchanged.get_json()['access_token'])
            if index == 0:
                with self.client.session_transaction() as sess:
                    sess['user_id'] = self.admin.id

        first_auth = {
            'Authorization': 'Bearer ' + tokens[0],
            'Idempotency-Key': 'case-review-owner-create',
        }
        created_record = self.client.post(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/reviews',
            json={'identity': 'Alice Agent',
                  'content': 'Alice review', 'score': 8},
            headers=first_auth)
        self.assertEqual(created_record.status_code, 201,
                         created_record.get_data(as_text=True))
        review_id = created_record.get_json()['id']

        second_auth = {'Authorization': 'Bearer ' + tokens[1]}
        visible = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/reviews',
            headers=second_auth)
        self.assertFalse(visible.get_json()['items'][0]['owned_by_me'])
        denied_update = self.client.patch(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}'
            f'/reviews/{review_id}',
            json={'content': 'Bob overwrite attempt'},
            headers=dict(second_auth, **{
                'Idempotency-Key': 'case-review-cross-update'}))
        self.assertEqual(denied_update.status_code, 403)
        self.assertEqual(denied_update.get_json()['code'],
                         'CASE_REVIEW_RECORD_NOT_OWNER')
        denied_delete = self.client.delete(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}'
            f'/reviews/{review_id}',
            headers=dict(second_auth, **{
                'Idempotency-Key': 'case-review-cross-delete'}))
        self.assertEqual(denied_delete.status_code, 403)
        self.assertEqual(denied_delete.get_json()['code'],
                         'CASE_REVIEW_RECORD_NOT_OWNER')

    def test_case_review_invite_does_not_bind_participant_identity(self):
        without_identity = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'case_review',
                'subject_id': self.review_topic.id,
            },
            headers={'Idempotency-Key': 'case-review-no-identity'},
        )
        self.assertEqual(
            without_identity.status_code, 201,
            without_identity.get_data(as_text=True))

        body = {
            'subject_type': 'case_review',
            'subject_id': self.review_topic.id,
            'agent_identity': '仅作为邀请备注，不绑定 Token',
        }
        created = self.client.post(
            '/api/v1/collaboration-sessions', json=body,
            headers={'Idempotency-Key': 'case-review-unique-identity'})
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        replay = self.client.post(
            '/api/v1/collaboration-sessions', json=body,
            headers={'Idempotency-Key': 'case-review-unique-identity'})
        self.assertEqual(replay.status_code, 200,
                         replay.get_data(as_text=True))
        duplicate = self.client.post(
            '/api/v1/collaboration-sessions', json=body,
            headers={'Idempotency-Key': 'case-review-duplicate-identity'})
        self.assertEqual(duplicate.status_code, 201, duplicate.get_data(as_text=True))

    def test_case_review_link_supports_72_hours_and_same_link_deadline_update(self):
        created = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'case_review',
                'subject_id': self.review_topic.id,
                'agent_identity': 'developer-ai:racinggo:deadline-test',
                'invitation_ttl_minutes': 4320,
                'token_ttl_minutes': 4320,
            },
            headers={'Idempotency-Key': 'case-review-72h-create'},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        payload = created.get_json()
        session_id = payload['id']
        invitation = payload['invitation_code']
        row = db.session.get(CollaborationSession, session_id)
        self.assertGreater(row.invitation_expires_at,
                           datetime.now() + timedelta(hours=71, minutes=59))
        self.assertEqual(row.token_ttl_seconds, 72 * 60 * 60)

        pending_deadline = datetime.now() + timedelta(hours=60)
        updated = self.client.patch(
            f'/api/v1/collaboration-sessions/{session_id}/deadline',
            json={'expires_at': pending_deadline.isoformat(timespec='seconds')},
            headers={'Idempotency-Key': 'case-review-pending-deadline'},
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()['deadline_field'],
                         'invitation_expires_at')
        self.assertFalse(updated.get_json()['link_rotated'])

        with self.client.session_transaction() as sess:
            sess.clear()
        exchanged = self.client.post(
            '/api/v1/collaboration-sessions/exchange',
            json={'invitation_code': invitation})
        self.assertEqual(exchanged.status_code, 200,
                         exchanged.get_data(as_text=True))
        access_token = exchanged.get_json()['access_token']

        with self.client.session_transaction() as sess:
            sess['user_id'] = self.admin.id
        row = db.session.get(CollaborationSession, session_id)
        row.token_expires_at = datetime.now() - timedelta(minutes=1)
        db.session.commit()
        active_deadline = datetime.now() + timedelta(hours=48)
        extended = self.client.patch(
            f'/api/v1/collaboration-sessions/{session_id}/deadline',
            json={'expires_at': active_deadline.isoformat(timespec='seconds')},
            headers={'Idempotency-Key': 'case-review-active-deadline'},
        )
        self.assertEqual(extended.status_code, 200,
                         extended.get_data(as_text=True))
        self.assertEqual(
            extended.get_json()['deadline_field'], 'invitation_expires_at')
        self.assertFalse(extended.get_json()['link_rotated'])
        row = db.session.get(CollaborationSession, session_id)
        self.assertEqual(
            row.invitation_expires_at.replace(microsecond=0),
            active_deadline.replace(microsecond=0),
        )

        with self.client.session_transaction() as sess:
            sess.clear()
        context = self.client.get(
            f'/api/v1/shift-left/case-reviews/{self.review_topic.id}/context',
            headers={'Authorization': f'Bearer {access_token}'})
        self.assertEqual(context.status_code, 200, context.get_data(as_text=True))

    def test_case_review_deadline_cannot_exceed_72_hours(self):
        created = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'case_review',
                'subject_id': self.review_topic.id,
                'agent_identity': 'developer-ai:racinggo:deadline-cap',
            },
            headers={'Idempotency-Key': 'case-review-deadline-cap-create'},
        )
        too_late = datetime.now() + timedelta(hours=73)
        response = self.client.patch(
            f"/api/v1/collaboration-sessions/{created.get_json()['id']}/deadline",
            json={'expires_at': too_late.isoformat(timespec='seconds')},
            headers={'Idempotency-Key': 'case-review-deadline-cap-update'},
        )
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['code'],
                         'INVALID_COLLABORATION_DEADLINE')

    def test_collaboration_create_rejects_ttl_over_72_hours_without_clamping(self):
        response = self.client.post(
            '/api/v1/collaboration-sessions',
            json={
                'subject_type': 'topic',
                'subject_id': self.discussion_topic.id,
                'agent_identity': 'developer-ai:racinggo:ttl-overflow',
                'invitation_ttl_minutes': 4321,
                'token_ttl_minutes': 4320,
            },
            headers={'Idempotency-Key': 'topic-ttl-overflow'},
        )
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(
            response.get_json()['code'], 'COLLABORATION_TTL_OUT_OF_RANGE')
        self.assertEqual(0, CollaborationSession.query.filter_by(
            agent_identity='developer-ai:racinggo:ttl-overflow').count())

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
