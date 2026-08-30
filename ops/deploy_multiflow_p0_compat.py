#!/usr/bin/env python3
"""Compatibility-first P0 deploy helper for the legacy Hub host.

The remote worktree contains hot fixes that are not necessarily present in the
local Git base.  This helper merges changed Python symbols instead of replacing
whole core modules, stages and compiles everything, and only then switches the
files.  Database changes are additive and MariaDB 10.1 compatible.
"""

import argparse
import ast
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
from datetime import datetime

import paramiko


REMOTE_ROOT = "/opt/openclaw-web"
CORE_MODULES = (
    "app/models.py",
    "app/api/workflows.py",
    "app/api/wecom.py",
    "app/services/workflows.py",
    "app/api/__init__.py",
    "app/views/__init__.py",
)
FULL_FILES = (
    "app/api/shift_left.py",
    "app/api/analysis_rules.py",
    "app/api/automation_candidates.py",
    "app/api/automation_capabilities.py",
    "app/api/automation_closed_loop.py",
    "app/api/entity_relations.py",
    "app/api/resource_leases.py",
    "app/api/testcase_promotions.py",
    "app/services/shift_left.py",
    "app/services/analysis_rules.py",
    "app/services/automation_candidates.py",
    "app/services/automation_closed_loop.py",
    "app/services/capability_gaps.py",
    "app/services/entity_relations.py",
    "app/services/evidence_manifests.py",
    "app/services/post_resolver.py",
    "app/services/requirement_coverage.py",
    "app/services/resource_leases.py",
    "app/services/testcase_library_versioning.py",
    "app/services/workflow_library_snapshots.py",
    "app/services/workflow_result_ingestion.py",
    "app/services/test_case_delete.py",
    "templates/developer_ai_collaborate.html",
    "templates/automation_closed_loop.html",
)
TEMPLATE_MERGES = ("templates/base.html", "templates/workflows.html")
API_MODULES = (
    "shift_left",
    "automation_candidates",
    "automation_capabilities",
    "testcase_promotions",
    "resource_leases",
    "entity_relations",
    "analysis_rules",
    "automation_closed_loop",
)
MIGRATIONS = (
    "20260810_shift_left_mvp_a.sql",
    "20260812_automation_case_candidates.sql",
    "20260812_capability_gap_lifecycle.sql",
    "20260812_entity_relations.sql",
    "20260812_finding_feedback_analysis_rules.sql",
    "20260812_resource_leases_and_test_account_ttl.sql",
    "20260812_testcase_library_promotions.sql",
    "20260812_workflow_evidence_manifests.sql",
    "20260812_workflow_run_library_snapshots.sql",
    "20260813_automation_capabilities.sql",
    "20260813_terminal_run_gap_reconciliation.sql",
    "20260813_workflow_result_contracts.sql",
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def node_key(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (type(node).__name__, node.name)
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = []
        for target in targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
        if names:
            return ("assign", ",".join(names))
    return None


def node_hash(node):
    return sha256(ast.dump(node, include_attributes=False).encode("utf-8"))


def span(node):
    starts = [node.lineno]
    starts.extend(item.lineno for item in getattr(node, "decorator_list", ()))
    return min(starts), node.end_lineno


def source_for(source, node):
    lines = source.splitlines(True)
    start, end = span(node)
    return "".join(lines[start - 1:end]).rstrip() + "\n\n"


def module_nodes(source):
    tree = ast.parse(source)
    result = {}
    for node in tree.body:
        key = node_key(node)
        if key:
            result[key] = node
    return tree, result


def import_sources(source):
    tree = ast.parse(source)
    result = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.append((ast.dump(node, include_attributes=False), source_for(source, node).strip()))
    return result


def insert_after_docstring(source, text):
    tree = ast.parse(source)
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(getattr(tree.body[0], "value", None), (ast.Str, ast.Constant))):
        line = tree.body[0].end_lineno
        lines = source.splitlines(True)
        return "".join(lines[:line]) + "\n" + text.rstrip() + "\n\n" + "".join(lines[line:])
    return text.rstrip() + "\n\n" + source


def merge_python(remote, base, local):
    """Keep remote-only changes and overlay local symbols changed from base."""
    _, remote_nodes = module_nodes(remote)
    _, base_nodes = module_nodes(base)
    _, local_nodes = module_nodes(local)
    replacements = []
    additions = []
    for key, local_node in local_nodes.items():
        base_node = base_nodes.get(key)
        remote_node = remote_nodes.get(key)
        changed_locally = base_node is None or node_hash(local_node) != node_hash(base_node)
        missing_remotely = remote_node is None
        if not (changed_locally or missing_remotely):
            continue
        replacement = source_for(local, local_node)
        if remote_node is None:
            additions.append(replacement)
        else:
            start, end = span(remote_node)
            replacements.append((start, end, replacement))

    lines = remote.splitlines(True)
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start - 1:end] = [replacement]
    merged = "".join(lines).rstrip() + "\n"
    if additions:
        merged += "\n" + "".join(additions)

    remote_imports = {key for key, _ in import_sources(merged)}
    missing_imports = [text for key, text in import_sources(local) if key not in remote_imports]
    if missing_imports:
        merged = insert_after_docstring(merged, "\n".join(missing_imports))
    ast.parse(merged)
    return merged


def merge_api_init(remote):
    if all(re.search(r"\b%s\b" % re.escape(name), remote) for name in API_MODULES):
        return remote
    marker = "# 注册 Agent Hub 通信中心蓝图"
    if marker not in remote:
        marker = "# 娉ㄥ唽 Agent Hub"
    statement = "from app.api import %s  # noqa: F401\n\n" % ", ".join(API_MODULES)
    if marker in remote:
        return remote.replace(marker, statement + marker, 1)
    return remote.rstrip() + "\n\n" + statement


def merge_base_template(remote, local):
    if 'href="/automation-closed-loop"' in remote:
        return remote
    match = re.search(
        r'(?ms)^\s*<a href="/automation-closed-loop".*?</a>\s*', local)
    if not match:
        raise RuntimeError("automation closed-loop navigation block not found")
    block = match.group(0)
    target = re.search(r'(?m)^\s*<a href="/test-reports"', remote)
    if not target:
        raise RuntimeError("test-reports navigation anchor not found remotely")
    return remote[:target.start()] + block + remote[target.start():]


def _replace_js_function(remote, local, name):
    pattern = re.compile(
        r"(?m)^(?:async\s+)?function\s+%s\s*\(" % re.escape(name))
    local_match = pattern.search(local)
    remote_match = pattern.search(remote)
    if not local_match or not remote_match:
        raise RuntimeError("workflow template function anchor missing: " + name)
    next_function = re.compile(r"(?m)^(?:async\s+)?function\s+[A-Za-z_$]")
    local_next = next_function.search(local, local_match.end())
    remote_next = next_function.search(remote, remote_match.end())
    local_end = local_next.start() if local_next else len(local)
    remote_end = remote_next.start() if remote_next else len(remote)
    return remote[:remote_match.start()] + local[local_match.start():local_end] + remote[remote_end:]


def merge_workflows_template(remote, local):
    if ".wf-flow-node.contract-invalid" not in remote:
        css_start = local.index(".wf-flow-node.contract-invalid")
        css_end_marker = "@media (max-width: 760px) { .wf-conclusions { grid-template-columns:1fr; } }"
        css_end = local.index(css_end_marker, css_start) + len(css_end_marker)
        anchor = ".wf-flow-node.active"
        anchor_start = remote.index(anchor)
        anchor_end = remote.index("\n", anchor_start) + 1
        remote = remote[:anchor_end] + local[css_start:css_end] + "\n" + remote[anchor_end:]
    for name in (
            "stepDisplayStateBadge", "openRun", "renderRunFlowNode",
            "renderRunStepInspector"):
        remote = _replace_js_function(remote, local, name)
    if "const linkedRunId = Number(" in remote:
        return remote
    pattern = re.compile(
        r"(?m)^(?P<i>\s*)await loadDefinitions\(\);\s*\n"
        r"(?P=i)await loadFavoriteDefinitions\(\);\s*\n"
        r"(?P=i)await loadRuns\(\);\s*\n"
        r"(?P=i)if \(currentRunId\) await openRun\(currentRunId\);"
    )
    match = pattern.search(remote)
    if not match:
        raise RuntimeError("workflow loadAll compatibility anchor not found remotely")
    indent = match.group("i")
    replacement = (
        indent + "const linkedRunId = Number(\n"
        + indent + "    new URLSearchParams(location.search).get('run_id') || 0) || null;\n"
        + indent + "await loadDefinitions();\n"
        + indent + "await loadFavoriteDefinitions();\n"
        + indent + "await loadRuns(linkedRunId);\n"
        + indent + "if (currentRunId && !linkedRunId) await openRun(currentRunId);"
    )
    return remote[:match.start()] + replacement + remote[match.end():]


def merge_config(remote):
    if 'SHIFT_LEFT_ENABLED' in remote:
        return remote
    anchor = "    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key')\n"
    if anchor not in remote:
        raise RuntimeError('config SECRET_KEY anchor not found remotely')
    setting = (
        "\n    # Incremental capability; production remains disabled by default.\n"
        "    SHIFT_LEFT_ENABLED = os.getenv('SHIFT_LEFT_ENABLED', '0') in (\n"
        "        '1', 'true', 'True', 'yes', 'on')\n"
    )
    return remote.replace(anchor, anchor + setting, 1)


def exec_remote(client, command, timeout=120, check=True):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if check and rc:
        raise RuntimeError("remote command failed (%s): %s\n%s" % (rc, command, err or out))
    return rc, out, err


def remote_read(sftp, path):
    with sftp.open(path, "rb") as handle:
        return handle.read()


def remote_write(sftp, path, data):
    parent = posixpath.dirname(path)
    try:
        sftp.stat(parent)
    except IOError:
        parts = parent.strip("/").split("/")
        current = ""
        for part in parts:
            current += "/" + part
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)
    with sftp.open(path, "wb") as handle:
        handle.write(data)


def local_git_base(workspace, path):
    return subprocess.check_output(
        ["git", "show", "HEAD:web/" + path], cwd=workspace).decode("utf-8")


def build_files(workspace, sftp):
    built = {}
    for path in CORE_MODULES:
        remote = remote_read(sftp, REMOTE_ROOT + "/" + path).decode("utf-8")
        local_path = os.path.join(workspace, "web", *path.split("/"))
        with open(local_path, encoding="utf-8") as handle:
            local = handle.read()
        if path == "app/api/__init__.py":
            merged = merge_api_init(remote)
        else:
            merged = merge_python(remote, local_git_base(workspace, path), local)
        ast.parse(merged)
        built[path] = merged.encode("utf-8")

    for path in FULL_FILES:
        local_path = os.path.join(workspace, "web", *path.split("/"))
        with open(local_path, "rb") as handle:
            built[path] = handle.read()
    remote_base = remote_read(sftp, REMOTE_ROOT + "/templates/base.html").decode("utf-8")
    with open(os.path.join(workspace, "web", "templates", "base.html"), encoding="utf-8") as handle:
        local_base = handle.read()
    built["templates/base.html"] = merge_base_template(remote_base, local_base).encode("utf-8")
    remote_workflows = remote_read(
        sftp, REMOTE_ROOT + "/templates/workflows.html").decode("utf-8")
    with open(os.path.join(workspace, "web", "templates", "workflows.html"), encoding="utf-8") as handle:
        local_workflows = handle.read()
    built["templates/workflows.html"] = merge_workflows_template(
        remote_workflows, local_workflows).encode("utf-8")
    remote_config = remote_read(sftp, REMOTE_ROOT + "/config.py").decode("utf-8")
    built["config.py"] = merge_config(remote_config).encode("utf-8")
    return built


def migration_runner_source(sql_payload):
    # Executed after staged code is switched in. All schema changes are additive.
    template = r'''
import atexit, json, os, re
os.environ['SKIP_AUTO_MIGRATE'] = '1'
from app import create_app, db
from sqlalchemy import text

payload = json.loads(__SQL_PAYLOAD__)
app = create_app('production')

def table_exists(conn, name):
    return bool(conn.execute(text("SHOW TABLES LIKE :name"), {'name': name}).fetchone())

def columns(conn, table):
    if not table_exists(conn, table):
        return set()
    return {row[0] for row in conn.execute(text("SHOW COLUMNS FROM `%s`" % table)).fetchall()}

def indexes(conn, table):
    if not table_exists(conn, table):
        return set()
    return {row[2] for row in conn.execute(text("SHOW INDEX FROM `%s`" % table)).fetchall()}

def add_column(conn, table, name, ddl):
    if name not in columns(conn, table):
        conn.execute(text("ALTER TABLE `%s` ADD COLUMN `%s` %s" % (table, name, ddl)))
        print("ADD_COLUMN", table, name)

def add_index(conn, table, name, expression, unique=False):
    if name not in indexes(conn, table):
        conn.execute(text("ALTER TABLE `%s` ADD %sINDEX `%s` (%s)" % (
            table, "UNIQUE " if unique else "", name, expression)))
        print("ADD_INDEX", table, name)

with app.app_context():
    with db.engine.begin() as conn:
        # MariaDB 10.1 defaults to Antelope/767-byte indexes. Enable the
        # supported Barracuda large-prefix mode only while creating these
        # utf8mb4 tables, use DYNAMIC rows, then restore the server defaults.
        old_file_format = conn.execute(text(
            "SHOW GLOBAL VARIABLES LIKE 'innodb_file_format'")) .fetchone()[1]
        old_large_prefix = conn.execute(text(
            "SHOW GLOBAL VARIABLES LIKE 'innodb_large_prefix'")) .fetchone()[1]
        conn.execute(text("SET GLOBAL innodb_file_format='Barracuda'"))
        conn.execute(text("SET GLOBAL innodb_large_prefix=ON"))
        def restore_innodb_defaults():
            with app.app_context():
                with db.engine.begin() as restore_conn:
                    restore_conn.execute(text("SET GLOBAL innodb_large_prefix=%s" % (
                        'ON' if str(old_large_prefix).upper() in ('ON', '1') else 'OFF')))
                    restore_conn.execute(text(
                        "SET GLOBAL innodb_file_format='%s'" % old_file_format))
        atexit.register(restore_innodb_defaults)

        # CREATE TABLE statements only. JSON is LONGTEXT on MariaDB 10.1.
        for filename, raw in payload:
            cleaned = re.sub(r"--[^\n]*", "", raw)
            cleaned = re.sub(r"\bJSON\b", "LONGTEXT", cleaned, flags=re.I)
            cleaned = re.sub(
                r"ENGINE=InnoDB(?!\s+ROW_FORMAT)",
                "ENGINE=InnoDB ROW_FORMAT=DYNAMIC", cleaned, flags=re.I)
            for statement in cleaned.split(';'):
                statement = statement.strip()
                if not statement or not re.match(r"^CREATE\s+TABLE", statement, re.I):
                    continue
                conn.execute(text(statement))
            print("CREATE_TABLES", filename)

        add_column(conn, 'workflow_runs', 'idempotency_key', 'VARCHAR(128) NULL')
        add_column(conn, 'workflow_runs', 'idempotency_request_hash', 'VARCHAR(64) NULL')
        add_column(conn, 'workflow_runs', 'controller_run_id', 'VARCHAR(160) NULL')
        add_column(conn, 'workflow_runs', 'correlation_id', 'VARCHAR(160) NULL')
        add_column(conn, 'workflow_runs', 'trigger_source', 'VARCHAR(64) NULL')
        add_column(conn, 'workflow_runs', 'business_conclusion', "VARCHAR(64) DEFAULT ''")
        add_column(conn, 'workflow_runs', 'automation_conclusion', "VARCHAR(64) DEFAULT ''")
        add_column(conn, 'workflow_runs', 'evidence_ingest_status', "VARCHAR(64) DEFAULT ''")
        add_index(conn, 'workflow_runs', 'uq_workflow_run_definition_idempotency', '`definition_id`, `idempotency_key`', True)
        add_index(conn, 'workflow_runs', 'ix_workflow_runs_controller_run_id', '`controller_run_id`')
        add_index(conn, 'workflow_runs', 'ix_workflow_runs_correlation_id', '`correlation_id`')
        add_index(conn, 'workflow_runs', 'ix_workflow_runs_trigger_source', '`trigger_source`')
        add_index(conn, 'workflow_runs', 'ix_workflow_runs_controller_created', '`controller_run_id`, `created_at`')
        add_index(conn, 'workflow_runs', 'ix_workflow_runs_correlation_created', '`correlation_id`, `created_at`')
        add_column(conn, 'workflow_run_steps', 'target_post', "VARCHAR(80) DEFAULT ''")
        add_column(conn, 'workflow_run_steps', 'contract_result_json', 'LONGTEXT NULL')
        add_index(conn, 'workflow_run_steps', 'ix_workflow_run_steps_target_post', '`target_post`')

        add_column(conn, 'wecom_send_logs', 'workflow_run_id', 'INT NULL')
        add_column(conn, 'wecom_send_logs', 'workflow_step_id', "VARCHAR(100) DEFAULT ''")
        add_column(conn, 'wecom_send_logs', 'request_summary_json', 'LONGTEXT NULL')
        add_column(conn, 'wecom_send_logs', 'template_version', "VARCHAR(64) DEFAULT ''")
        add_column(conn, 'wecom_send_logs', 'message_hash', "VARCHAR(64) DEFAULT ''")
        add_column(conn, 'wecom_send_logs', 'receipt_json', 'LONGTEXT NULL')
        add_column(conn, 'wecom_send_logs', 'gate_result_json', 'LONGTEXT NULL')
        add_index(conn, 'wecom_send_logs', 'ix_wecom_send_logs_workflow_run', '`workflow_run_id`')
        add_index(conn, 'wecom_send_logs', 'ix_wecom_send_logs_message_hash', '`message_hash`')

        add_column(conn, 'test_case_libraries', 'revision', 'INT NOT NULL DEFAULT 0')
        add_column(conn, 'test_case_libraries', 'content_hash', "VARCHAR(71) NOT NULL DEFAULT ''")

        add_column(conn, 'test_accounts', 'lease_ttl_seconds', 'INT NULL DEFAULT 1800')
        add_column(conn, 'test_accounts', 'lease_expires_at', 'DATETIME NULL')
        add_column(conn, 'test_accounts', 'lease_heartbeat_at', 'DATETIME NULL')
        add_column(conn, 'test_accounts', 'workflow_run_id', 'INT NULL')
        add_column(conn, 'test_accounts', 'controller_run_id', 'VARCHAR(160) NULL')
        add_index(conn, 'test_accounts', 'ix_test_accounts_lease_expires', '`lease_expires_at`')
        add_index(conn, 'test_accounts', 'ix_test_accounts_workflow_run', '`workflow_run_id`')
        add_index(conn, 'test_accounts', 'ix_test_accounts_controller_run', '`controller_run_id`')
        add_column(conn, 'test_account_usage_logs', 'lease_id', 'INT NULL')
        add_column(conn, 'test_account_usage_logs', 'workflow_run_id', 'INT NULL')
        add_column(conn, 'test_account_usage_logs', 'controller_run_id', 'VARCHAR(160) NULL')

        for name, ddl in (
            ('required_operations_json', 'LONGTEXT NULL'),
            ('required_observables_json', 'LONGTEXT NULL'),
            ('required_reset_hooks_json', 'LONGTEXT NULL'),
            ('owner', "VARCHAR(160) DEFAULT ''"),
            ('development_requirement_ref', "VARCHAR(500) DEFAULT ''"),
            ('development_requirement_url', "VARCHAR(1000) DEFAULT ''"),
            ('development_requirement_status', "VARCHAR(64) DEFAULT ''"),
            ('resolution_json', 'LONGTEXT NULL'),
            ('resolved_by', "VARCHAR(160) DEFAULT ''"),
            ('resolved_at', 'DATETIME NULL'),
        ):
            add_column(conn, 'capability_gaps', name, ddl)
        add_index(conn, 'capability_gaps', 'ix_capability_gaps_owner', '`owner`')
        add_index(conn, 'capability_gaps', 'ix_capability_gaps_requirement_ref', '`development_requirement_ref`')
print('MIGRATION_OK')
'''
    return template.replace(
        '__SQL_PAYLOAD__', repr(json.dumps(sql_payload, ensure_ascii=False)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="root")
    parser.add_argument("--key", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = "/tmp/multiflow_p0_%s" % stamp
    backup = REMOTE_ROOT + "/backups/multiflow_p0_%s" % stamp
    backup_ready = False
    files_switched = False
    current_files = []
    new_files = []

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, port=args.port, username=args.user,
                   key_filename=args.key, timeout=20)
    sftp = client.open_sftp()
    try:
        built = build_files(workspace, sftp)
        exec_remote(client, "mkdir -p %s" % shlex.quote(stage))
        for path, data in built.items():
            remote_write(sftp, stage + "/" + path, data)

        python_files = [stage + "/" + path for path in built if path.endswith(".py")]
        compile_command = "%s/venv/bin/python -m py_compile %s" % (
            REMOTE_ROOT, " ".join(shlex.quote(path) for path in python_files))
        exec_remote(client, compile_command, timeout=180)
        print("STAGE_COMPILE_OK", len(python_files))
        if not args.apply:
            print("DRY_RUN_OK", stage)
            return 0

        exec_remote(client, "mkdir -p %s/files" % shlex.quote(backup))
        current_files = [path for path in built if remote_exists(sftp, REMOTE_ROOT + "/" + path)]
        new_files = [path for path in built if path not in current_files]
        for path in current_files:
            target = backup + "/files/" + path
            remote_write(sftp, target, remote_read(sftp, REMOTE_ROOT + "/" + path))

        baseline_code = r'''
import json, os
os.environ['SKIP_AUTO_MIGRATE']='1'
from app import create_app, db
from sqlalchemy import text
app=create_app('production')
with app.app_context():
 flow=db.session.execute(text("SELECT id, workflow_key, name, definition_json, updated_at FROM workflow_definitions WHERE id=12")).fetchone()
 library=db.session.execute(text("SELECT id, name, project_name, revision, content_hash, updated_at FROM test_case_libraries WHERE id=33")).fetchone() if 'revision' in {r[0] for r in db.session.execute(text("SHOW COLUMNS FROM test_case_libraries")).fetchall()} else db.session.execute(text("SELECT id, name, project_name, updated_at FROM test_case_libraries WHERE id=33")).fetchone()
 print(json.dumps({'flow12': list(flow) if flow else None, 'library33': list(library) if library else None}, ensure_ascii=False, default=str, sort_keys=True))
'''
        baseline_encoded = __import__('base64').b64encode(baseline_code.encode()).decode()
        _, baseline, _ = exec_remote(
            client,
            "cd %s && SKIP_AUTO_MIGRATE=1 venv/bin/python -c %s" % (
                shlex.quote(REMOTE_ROOT),
                shlex.quote("import base64;exec(base64.b64decode('%s'))" % baseline_encoded)),
            timeout=120)
        remote_write(sftp, backup + "/workflow_definitions_12_33.json", baseline.encode("utf-8"))

        dump_code = r'''
import os, subprocess
os.environ['SKIP_AUTO_MIGRATE']='1'
from app import create_app, db
app=create_app('production')
with app.app_context():
 u=db.engine.url
 env=os.environ.copy(); env['MYSQL_PWD']=u.password or ''
 cmd=['mysqldump','--single-transaction','--quick','--routines','--triggers','-h',u.host or 'localhost','-P',str(u.port or 3306),'-u',u.username,u.database]
 with open(%r,'wb') as f: subprocess.check_call(cmd,stdout=f,env=env)
''' % (backup + "/database.sql")
        encoded = __import__('base64').b64encode(dump_code.encode()).decode()
        exec_remote(client, "cd %s && SKIP_AUTO_MIGRATE=1 venv/bin/python -c %s" % (
            shlex.quote(REMOTE_ROOT),
            shlex.quote("import base64;exec(base64.b64decode('%s'))" % encoded)), timeout=600)
        backup_ready = True
        print("BACKUP_OK", backup)

        for path, data in built.items():
            remote_write(sftp, REMOTE_ROOT + "/" + path, data)
        files_switched = True

        sql_payload = []
        for filename in MIGRATIONS:
            with open(os.path.join(workspace, "ops", "migrations", filename), encoding="utf-8") as handle:
                sql_payload.append((filename, handle.read()))
        runner = migration_runner_source(sql_payload).encode("utf-8")
        remote_write(sftp, stage + "/migrate.py", runner)
        migrate_eval = "exec(compile(open(%r, encoding='utf-8').read(), %r, 'exec'))" % (
            stage + "/migrate.py", stage + "/migrate.py")
        exec_remote(client, "cd %s && SKIP_AUTO_MIGRATE=1 venv/bin/python -c %s" % (
            shlex.quote(REMOTE_ROOT), shlex.quote(migrate_eval)), timeout=600)

        initialize_code = r'''
import os
os.environ['SKIP_AUTO_MIGRATE']='1'
from app import create_app, db
from app.models import TestCaseLibrary
from app.services.testcase_library_versioning import ensure_library_revision
app=create_app('production')
with app.app_context():
 library=TestCaseLibrary.query.filter_by(id=33).with_for_update().first()
 assert library is not None, 'testcase library #33 was not found'
 revision, drifted=ensure_library_revision(library, 'migration')
 db.session.commit()
 print('LIBRARY33_OK revision=%s case_count=%s content_hash=%s drifted=%s' % (revision.revision, revision.case_count, revision.content_hash, str(drifted).lower()))
'''
        initialize_encoded = __import__('base64').b64encode(initialize_code.encode()).decode()
        exec_remote(client, "cd %s && SKIP_AUTO_MIGRATE=1 venv/bin/python -c %s" % (
            shlex.quote(REMOTE_ROOT),
            shlex.quote("import base64;exec(base64.b64decode('%s'))" % initialize_encoded)), timeout=180)

        # Import/routes check before touching the running workers.
        check_code = r'''
import os
os.environ['SKIP_AUTO_MIGRATE']='1'
from app import create_app
app=create_app('production')
rules={r.rule for r in app.url_map.iter_rules()}
required={'/api/v1/workflow-runs/latest','/api/v1/automation-closed-loop/overview','/automation-closed-loop'}
missing=sorted(required-rules)
assert not missing, missing
print('APP_IMPORT_OK',len(rules))
'''
        check_encoded = __import__('base64').b64encode(check_code.encode()).decode()
        exec_remote(client, "cd %s && SKIP_AUTO_MIGRATE=1 SHIFT_LEFT_ENABLED=0 venv/bin/python -c %s" % (
            shlex.quote(REMOTE_ROOT),
            shlex.quote("import base64;exec(base64.b64decode('%s'))" % check_encoded)), timeout=180)

        exec_remote(client, "systemctl restart openclaw-web", timeout=120)
        exec_remote(client, "systemctl is-active openclaw-web", timeout=60)
        rc, out, err = exec_remote(
            client,
            "curl -sS -o /tmp/multiflow_health.out -w '%{http_code}' http://127.0.0.1:18800/",
            timeout=60, check=False)
        if rc or out.strip() not in ("200", "302"):
            raise RuntimeError("health check failed rc=%s http=%s %s" % (rc, out, err))

        # Flow #12 definition must remain byte-for-byte unchanged. Library #33
        # may only gain its revision/hash registration; testcase content is not touched.
        _, after, _ = exec_remote(
            client,
            "cd %s && SKIP_AUTO_MIGRATE=1 venv/bin/python -c %s" % (
                shlex.quote(REMOTE_ROOT),
                shlex.quote("import base64;exec(base64.b64decode('%s'))" % baseline_encoded)),
            timeout=120)
        before_payload = json.loads(baseline.strip().splitlines()[-1])
        after_payload = json.loads(after.strip().splitlines()[-1])
        if before_payload.get('flow12') != after_payload.get('flow12'):
            raise RuntimeError('Flow Definition #12 changed during deployment')
        print("DEPLOY_OK", backup, "HTTP", out.strip())
        return 0
    except Exception:
        if backup_ready and files_switched:
            try:
                for path in current_files:
                    remote_write(
                        sftp, REMOTE_ROOT + "/" + path,
                        remote_read(sftp, backup + "/files/" + path))
                for path in new_files:
                    target = REMOTE_ROOT + "/" + path
                    if not target.startswith(REMOTE_ROOT + "/"):
                        raise RuntimeError("unsafe rollback path: " + target)
                    if remote_exists(sftp, target):
                        sftp.remove(target)
                exec_remote(client, "systemctl restart openclaw-web", timeout=120)
                exec_remote(client, "systemctl is-active openclaw-web", timeout=60)
                print("ROLLBACK_OK", backup, file=sys.stderr)
            except Exception as rollback_error:
                print("ROLLBACK_FAILED %s" % rollback_error, file=sys.stderr)
        print("DEPLOY_FAILED stage=%s backup=%s" % (stage, backup), file=sys.stderr)
        raise
    finally:
        sftp.close()
        client.close()


def remote_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
