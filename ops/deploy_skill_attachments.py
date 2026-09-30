"""Deploy Skill attachments additively, preserving production hotfixes."""
import deploy_agent_teams as release


release.BASE = '41f3275'
release.FILES = ('app/models.py', 'app/api/skills.py', 'templates/skills.html')
release.MIGRATIONS = ('20260930_skill_attachments.sql',)
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
from pathlib import Path
app = create_app('production')
with app.app_context():
    existing = set(inspect(db.engine).get_table_names())
    assert 'skills' in existing
    with db.engine.begin() as conn:
        for name in ('skills', 'skill_attachments'):
            if name in existing:
                ddl = conn.execute(text('SHOW CREATE TABLE ' + name)).fetchone()[1]
                with open(backup + '/' + name + '_schema.sql', 'w') as stream:
                    stream.write(ddl)
        for name, source in migrations:
            for statement in source.split(';'):
                if statement.strip():
                    conn.execute(text(statement))
    actual = {c['name'] for c in inspect(db.engine).get_columns('skill_attachments')}
    assert {c.name for c in db.metadata.tables['skill_attachments'].columns} <= actual
    root = Path(os.environ.get('SKILL_ATTACHMENT_ROOT') or
                '/opt/openclaw-web/hub-store/skill-attachments').resolve()
    assert str(root).startswith('/opt/'), 'Attachment storage must be persistent'
    root.mkdir(parents=True, exist_ok=True)
    assert os.access(root, os.W_OK)
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'attachment_root': str(root),
        'migrations_applied': 1, 'business_rows_modified': 0, 'backup': backup,
    }))
'''
release.SMOKE = r'''
import io, hashlib, time, zipfile, requests
from app import create_app, db
from app.models import User, Skill, SkillAttachment, SkillUsageEvent
from app.api.skills import _skill_attachment_path
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    # An isolated private record, never assigned to an Agent or published.
    skill = Skill(name='attachment-release-smoke-' + str(time.time_ns()),
                  display_name='Skill attachment release smoke',
                  template_content='# Smoke', created_by=admin.username,
                  visibility='private', review_status='approved')
    db.session.add(skill)
    db.session.commit()
    skill_id = skill.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    live = os.getcwd() == '/opt/openclaw-web'
    headers = {}
    if live:
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/skills', headers=headers, timeout=5)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP service unavailable')
    def get(path):
        if live:
            return requests.get('http://127.0.0.1:18800' + path, headers=headers, timeout=20)
        return client.get(path)
    def body(response):
        return response.content if live else response.data
    def payload(response):
        return response.json() if live else response.json
    try:
        page = get('/skills')
        assert page.status_code == 200
        assert b'skill-attachment-files' in body(page) and b'showSkillAttachment' in body(page)
        data = b'# attachment smoke\nprint("not executed")\n'
        path = '/api/v1/skills/%s/attachments' % skill_id
        if live:
            upload = requests.post('http://127.0.0.1:18800' + path, headers=headers,
                                   files={'file': ('smoke.py', data)}, timeout=20)
        else:
            upload = client.post(path, data={'file': (io.BytesIO(data), 'smoke.py')})
        assert upload.status_code == 201, upload.status_code
        attachment = payload(upload)
        assert attachment['sha256'] == hashlib.sha256(data).hexdigest()
        detail = get(path + '/' + str(attachment['id']))
        assert detail.status_code == 200 and payload(detail)['text_preview'] == data.decode()
        download = get(attachment['download_url'])
        assert download.status_code == 200 and body(download) == data
        assert download.headers['X-Content-Type-Options'] == 'nosniff'
        pack = get('/api/v1/skills/%s/pack' % skill_id)
        assert pack.status_code == 200
        with zipfile.ZipFile(io.BytesIO(body(pack))) as archive:
            assert archive.read(skill.name + '/attachments/smoke.py') == data
            assert archive.read(skill.name + '/SKILL.md') == b'# Smoke'
        anonymous = app.test_client().get(path)
        assert anonymous.status_code == 401
        print('TEAM_RELEASE ' + json.dumps({
            'smoke': 'passed', 'live_http': live, 'upload_preview_download_pack': True,
            'workflow_runs_started': 0, 'workers_modified': False,
        }))
    finally:
        db.session.expire_all()
        SkillUsageEvent.query.filter_by(skill_id=skill_id).delete(synchronize_session=False)
        for attachment in SkillAttachment.query.filter_by(skill_id=skill_id).all():
            _skill_attachment_path(attachment).unlink(missing_ok=True)
            db.session.delete(attachment)
        db.session.flush()
        db.session.delete(db.session.get(Skill, skill_id))
        db.session.commit()
        folder = '/opt/openclaw-web/hub-store/skill-attachments/' + str(skill_id)
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
'''


if __name__ == '__main__':
    release.main()
