#!/usr/bin/env python3
"""OpenClaw Hub Workflow Worker.

Polls Hub workflow tasks, claims supported runner steps, executes configured
local commands, and posts structured step results back to Hub.
"""
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request


HUB_URL = os.getenv('HUB_URL', '').rstrip('/')
CLAW_TOKEN = os.getenv('CLAW_TOKEN') or os.getenv('HUB_API_TOKEN') or ''
CLAW_ID = os.getenv('CLAW_ID', '').strip()
WORKER_ID = os.getenv('WORKFLOW_WORKER_ID') or (
    f"{socket.gethostname()}:{CLAW_ID or 'unknown'}:{os.getpid()}"
)
RUNNER_CONFIG = os.getenv('WORKFLOW_RUNNER_CONFIG', '/etc/openclaw-workflow-runners.json')
POLL_SEC = int(os.getenv('WORKFLOW_POLL_SEC', '10'))
LEASE_SEC = int(os.getenv('WORKFLOW_LEASE_SEC', '300'))
HEARTBEAT_SEC = int(os.getenv('WORKFLOW_HEARTBEAT_SEC', '30'))
COMMAND_TIMEOUT_SEC = int(os.getenv('WORKFLOW_COMMAND_TIMEOUT_SEC', '3600'))
DRY_RUN = os.getenv('WORKFLOW_WORKER_DRY_RUN', '0') in ('1', 'true', 'True')


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def http(method, path, body=None, timeout=30):
    if not HUB_URL or not CLAW_TOKEN:
        raise RuntimeError('HUB_URL and CLAW_TOKEN/HUB_API_TOKEN are required')
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(f'{HUB_URL}{path}', data=data, method=method)
    req.add_header('Authorization', f'Bearer {CLAW_TOKEN}')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode('utf-8') or '{}'
            return res.status, json.loads(raw) if raw.strip().startswith(('{', '[')) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace') or '{}'
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {'error': raw[:500]}
        return exc.code, payload


def load_runner_config(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get('runners'), dict):
        data = data['runners']
    return data if isinstance(data, dict) else {}


def supported_runner(config, runner):
    value = config.get(runner)
    return value if isinstance(value, dict) and value.get('command') else None


def build_command(spec):
    command = spec.get('command')
    if isinstance(command, list):
        return [str(x) for x in command], False
    return str(command), bool(spec.get('shell'))


def parse_result(stdout, returncode, elapsed_ms):
    text = (stdout or '').strip()
    if text:
        candidates = [text]
        if '\n' in text:
            candidates.append(text.splitlines()[-1].strip())
        for candidate in candidates:
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    result.setdefault('status', 'passed' if returncode == 0 else 'blocked')
                    result.setdefault('logs', {})
                    result['logs'].setdefault('worker_elapsed_ms', elapsed_ms)
                    return result
            except Exception:
                pass
    if returncode == 0:
        return {
            'status': 'passed',
            'summary': 'Workflow runner command completed',
            'metrics': {'returncode': returncode},
            'logs': {'stdout': text[-4000:], 'worker_elapsed_ms': elapsed_ms},
        }
    return {
        'status': 'blocked',
        'summary': f'Workflow runner command failed: returncode={returncode}',
        'metrics': {'returncode': returncode},
        'blocker': {
            'type': 'runner_command_failed',
            'message': f'command exited with {returncode}',
            'suggested_action': '检查 workflow worker 日志和本地 runner 脚本',
        },
        'logs': {'stdout': text[-4000:], 'worker_elapsed_ms': elapsed_ms},
    }


def heartbeat_path(task):
    return f"/api/v1/workflow-runs/{task['run_id']}/steps/{task['step_id']}/heartbeat"


def progress_path(task):
    return task.get('progress_api') or f"/api/v1/workflow-runs/{task['run_id']}/steps/{task['step_id']}/progress"


def post_heartbeat(task, http_func=http):
    return http_func('POST', heartbeat_path(task), {
        'worker_id': WORKER_ID,
    }, timeout=15)


def post_progress(task, phase, message, percent=None, progress=None, heartbeat=False, http_func=http):
    body = {
        'worker_id': WORKER_ID,
        'phase': phase,
        'message': message,
        'heartbeat': bool(heartbeat),
    }
    if percent is not None:
        body['percent'] = percent
    if isinstance(progress, dict) and progress:
        body['progress'] = progress
    return http_func('POST', progress_path(task), body, timeout=15)


def safe_post_progress(task, phase, message, percent=None, progress=None, heartbeat=False):
    try:
        code, body = post_progress(task, phase, message, percent=percent, progress=progress, heartbeat=heartbeat)
        if code not in (200, 201):
            log(f"[progress] failed run={task.get('run_id')} step={task.get('step_id')} code={code} body={body}")
    except Exception as exc:
        log(f"[progress] error run={task.get('run_id')} step={task.get('step_id')}: {type(exc).__name__}: {exc}")


def heartbeat_loop(task, stop_event, interval_sec=HEARTBEAT_SEC, http_func=http):
    while True:
        try:
            code, body = post_heartbeat(task, http_func=http_func)
            if code not in (200, 201):
                log(f"[heartbeat] failed run={task.get('run_id')} step={task.get('step_id')} code={code} body={body}")
        except Exception as exc:
            log(f"[heartbeat] error run={task.get('run_id')} step={task.get('step_id')}: {type(exc).__name__}: {exc}")
        if stop_event.wait(max(1, int(interval_sec or HEARTBEAT_SEC))):
            return


def start_heartbeat_thread(task, interval_sec=HEARTBEAT_SEC, http_func=http):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=heartbeat_loop,
        args=(task, stop_event, interval_sec, http_func),
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def execute_runner(task, spec):
    if DRY_RUN:
        return {
            'status': 'passed',
            'summary': f"DRY_RUN: {task.get('runner')} simulated by {WORKER_ID}",
            'metrics': {'dry_run': True},
            'evidence': {'device_id': socket.gethostname()},
            'logs': {'worker_id': WORKER_ID},
        }

    fd, task_path = tempfile.mkstemp(prefix='workflow-task-', suffix='.json')
    heartbeat_stop = None
    heartbeat_thread = None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        cmd, shell = build_command(spec)
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (spec.get('env') or {}).items()})
        env['WORKFLOW_TASK_JSON'] = task_path
        env['WORKFLOW_RUN_ID'] = str(task.get('run_id') or '')
        env['WORKFLOW_STEP_ID'] = str(task.get('step_id') or '')
        env['WORKFLOW_RUNNER'] = str(task.get('runner') or '')
        env['WORKFLOW_PROGRESS_API'] = f"{HUB_URL}{progress_path(task)}"
        env['WORKFLOW_HEARTBEAT_API'] = f"{HUB_URL}{heartbeat_path(task)}"
        timeout = int(spec.get('timeout_sec') or COMMAND_TIMEOUT_SEC)
        cwd = spec.get('cwd') or None
        log(f"[runner] start {task.get('runner')} run={task.get('run_id')} step={task.get('step_id')}")
        start = time.time()
        heartbeat_stop, heartbeat_thread = start_heartbeat_thread(task)
        safe_post_progress(task, 'runner_started', 'workflow worker 已启动本地 runner', percent=1, heartbeat=True)
        proc = subprocess.run(
            cmd if shell or isinstance(cmd, list) else shlex.split(cmd),
            shell=shell,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        result = parse_result(proc.stdout, proc.returncode, elapsed_ms)
        safe_post_progress(
            task,
            'runner_completed' if proc.returncode == 0 else 'runner_blocked',
            result.get('summary') or 'workflow runner 已结束',
            percent=100 if proc.returncode == 0 else None,
            heartbeat=True,
        )
        if proc.stderr:
            result.setdefault('logs', {})['stderr'] = proc.stderr[-4000:]
        return result
    except subprocess.TimeoutExpired as exc:
        return {
            'status': 'blocked',
            'summary': f'Workflow runner timeout after {exc.timeout}s',
            'blocker': {
                'type': 'runner_timeout',
                'message': f'command timed out after {exc.timeout}s',
                'suggested_action': '检查本地脚本是否卡住，必要时终止 Run 或重试 Step',
            },
            'logs': {'error_excerpt': str(exc)},
        }
    except Exception as exc:
        return {
            'status': 'blocked',
            'summary': f'Workflow runner exception: {type(exc).__name__}',
            'blocker': {
                'type': 'runner_exception',
                'message': str(exc),
                'suggested_action': '检查 workflow worker 配置和 runner 脚本',
            },
            'logs': {'traceback': traceback.format_exc()[-4000:]},
        }
    finally:
        if heartbeat_stop:
            heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=5)
        try:
            os.remove(task_path)
        except Exception:
            pass


def claim_task(task):
    path = f"/api/v1/workflow-runs/{task['run_id']}/steps/{task['step_id']}/claim"
    code, body = http('POST', path, {
        'worker_id': WORKER_ID,
        'lease_seconds': LEASE_SEC,
    })
    if code == 200:
        return True
    log(f"[claim] skip run={task.get('run_id')} step={task.get('step_id')} code={code} body={body}")
    return False


def post_result(task, result):
    path = f"/api/v1/workflow-runs/{task['run_id']}/steps/{task['step_id']}/result"
    code, body = http('POST', path, result, timeout=60)
    if code not in (200, 201):
        raise RuntimeError(f'post result failed code={code} body={body}')
    return body


def loop_once(config):
    code, tasks = http('GET', '/api/v1/workflow-runs/worker/tasks', timeout=30)
    if code != 200:
        log(f'[tasks] fetch failed code={code} body={tasks}')
        return
    if not isinstance(tasks, list):
        log(f'[tasks] unexpected payload: {tasks}')
        return
    for task in tasks:
        runner = task.get('runner') or ''
        spec = supported_runner(config, runner)
        if not spec:
            continue
        if not claim_task(task):
            continue
        result = execute_runner(task, spec)
        post_result(task, result)
        log(f"[result] posted run={task.get('run_id')} step={task.get('step_id')} status={result.get('status')}")


def main():
    config = load_runner_config(RUNNER_CONFIG)
    log(f"[start] worker_id={WORKER_ID} dry_run={DRY_RUN} runners={sorted(config.keys())}")
    while True:
        try:
            loop_once(config)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f'[loop] error: {type(exc).__name__}: {exc}')
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
