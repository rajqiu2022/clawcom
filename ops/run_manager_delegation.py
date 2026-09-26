"""Run ``ops/manager_delegation.py`` on the Hub host from this workstation.

The CLI is the canonical write path for temporary manager handovers, and it must
execute against production's own environment (``load_service_env`` inherits the
running service's ``/proc/<MainPID>/environ``). This wrapper stages the CLI into
a private temp dir on the host, executes it with the service venv, relays the
``MANAGER_DELEGATION <json>`` report and removes the stage.

The CLI itself is dry-run unless ``--apply`` is passed, so the wrapper never adds
authority: it only transports it.

    python ops/run_manager_delegation.py -- --team-id 1 --status
    python ops/run_manager_delegation.py -- --migrate --apply
    python ops/run_manager_delegation.py -- --team-id 1 --delegate-claw-id 61 \
        --reason "Provider 额度耗尽" --apply
    python ops/run_manager_delegation.py -- --team-id 1 --revoke --apply
"""
import hashlib
import re
import shlex
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paramiko  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'ops' / 'manager_delegation.py'
REMOTE = '/opt/openclaw-web'
HOST, PORT, USER = '9.134.11.169', 36000, 'root'
KEY = 'C:/Users/rajqiu/.ssh/id_9.134.11.169'


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == '--':
        argv = argv[1:]
    payload = CLI.read_bytes().replace(b'\r\n', b'\n')

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(HOST, port=PORT, username=USER, key_filename=KEY, timeout=25)
    stage = '/tmp/manager-delegation-' + uuid.uuid4().hex
    try:
        _, out, _ = client.exec_command('mkdir -m 700 ' + stage, timeout=60)
        out.read()
        with client.open_sftp() as sftp:
            with sftp.open(stage + '/cli.py', 'wb') as stream:
                stream.write(payload)
        cmd = ('cd %s && PYTHONPATH=%s ./venv/bin/python %s/cli.py %s'
               % (REMOTE, REMOTE, stage, ' '.join(shlex.quote(a) for a in argv)))
        _, stdout, stderr = client.exec_command(cmd, timeout=300)
        text = stdout.read().decode('utf-8', 'replace')
        raw_error = stderr.read().decode('utf-8', 'replace')
        code = stdout.channel.recv_exit_status()
    finally:
        try:
            _, cleanup, _ = client.exec_command('rm -rf ' + shlex.quote(stage), timeout=60)
            cleanup.read()
        finally:
            client.close()

    print('CLI_SHA256 ' + hashlib.sha256(payload).hexdigest()[:16])
    report = None
    for line in text.splitlines():
        if line.startswith('MANAGER_DELEGATION '):
            print(line)
            report = line.split(' ', 1)[1]
        else:
            print(line)
    if code:
        # Only frames and exception kinds: DB diagnostics can carry secrets.
        frames = [l.strip() for l in raw_error.splitlines() if l.lstrip().startswith('File ')]
        kinds = re.findall(r'^[A-Za-z_.]*(?:Error|Exception):.*$', raw_error, re.M)
        print('REMOTE_ERROR rc=%s' % code)
        for line in frames[-4:]:
            print('  ' + line)
        for line in kinds[-2:]:
            print('  ' + line[:300])
    return 0 if code == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
