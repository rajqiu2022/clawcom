import unittest
from pathlib import Path


class ShiftLeftFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        templates = Path(__file__).resolve().parents[1] / 'web' / 'templates'
        cls.reports = (templates / 'test_reports.html').read_text(encoding='utf-8')
        cls.topic = (templates / 'topic_detail.html').read_text(encoding='utf-8')
        cls.handoff = (templates / 'developer_ai_collaborate.html').read_text(
            encoding='utf-8')

    def test_report_and_case_review_entries_are_feature_gated(self):
        self.assertIn('SHIFT_LEFT_ENABLED && d.can_edit', self.reports)
        self.assertIn("createDeveloperAiLink('analysis_report'", self.reports)
        self.assertIn('t.external_collaboration_enabled ?? SHIFT_LEFT_ENABLED', self.topic)
        self.assertIn('t.can_manage_external_collaboration ??', self.topic)
        self.assertIn('外部协作（仅发起人/管理员）', self.topic)
        self.assertIn('subject_type:subjectType', self.topic)
        self.assertIn("createCaseReviewDeveloperAiLink('topic')", self.topic)

    def test_invitation_secret_is_carried_in_fragment(self):
        self.assertIn("#invite=${encodeURIComponent(data.invitation_code)}", self.reports)
        self.assertIn("#invite=${encodeURIComponent(data.invitation_code)}", self.topic)
        self.assertIn('location.hash.slice(1)', self.handoff)
        self.assertIn("params.get('subject')", self.handoff)
        self.assertIn('/api/v1/collaboration-sessions/preview', self.handoff)
        self.assertIn('id="subject-link"', self.handoff)
        self.assertIn("subject=${encodeURIComponent(`/topics/${TOPIC_ID}`)}", self.topic)

    def test_handoff_does_not_exchange_until_explicit_click(self):
        self.assertIn("getElementById('exchange-btn')?.addEventListener('click'", self.handoff)
        self.assertNotIn("history.replaceState(null, '', location.pathname)", self.handoff)
        self.assertIn('每个参与 Agent 应自行兑换并记住自己的 Token', self.handoff)
        self.assertIn('为另一个 Agent 签发独立 Token', self.handoff)
        self.assertIn('浏览器不是必需', self.handoff)
        self.assertIn('打开课题/评审内容', self.handoff)

    def test_case_review_handoff_is_ready_for_external_ai(self):
        self.assertIn('defaultCaseReviewDeveloperAiIdentity', self.topic)
        self.assertNotIn('value="developer-ai:case-review"', self.topic)
        self.assertNotIn('_case_ai_identity', self.topic)
        self.assertIn('多 Agent 独立 Token', self.topic)
        self.assertIn('loadCaseReviewCollaborationSessions', self.topic)
        self.assertIn('data-case-ai-revoke', self.topic)
        self.assertIn('data-case-ai-extend', self.topic)
        self.assertIn('/deadline`', self.topic)
        self.assertIn('max="4320"', self.topic)
        self.assertIn('max="4320"', self.reports)
        self.assertNotIn('max="1440"', self.reports)
        self.assertIn('复制完整 AI 提示词', self.handoff)
        self.assertIn('data.bootstrap || data', self.handoff)
        self.assertIn('先调用 context 和 reviews，再分页读取 cases', self.handoff)
        self.assertIn('owned_by_me=true', self.handoff)

    def test_any_topic_can_invite_scoped_external_ai(self):
        self.assertIn('createTopicDeveloperAiLink', self.topic)
        self.assertIn('↗ 邀请外部协作', self.topic)
        self.assertIn("subjectType === 'topic'", self.topic)
        self.assertIn('topic-discussion-bootstrap.v1', self.handoff)
        self.assertIn('每次写入使用唯一 Idempotency-Key', self.handoff)


if __name__ == '__main__':
    unittest.main()
