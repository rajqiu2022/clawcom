"""Publish Team skills and append Worker handoff; inspection is the default.

Uses existing administrator authentication in memory; never installs skills,
changes Worker configuration, restarts services, or starts a workflow.
"""
import argparse
import json
import uuid

import paramiko

from deploy_agent_teams import ENV_SOURCE, ROOT, command, run_python


REMOTE_SCRIPT = r'''
from app import create_app, db
from app.models import User
import requests, hashlib, io, zipfile

app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin is not None
    cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': admin.id})
    headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
    db.session.remove()

def api(method, path, body=None, extra=None):
    response = requests.request(method, 'http://127.0.0.1:18800/api/v1' + path,
                                headers=dict(headers, **(extra or {})), json=body, timeout=30)
    if not response.ok:
        print('TEAM_RELEASE ' + json.dumps({'api_error': path, 'status': response.status_code}), flush=True)
        raise SystemExit()
    return response

def emit(value):
    print('TEAM_RELEASE ' + json.dumps(value, ensure_ascii=False), flush=True)

page = api('GET', '/knowledge/%s' % (journal_id or 454)).json()
listing = api('GET', '/skills?summary=true&show_deleted=true&show_off_shelf=true').json()
rows = listing if isinstance(listing, list) else listing.get('skills', listing.get('items', []))
matches = [s for s in rows if s.get('name') in names]
if not apply_changes:
    if journal_id:
        emit({'journal': page, 'matching_skills': matches})
        raise SystemExit()
    books = api('GET', '/knowledge-notebooks?project_id=6').json()['items']
    emit({'knowledge_454': {k: page.get(k) for k in ('id', 'title', 'entry_type', 'notebook_id', 'current_revision')},
          'matching_skills': matches,
          'notebooks': [{'id': b['id'], 'title': b['title'], 'pages': [
              {k: p.get(k) for k in ('id', 'title', 'current_revision', 'archived_at')}
              for p in b.get('pages', [])]} for b in books]})
else:
    if journal_id:
        assert page.get('entry_type') == 'test_journal', 'Target is not a journal page'
        assert page.get('current_revision') == expected_revision, 'Journal changed; inspect again'
        assert not page.get('archived_at'), 'Journal archived'
        assert marker not in page['content'], 'Handoff already present; inspect instead of duplicating'
    published = []
    for spec in specs:
        existing = [s for s in matches if s['name'] == spec['name']]
        assert len(existing) <= 1
        if existing:
            skill = api('GET', '/skills/%s' % existing[0]['id']).json()
            assert not skill.get('is_deleted'), 'Existing skill deleted'
        else:
            skill = api('POST', '/skills', spec).json()
        sid = skill['id']
        api('PUT', '/skills/%s/files/SKILL.md' % sid,
            {'content': spec['template_content'], 'description': spec['description']})
        verified = api('GET', '/skills/%s' % sid).json()
        assert verified['review_status'] == 'approved'
        assert verified['visibility'] == 'public'
        assert verified['template_content'] == spec['template_content']
        raw = api('GET', '/skills/%s/raw' % sid).text
        assert spec['template_content'] in raw
        archive = zipfile.ZipFile(io.BytesIO(api('GET', '/skills/%s/pack' % sid).content))
        assert archive.read(spec['name'] + '/SKILL.md').decode('utf-8') == spec['template_content']
        result = {'id': sid, 'name': spec['name'], 'review_status': verified['review_status'],
                  'market_status': verified.get('market_status'),
                  'sha256': hashlib.sha256(spec['template_content'].encode('utf-8')).hexdigest()}
        published.append(result)
        emit({'published_skill': result})
    if not journal_id:
        emit({'skills_published': len(published), 'journal_updated': False,
              'worker_modified': False, 'skills_installed': False, 'runs_started': False})
        raise SystemExit()
    links = '\n'.join('- Skill #%s：[%s](http://clawteam.woa.com:18800/api/v1/skills/%s/raw)，v1.0.0，已发布。'
                      % (s['id'], s['name'], s['id']) for s in published)
    content = page['content'] + '\n\n' + addition.replace('{{PUBLISHED_SKILLS}}', links)
    body = {'title': page['title'], 'content': content, 'expected_revision': expected_revision,
            'change_summary': '发布团队协作和成员动态 Skill；补充 Worker 可靠上报接线任务与验收标准'}
    api('POST', '/knowledge/%s/revisions' % journal_id, body,
        {'Idempotency-Key': 'team-skills-worker-handoff-%s-r%s-v1' % (journal_id, expected_revision)})
    readback = api('GET', '/knowledge/journal-pages/%s' % journal_id).json()
    assert readback['content'] == content
    assert readback['current_revision'] == expected_revision + 1
    emit({'journal_id': journal_id, 'revision': readback['current_revision'], 'preserved_previous_content': True,
          'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
          'worker_modified': False, 'skills_installed': False, 'runs_started': False})
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-revision', type=int)
    parser.add_argument('--journal-id', type=int, help='Only set after the destination is confirmed')
    args = parser.parse_args()
    names = ['agent-team-collaboration', 'agent-team-member-activity']
    specs = []
    addition = ''
    if args.apply:
        if args.journal_id and args.expected_revision is None:
            parser.error('--journal-id requires --expected-revision after inspection')
        for name, display, desc in [
            (names[0], 'Agent 团队协作与经理调度', '团队角色、经理任期、Mission 计划与受控派发、阶段交接及证据回读'),
            (names[1], 'Agent 团队成员动态上报', '上报自己的空闲、工作、阻塞、任务进度和历史；正确处理幂等、版本冲突及重启对账'),
        ]:
            specs.append(dict(name=name, display_name=display, description=desc,
                              category='custom', scope='global', visibility='public', is_standard=False,
                              template_content=(ROOT / 'openclaw-agent' / 'skills' / name / 'SKILL.md').read_text(encoding='utf-8')))
        addition = (ROOT / 'docs' / 'Agent_Team_Worker_Handoff_2026-09-20.md').read_text(encoding='utf-8')
    variables = dict(names=names, specs=specs, addition=addition, apply_changes=args.apply,
                     journal_id=args.journal_id,
                     expected_revision=args.expected_revision,
                     marker='## 2026-09-20：团队 Skill 发布与 Worker 可靠上报接线')
    source = ENV_SOURCE + '\n' + '\n'.join('%s = %r' % pair for pair in variables.items()) + '\n' + REMOTE_SCRIPT
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('9.134.11.169', port=36000, username='root', key_filename=args.key, timeout=20)
    try:
        stage = '/tmp/team-skills-' + uuid.uuid4().hex
        command(client, 'mkdir -m 700 ' + stage)
        with client.open_sftp() as sftp:
            output = run_python(client, sftp, stage, 'publish', source)
            for line in output.splitlines():
                if line.startswith('TEAM_RELEASE '):
                    result = json.loads(line[len('TEAM_RELEASE '):])
                    if 'api_error' in result:
                        raise RuntimeError('API returned %s for %s' % (result['status'], result['api_error']))
    finally:
        client.close()


if __name__ == '__main__':
    main()
