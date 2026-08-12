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
        self.assertIn('const canInviteDeveloperAi = SHIFT_LEFT_ENABLED', self.topic)
        self.assertIn("subject_type:'case_review'", self.topic)

    def test_invitation_secret_is_carried_in_fragment(self):
        self.assertIn("#invite=${encodeURIComponent(data.invitation_code)}", self.reports)
        self.assertIn("#invite=${encodeURIComponent(data.invitation_code)}", self.topic)
        self.assertIn('location.hash.slice(1)', self.handoff)

    def test_handoff_does_not_exchange_until_explicit_click(self):
        self.assertIn("getElementById('exchange-btn')?.addEventListener('click'", self.handoff)
        self.assertIn("history.replaceState(null, '', location.pathname)", self.handoff)
        self.assertIn('邀请只能兑换一次', self.handoff)


if __name__ == '__main__':
    unittest.main()
