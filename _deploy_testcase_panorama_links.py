#!/usr/bin/env python3
"""Deploy testcase-panorama link feature and sync related skills."""
import os
import textwrap

import paramiko

ROOT = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.expanduser('~/.ssh/id_9.134.11.169')
HOST, PORT, USER = '9.134.11.169', 36000, 'root'
REMOTE_WEB = '/opt/openclaw-web'

FILES = [
    ('web/app/models.py', 'app/models.py'),
    ('web/app/__init__.py', 'app/__init__.py'),
    ('web/app/api/__init__.py', 'app/api/__init__.py'),
    ('web/app/api/testcases.py', 'app/api/testcases.py'),
    ('web/app/api/panorama.py', 'app/api/panorama.py'),
    ('web/app/api/testcase_panorama_links.py', 'app/api/testcase_panorama_links.py'),
    ('web/app/services/testcase_panorama_links.py', 'app/services/testcase_panorama_links.py'),
    ('web/templates/panorama.html', 'templates/panorama.html'),
    ('web/templates/testcases.html', 'templates/testcases.html'),
    ('openclaw-agent/skills/testcase-manager/SKILL.md',
     'openclaw-agent/skills/testcase-manager/SKILL.md'),
    ('openclaw-agent/skills/game-module-panorama/SKILL.md',
     'openclaw-agent/skills/game-module-panorama/SKILL.md'),
]


def _run(ssh, command, timeout=120):
    stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    if out:
        print(out)
    if err:
        print('STDERR:', err[:1200])
    return out, err


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, key_filename=KEY, timeout=30)
    sftp = ssh.open_sftp()
    for local, remote in FILES:
        local_path = os.path.join(ROOT, local)
        remote_path = f'{REMOTE_WEB}/{remote}'
        _run(ssh, f"mkdir -p {os.path.dirname(remote_path)}")
        sftp.put(local_path, remote_path)
        print('UPLOADED', remote)

    # Keep hub-store skill cache in sync with the package path.
    for skill in ('testcase-manager', 'game-module-panorama'):
        src = f'{REMOTE_WEB}/openclaw-agent/skills/{skill}/SKILL.md'
        dst = f'{REMOTE_WEB}/hub-store/skills/{skill}/SKILL.md'
        _run(ssh, f"mkdir -p {os.path.dirname(dst)} && cp {src} {dst}")
    sftp.close()

    sync_skills = textwrap.dedent(f'''
    import sys
    sys.path.insert(0, '{REMOTE_WEB}')
    from app import create_app, db
    from app.models import Skill

    app = create_app()
    with app.app_context():
        for name in ('testcase-manager', 'game-module-panorama'):
            path = f'{REMOTE_WEB}/openclaw-agent/skills/{{name}}/SKILL.md'
            s = Skill.query.filter_by(name=name).first()
            if not s:
                print('SKILL_NOT_FOUND', name)
                continue
            with open(path, 'r', encoding='utf-8') as f:
                s.template_content = f.read()
            s.review_status = 'approved'
            print('SKILL_SYNCED', s.id, s.name, len(s.template_content))
        db.session.commit()
    ''').strip()
    _run(ssh, f"cd {REMOTE_WEB} && ./venv/bin/python - <<'PY'\n{sync_skills}\nPY")

    _run(ssh, 'systemctl restart openclaw-web && sleep 3 && systemctl is-active openclaw-web', timeout=90)

    verify = textwrap.dedent(f'''
    import sys
    sys.path.insert(0, '{REMOTE_WEB}')
    from app import create_app, db
    from app.models import Skill

    app = create_app()
    with app.app_context():
        for table in ('testcase_panorama_links', 'panorama_module_test_metrics', 'test_case_change_logs'):
            rows = db.session.execute(f"SHOW TABLES LIKE '{{table}}'").fetchall()
            print('TABLE', table, bool(rows))
        rules = []
        for rule in app.url_map.iter_rules():
            text = str(rule)
            if 'testcase-panorama-links' in text or 'test-metrics' in text or 'testcase-changes' in text:
                rules.append(text)
        print('ROUTES', sorted(rules))
        for name in ('testcase-manager', 'game-module-panorama'):
            s = Skill.query.filter_by(name=name).first()
            content = s.template_content or ''
            print('VERIFY_SKILL', name,
                  'testcase-panorama-links' in content,
                  'testcase-changes' in content,
                  'test-metrics' in content,
                  'bug_risk_score' in content)
    ''').strip()
    _run(ssh, f"cd {REMOTE_WEB} && ./venv/bin/python - <<'PY'\n{verify}\nPY")
    ssh.close()


if __name__ == '__main__':
    main()
