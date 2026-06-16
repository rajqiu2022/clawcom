#!/usr/bin/env python3
"""Deploy custom test report categories."""
import os
import textwrap

import paramiko

ROOT = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.expanduser('~/.ssh/id_9.134.11.169')
HOST, PORT, USER = '9.134.11.169', 36000, 'root'
REMOTE = '/opt/openclaw-web'

FILES = [
    ('web/app/models.py', 'app/models.py'),
    ('web/app/__init__.py', 'app/__init__.py'),
    ('web/app/api/test_reports.py', 'app/api/test_reports.py'),
    ('web/app/services/test_report_categories.py', 'app/services/test_report_categories.py'),
    ('web/templates/test_reports.html', 'templates/test_reports.html'),
    ('openclaw-agent/skills/test-report-manager/SKILL.md',
     'openclaw-agent/skills/test-report-manager/SKILL.md'),
]


def run(ssh, cmd, timeout=90):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    print('$', cmd.split('\n')[0][:120])
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
        remote_path = f'{REMOTE}/{remote}'
        run(ssh, f"mkdir -p {os.path.dirname(remote_path)}")
        sftp.put(os.path.join(ROOT, local), remote_path)
        print('uploaded', remote)
    sftp.close()

    run(ssh, (
        f"cd {REMOTE} && ./venv/bin/python -m py_compile "
        "app/models.py app/__init__.py app/api/test_reports.py app/services/test_report_categories.py"
    ))
    run(ssh, f"mkdir -p {REMOTE}/hub-store/skills/test-report-manager && "
             f"cp {REMOTE}/openclaw-agent/skills/test-report-manager/SKILL.md "
             f"{REMOTE}/hub-store/skills/test-report-manager/SKILL.md")

    sync_skill = textwrap.dedent(f'''
    import sys
    sys.path.insert(0, '{REMOTE}')
    from app import create_app, db
    from app.models import Skill

    app = create_app()
    with app.app_context():
        s = Skill.query.filter_by(name='test-report-manager').first()
        if not s:
            print('SKILL_NOT_FOUND test-report-manager')
        else:
            with open('{REMOTE}/openclaw-agent/skills/test-report-manager/SKILL.md', 'r', encoding='utf-8') as f:
                s.template_content = f.read()
            s.review_status = 'approved'
            db.session.commit()
            print('SKILL_SYNCED', s.id, s.name, len(s.template_content))
    ''').strip()
    run(ssh, f"cd {REMOTE} && ./venv/bin/python - <<'PY'\n{sync_skill}\nPY", timeout=120)

    run(ssh, 'systemctl restart openclaw-web && sleep 3 && systemctl is-active openclaw-web')

    verify = textwrap.dedent(f'''
    import sys
    sys.path.insert(0, '{REMOTE}')
    from app import create_app, db

    app = create_app()
    with app.app_context():
        tables = db.session.execute("SHOW TABLES LIKE 'test_report_custom_categories'").fetchall()
        col = db.session.execute("SHOW COLUMNS FROM test_reports LIKE 'custom_category_key'").fetchall()
        rules = sorted(str(r) for r in app.url_map.iter_rules()
                       if 'custom-categories' in str(r) or 'custom-category-reports' in str(r))
        print('TABLE_CUSTOM_CATEGORIES', bool(tables))
        print('COLUMN_CUSTOM_CATEGORY_KEY', bool(col))
        print('ROUTES', rules)
        s = __import__('app.models', fromlist=['Skill']).Skill.query.filter_by(name='test-report-manager').first()
        content = s.template_content or ''
        print('VERIFY_SKILL test-report-manager',
              'custom-category-reports' in content,
              'custom_category_key' in content,
              '每日代码分析报告' in content)
    ''').strip()
    run(ssh, f"cd {REMOTE} && ./venv/bin/python - <<'PY'\n{verify}\nPY", timeout=120)
    ssh.close()


if __name__ == '__main__':
    main()
