import importlib.util
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / 'ops' / 'verify_multiflow_readiness.py'
_SPEC = importlib.util.spec_from_file_location(
    'verify_multiflow_readiness', _MODULE_PATH)
readiness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(readiness)


class _Inspector:
    def __init__(self, schema):
        self.schema = schema

    def get_table_names(self):
        return list(self.schema)

    def get_columns(self, table):
        return [{'name': name} for name in self.schema[table]]


class MultiFlowReadinessTest(unittest.TestCase):
    def test_acceptance_runner_keeps_every_at_case(self):
        runner = (_ROOT / 'ops' / 'run_multiflow_acceptance.ps1').read_text(
            encoding='utf-8')

        for number in range(1, 13):
            self.assertIn(f'test_at{number:02d}', runner)

    def test_complete_schema_passes(self):
        schema = {
            table: set(columns)
            for table, columns in readiness.REQUIRED_TABLE_COLUMNS.items()
        }

        result = readiness.check_database_schema(_Inspector(schema))

        self.assertEqual(result['status'], 'pass')

    def test_missing_table_and_column_are_reported_together(self):
        schema = {
            table: set(columns)
            for table, columns in readiness.REQUIRED_TABLE_COLUMNS.items()
            if table != 'analysis_rule_replays'
        }
        schema['workflow_runs'].remove('correlation_id')

        result = readiness.check_database_schema(_Inspector(schema))

        self.assertEqual(result['status'], 'fail')
        self.assertIn(
            'analysis_rule_replays', result['details']['missing_tables'])
        self.assertEqual(
            result['details']['missing_columns']['workflow_runs'],
            ['correlation_id'])

    def test_migration_asset_check_lists_missing_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / 'ops' / 'migrations').mkdir(parents=True)
            present = readiness.REQUIRED_MIGRATIONS[0]
            (root / 'ops' / 'migrations' / present).write_text(
                '-- present', encoding='utf-8')

            result = readiness.check_migration_assets(root)

        self.assertEqual(result['status'], 'fail')
        self.assertNotIn(present, result['details']['missing'])
        self.assertEqual(
            len(result['details']['missing']),
            len(readiness.REQUIRED_MIGRATIONS) - 1)

    def test_warnings_do_not_block_readiness_but_failures_do(self):
        ready = readiness.finalize_report([
            {'status': 'pass'}, {'status': 'warn'},
        ])
        blocked = readiness.finalize_report([
            {'status': 'pass'}, {'status': 'fail'},
        ])

        self.assertTrue(ready['ready'])
        self.assertFalse(blocked['ready'])
        self.assertEqual(ready['summary']['warn'], 1)
        self.assertEqual(blocked['summary']['fail'], 1)

    def test_shift_left_can_be_warning_or_strict_failure(self):
        warning = readiness.check_runtime_config(
            {'SHIFT_LEFT_ENABLED': False})
        strict = readiness.check_runtime_config(
            {'SHIFT_LEFT_ENABLED': False}, require_enabled=True)

        self.assertEqual(warning['status'], 'warn')
        self.assertEqual(strict['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
