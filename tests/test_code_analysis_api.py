import sys
import types
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB)) if str(WEB) not in sys.path else None
class Noop:
    def __init__(self, *a, **kw): pass
    def __call__(self, *a, **kw): return self
    def __getattr__(self, name): return Noop()
for name, attrs in [('flask_cors', {'CORS': Noop}), ('flask_socketio', {
    'SocketIO': Noop, 'emit': Noop(), 'join_room': Noop(), 'leave_room': Noop()})]:
    if name not in sys.modules:
        module = types.ModuleType(name)
        for k, v in attrs.items(): setattr(module, k, v)
        sys.modules[name] = module

from flask import Flask
from app import db
from app.api import api_bp
from app.models import (Project, User, OpenClawInstance, Skill, TestPlan as Plan,
    TestReport as Report, CodeAnalysisProject, CodeAnalysisJob, CodeAnalysisDecision,
    KnowledgeEntry, KnowledgeEntryRevision, ShiftLeftAnalysisFinding,
    ShiftLeftFinding, ShiftLeftFindingFeedback, WorkflowRun, WorkflowRunStep, hash_token)


class CodeAnalysisApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='test', TESTING=True,
            CODE_ANALYSIS_ENABLED=True, SHIFT_LEFT_ENABLED=True)
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.project, self.other = Project(name='A', tapd_workspace_id='123'), Project(name='B')
        db.session.add_all([self.project, self.other]); db.session.flush()
        self.owner = User(username='owner', role='user', managed_projects=[self.project.id])
        self.reader = User(username='reader', role='user', managed_projects=[self.other.id])
        self.admin = User(username='admin', role='super_admin')
        for user in (self.owner, self.reader, self.admin):
            user.set_password('test-only')
        self.agent = OpenClawInstance(name='Analyzer', safe_name='analyzer', claw_tag='analyzer',
            project_id=self.project.id, owner='owner', api_token_hash=hash_token('agent-token'))
        self.skill = Skill(name='code-review', display_name='Code Review', scope='global',
            review_status='approved', visibility='public', template_content='Check actual code evidence.',created_by='owner')
        db.session.add_all([self.owner,self.reader,self.admin,self.agent,self.skill]); db.session.flush()
        self.plan = Plan(name='Version plan', project_id=self.project.id, status='completed',
                         start_date=date(2026,9,30), end_date=date(2026,9,30), description='Login scope')
        self.report = Report(title='Full analysis', project_id=self.project.id,
            status='published', is_hidden=False, content='All candidates', submitter_claw_id=self.agent.id)
        db.session.add_all([self.plan,self.report]); db.session.commit()
        self.client = self.app.test_client(); self.login(self.owner)
        self.token_client = self.app.test_client()
        self.client.post(f'/api/v1/code-analysis/projects/{self.project.id}/initialize')
        cfg = db.session.get(CodeAnalysisProject,self.project.id)
        response = self.client.put(f'/api/v1/code-analysis/projects/{self.project.id}',json={
            'expected_revision':cfg.revision, 'skill_id':self.skill.id, 'executor_claw_id':self.agent.id,
            'repository_url':'https://git.example.com/a/repo.git','source_ref':'main'})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def login(self, user):
        with self.client.session_transaction() as session: session['user_id']=user.id

    def create_job(self, key='create-1'):
        response=self.client.post('/api/v1/code-analysis/runs',json={
            'plan_id':self.plan.id,'baseline_ref':'v1'},headers={'Idempotency-Key':key})
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        return response.get_json()['id']

    def start(self, jid):
        response=self.client.post(f'/api/v1/code-analysis/runs/{jid}/start',json={})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        run=db.session.get(WorkflowRun,response.get_json()['workflow_run_id'])
        step=WorkflowRunStep.query.filter_by(run_id=run.id,step_id='analyze').one()
        run.status,step.status,step.attempt_no='running','running',1
        db.session.commit()
        return run

    def result(self, jid, run, key='result-1', **overrides):
        data={'workflow_run_id':run.id,'attempt_no':1,
            'baseline':{'base_sha':'a'*40,'target_sha':'b'*40}, 'report_id':self.report.id,
            'findings':[{'finding_key':'login-null','title':'Null callback','description':'Evidence',
                         'severity':'high','code_locations':[{'file':'Login.cs','line':42}]}]}
        data.update(overrides)
        return self.token_client.post(f'/api/v1/code-analysis/runs/{jid}/result',json=data,
            headers={'Idempotency-Key':key,'Authorization':'Bearer agent-token'})

    def completed(self):
        jid=self.create_job();run=self.start(jid)
        result=self.result(jid,run)
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        return jid,ShiftLeftFinding.query.one()

    def decide(self,jid,f,action='ignore',label='risk_accepted',key='decision-1'):
        response=self.client.post(f'/api/v1/code-analysis/runs/{jid}/findings/{f.id}/decision',json={
            'expected_revision':f.revision,'action':action,'truth_label':label,
            'reason':'Human evidence and business rationale'},headers={'Idempotency-Key':key})
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        return response.get_json()

    def test_bootstrap_is_idempotent_and_blank(self):
        self.client.post(f'/api/v1/code-analysis/projects/{self.project.id}/initialize')
        self.assertEqual(KnowledgeEntry.query.count(),2)
        self.assertEqual(KnowledgeEntryRevision.query.count(),2)
        self.assertTrue(all(e.content=='' for e in KnowledgeEntry.query.all()))
        self.login(self.admin)
        self.client.post(f'/api/v1/code-analysis/projects/{self.other.id}/initialize')
        self.assertEqual(KnowledgeEntry.query.filter_by(category='code_analysis_general').count(),1)

    def test_completed_plan_can_start_without_mission_and_is_idempotent(self):
        jid=self.create_job();run=self.start(jid)
        again=self.client.post(f'/api/v1/code-analysis/runs/{jid}/start',json={})
        self.assertEqual(again.get_json()['workflow_run_id'],run.id)
        self.assertEqual(WorkflowRun.query.count(),1)
        snapshot=db.session.get(CodeAnalysisJob,jid).snapshot_json
        self.plan.description='Changed scope';self.skill.template_content='Changed method';db.session.commit()
        self.assertEqual(snapshot['plan']['description'],'Login scope')
        self.assertEqual(snapshot['skill']['content'],'Check actual code evidence.')
        config=db.session.get(CodeAnalysisProject,self.project.id)
        self.assertEqual(config.executor_claw_id,self.agent.id)

    def test_create_idempotency_and_cross_project_denial(self):
        jid=self.create_job()
        replay=self.client.post('/api/v1/code-analysis/runs',json={'plan_id':self.plan.id,'baseline_ref':'v1'},
            headers={'Idempotency-Key':'create-1'})
        self.assertEqual(replay.get_json()['id'],jid)
        self.login(self.reader)
        self.assertEqual(self.client.get(f'/api/v1/code-analysis/runs/{jid}').status_code,403)
        self.assertEqual(self.client.post('/api/v1/code-analysis/runs',json={'plan_id':self.plan.id,'baseline_ref':'v1'},
            headers={'Idempotency-Key':'x'}).status_code,403)

    def test_report_and_sha_validation_and_stale_attempt(self):
        jid=self.create_job();run=self.start(jid)
        self.assertEqual(self.result(jid,run,baseline={'base_sha':'main','target_sha':'HEAD'}).status_code,400)
        self.report.status='draft';db.session.commit()
        self.assertEqual(self.result(jid,run,key='bad-report').status_code,400)
        self.report.status='published';db.session.commit()
        self.assertEqual(self.result(jid,run,key='old',attempt_no=0).status_code,409)
        self.assertEqual(self.result(jid,run,key='ok').status_code,200)
        self.assertEqual(self.result(jid,run,key='ok').status_code,200)
        self.assertEqual(ShiftLeftFinding.query.count(),1)

    def test_risk_acceptance_is_not_false_positive_and_dispatches_learning(self):
        jid,f=self.completed();d=self.decide(jid,f)
        self.assertEqual(ShiftLeftFindingFeedback.query.count(),0)
        self.assertEqual(d['truth_label'],'risk_accepted')
        self.assertEqual(d['learning_status'],'dispatched')
        self.assertTrue(d['learning_run_id'])
        row=db.session.get(CodeAnalysisDecision,d['id'])
        run=db.session.get(WorkflowRun,row.learning_run_id)
        run.status='running';step=WorkflowRunStep.query.filter_by(run_id=run.id).one()
        step.status,step.attempt_no='running',1;db.session.commit()
        page=db.session.get(KnowledgeEntry,row.snapshot_json['knowledge_id'])
        response=self.token_client.post(f'/api/v1/code-analysis/decisions/{row.id}/learning-result',json={
            'workflow_run_id':run.id,'attempt_no':1,'expected_revision':page.current_revision,
            'project_content':'Risk accepted only for this project.','summary':'Do not count risk acceptance as false positive.',
            'general_proposal':'Generic proposal, not automatically published.'},headers={'Authorization':'Bearer agent-token'})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.assertEqual(response.get_json()['learning_status'],'learned')
        self.assertEqual(page.current_revision,2)
        general=KnowledgeEntry.query.filter_by(category='code_analysis_general').one()
        self.assertEqual(general.content,'')

    def test_human_correction_supersedes_prior_learning(self):
        jid,f=self.completed();old=self.decide(jid,f)
        new=self.decide(jid,f,action='confirm',label='false_positive',key='corrected')
        response=self.token_client.post(f'/api/v1/code-analysis/decisions/{old["id"]}/learning-result',json={
            'workflow_run_id':old['learning_run_id'],'attempt_no':1,
            'expected_revision':1,'project_content':'old','summary':'old'},headers={'Authorization':'Bearer agent-token'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(CodeAnalysisDecision.query.count(),2)
        self.assertEqual(ShiftLeftFindingFeedback.query.one().label,'false_positive')

    def test_selected_knowledge_sharing_is_read_only_and_revocable(self):
        config=db.session.get(CodeAnalysisProject,self.project.id);kid=config.knowledge_id
        response=self.client.put(f'/api/v1/resource-sharing/knowledge/{kid}',json={
            'scope':'selected','project_ids':[self.other.id],'expected_revision':1})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.login(self.reader)
        page=self.client.get(f'/api/v1/knowledge/journal-pages/{kid}')
        self.assertEqual(page.status_code,200);self.assertFalse(page.get_json()['can_edit'])
        self.assertEqual(self.client.get(f'/api/v1/knowledge/{kid}/revisions').status_code,200)
        self.assertEqual(self.client.post(f'/api/v1/knowledge/{kid}/revisions',json={
            'title':'Overwrite','content':'Other project','expected_revision':1},headers={'Idempotency-Key':'bad'}).status_code,404)
        self.assertEqual(self.client.put(f'/api/v1/resource-sharing/knowledge/{kid}',json={
            'scope':'all','expected_revision':2}).status_code,403)
        self.login(self.owner)
        self.client.put(f'/api/v1/resource-sharing/knowledge/{kid}',json={'scope':'project','expected_revision':2})
        self.login(self.reader)
        self.assertEqual(self.client.get(f'/api/v1/knowledge/{kid}').status_code,404)
        self.assertEqual(self.client.get(f'/api/v1/knowledge/{kid}/export.md').status_code,404)

    def test_skill_sharing_controls_raw_pack_and_details(self):
        self.skill.scope='project';self.skill.applicable_projects=[self.project.id];db.session.commit()
        self.client.put(f'/api/v1/resource-sharing/skill/{self.skill.id}',json={
            'scope':'selected','project_ids':[self.other.id]})
        self.login(self.reader)
        self.assertEqual(self.client.get(f'/api/v1/skills/{self.skill.id}/raw').status_code,200)
        self.assertEqual(self.client.get(f'/api/v1/skills/{self.skill.id}/pack').status_code,200)
        self.login(self.owner)
        self.client.put(f'/api/v1/resource-sharing/skill/{self.skill.id}',json={'scope':'project','expected_revision':1})
        self.login(self.reader)
        self.assertEqual(self.client.get(f'/api/v1/skills/{self.skill.id}').status_code,403)
        self.assertEqual(self.client.get(f'/api/v1/skills/{self.skill.id}/raw').status_code,403)
        self.assertEqual(self.client.get(f'/api/v1/skills/{self.skill.id}/pack').status_code,403)

    def test_submit_is_explicit_deduplicated_and_ambiguous_safe(self):
        jid,f=self.completed();d=self.decide(jid,f,'submit','true_positive')
        path=f'/api/v1/code-analysis/decisions/{d["id"]}/submit-bug'
        with patch('app.api.tapd._get_tapd_credentials',return_value=('user','secret')),patch(
                'app.api.tapd._tapd_request',return_value=[{'Bug':{'id':'123456'}}]) as remote:
            self.assertEqual(self.client.post(path,json={}).status_code,400)
            self.assertEqual(self.client.post(path,json={'confirmed':True}).status_code,200)
            self.assertEqual(self.client.post(path,json={'confirmed':True}).status_code,200)
            self.assertEqual(remote.call_count,1)

    def test_ambiguous_tapd_write_is_not_retried(self):
        jid,f=self.completed();d=self.decide(jid,f,'submit','true_positive')
        path=f'/api/v1/code-analysis/decisions/{d["id"]}/submit-bug'
        with patch('app.api.tapd._get_tapd_credentials',return_value=('user','secret')),patch(
                'app.api.tapd._tapd_request',side_effect=TimeoutError()) as remote:
            self.assertEqual(self.client.post(path,json={'confirmed':True}).status_code,502)
            self.assertEqual(self.client.post(path,json={'confirmed':True}).status_code,409)
            self.assertEqual(remote.call_count,1)
        self.assertEqual(db.session.get(CodeAnalysisDecision,d['id']).submission_status,'unknown')

    def test_repository_credential_and_command_rejection(self):
        from app.services.code_analysis import repository,git_ref
        for url in ('https://u:password@host/repo','file:///C:/repo','https://host/repo?token=secret','ssh://u:pass@host/repo'):
            with self.assertRaises(ValueError):repository(url)
        for ref in ('--upload-pack=evil','a..b','main\nwhoami','x@{0}'):
            with self.assertRaises(ValueError):git_ref(ref)

    def test_project_agent_dispatch_binds_configured_executor_not_caller(self):
        caller=OpenClawInstance(name='Requester',safe_name='requester',claw_tag='requester',
            project_id=self.project.id,owner='owner',api_token_hash=hash_token('requester-token'))
        db.session.add(caller);db.session.commit()
        jid=self.create_job()
        response=self.token_client.post(f'/api/v1/code-analysis/runs/{jid}/start',json={},
            headers={'Authorization':'Bearer requester-token'})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        from app.api.workflows import _run_worker_claw_id
        run=db.session.get(WorkflowRun,response.get_json()['workflow_run_id'])
        self.assertEqual(_run_worker_claw_id(run),self.agent.id)
        step=WorkflowRunStep.query.filter_by(run_id=run.id,step_id='analyze').one()
        self.assertEqual(step.target_claw_id,self.agent.id)

    def test_result_receipt_can_replay_after_workflow_finished(self):
        jid=self.create_job();run=self.start(jid)
        self.assertEqual(self.result(jid,run).status_code,200)
        run.status='succeeded';db.session.commit()
        self.assertEqual(self.result(jid,run).status_code,200)
        self.assertEqual(self.result(jid,run,key='different').status_code,409)
        self.assertEqual(ShiftLeftFinding.query.count(),1)

    def test_general_publication_requires_review_and_is_idempotent(self):
        jid,f=self.completed();d=self.decide(jid,f)
        path=f'/api/v1/code-analysis/decisions/{d["id"]}/publish-general'
        self.assertEqual(self.client.post(path,json={'content':'Generic only','expected_revision':1}).status_code,400)
        data={'confirmed':True,'content':'Generic only','expected_revision':1}
        first=self.client.post(path,json=data)
        self.assertEqual(first.status_code,200,first.get_data(as_text=True))
        self.assertEqual(first.get_json()['general_published_revision'],2)
        self.assertEqual(self.client.post(path,json=data).status_code,200)
        general=KnowledgeEntry.query.filter_by(category='code_analysis_general').one()
        self.assertEqual(general.current_revision,2)
        self.assertEqual(general.content.count('Generic only'),1)
        self.login(self.reader)
        self.assertEqual(self.client.post(path,json=data).status_code,403)

    def test_shared_knowledge_freezes_reference_and_revocation_blocks_new_start(self):
        config=db.session.get(CodeAnalysisProject,self.project.id)
        self.login(self.admin)
        extra=KnowledgeEntry(title='B generic evidence',content='Component method',project_id=self.other.id,
            scope='project',category='analysis_method',status='approved',created_by='admin')
        db.session.add(extra);db.session.commit()
        self.assertEqual(self.client.put(f'/api/v1/resource-sharing/knowledge/{extra.id}',json={
            'scope':'selected','project_ids':[self.project.id]}).status_code,200)
        self.login(self.owner)
        saved=self.client.put(f'/api/v1/code-analysis/projects/{self.project.id}',json={
            'expected_revision':config.revision,'skill_id':self.skill.id,'executor_claw_id':self.agent.id,
            'repository_url':config.repository_url,'source_ref':'main','knowledge_ids':[extra.id]})
        self.assertEqual(saved.status_code,200,saved.get_data(as_text=True))
        jid=self.create_job()
        snapshot=db.session.get(CodeAnalysisJob,jid).snapshot_json
        self.assertEqual(next(k['content'] for k in snapshot['knowledge'] if k['id']==extra.id),'Component method')
        listed=self.client.get(f'/api/v1/code-analysis/runs?project_id={self.project.id}').get_json()['items'][0]
        self.assertNotIn('knowledge',listed['snapshot']);self.assertNotIn('context_snapshot',listed)
        self.login(self.admin)
        self.client.put(f'/api/v1/resource-sharing/knowledge/{extra.id}',json={'scope':'project','expected_revision':1})
        self.login(self.owner)
        response=self.client.post(f'/api/v1/code-analysis/runs/{jid}/start',json={})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.get_json()['code'],'RESOURCE_ACCESS_REVOKED')
        self.assertEqual(WorkflowRun.query.count(),0)

    def test_analysis_report_not_published_cross_project_by_shared_knowledge(self):
        self.completed();self.login(self.reader)
        self.assertEqual(self.client.get(f'/api/v1/test-reports/{self.report.id}').status_code,403)

    def test_learning_conflict_does_not_claim_success(self):
        jid,f=self.completed();d=self.decide(jid,f)
        run=db.session.get(WorkflowRun,d['learning_run_id']);run.status='running'
        step=WorkflowRunStep.query.filter_by(run_id=run.id).one();step.status='running';step.attempt_no=1
        page=db.session.get(KnowledgeEntry,db.session.get(CodeAnalysisDecision,d['id']).snapshot_json['knowledge_id'])
        page.current_revision=2;db.session.commit()
        response=self.token_client.post(f'/api/v1/code-analysis/decisions/{d["id"]}/learning-result',json={
            'workflow_run_id':run.id,'attempt_no':1,'expected_revision':1,
            'project_content':'would overwrite','summary':'stale'},headers={'Authorization':'Bearer agent-token'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(page.content,'')
        self.assertNotEqual(db.session.get(CodeAnalysisDecision,d['id']).learning_status,'learned')

    def test_feature_flag_off_is_no_write(self):
        self.app.config['CODE_ANALYSIS_ENABLED']=False
        self.assertEqual(self.client.post('/api/v1/code-analysis/runs',json={'plan_id':self.plan.id}).status_code,404)
        self.assertEqual(CodeAnalysisJob.query.count(),0)

    def test_bug_submission_uses_human_reviewed_snapshot(self):
        jid,f=self.completed();d=self.decide(jid,f,'submit','true_positive')
        f.title='Unreviewed later title';f.description='Unreviewed later evidence';db.session.commit()
        with patch('app.api.tapd._get_tapd_credentials',return_value=('user','secret')),patch(
                'app.api.tapd._tapd_request',return_value=[{'Bug':{'id':'123456'}}]) as remote:
            response=self.client.post(f'/api/v1/code-analysis/decisions/{d["id"]}/submit-bug',json={'confirmed':True})
            self.assertEqual(response.status_code,200)
            payload=remote.call_args.kwargs['data']
            self.assertEqual(payload['title'],'Null callback')
            self.assertNotIn('Unreviewed later evidence',payload['description'])

    def test_plan_with_analysis_cannot_be_deleted(self):
        self.create_job()
        response=self.client.delete(f'/api/v1/test-plans/{self.plan.id}')
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.get_json()['code'],'PLAN_HAS_CODE_ANALYSIS')
        self.assertIsNotNone(db.session.get(Plan,self.plan.id))


if __name__=='__main__': unittest.main()
