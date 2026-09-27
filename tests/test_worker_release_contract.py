import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return _Noop()


for name, attrs in (
    ('flask_cors', {'CORS': _Noop}),
    ('flask_socketio', {
        'SocketIO': _Noop, 'emit': _Noop(),
        'join_room': _Noop(), 'leave_room': _Noop(),
    }),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

from flask import Flask  # noqa: E402
from app import db  # noqa: E402
from app.models import WorkerRelease  # noqa: E402
from app.services.worker_releases import (  # noqa: E402
    import_latest_candidate,
    verify_catalog_release,
)


WORKER_CATALOG = Path('F:/Code/claw-worker-windows-v2/dist/worker-releases')


class WorkerReleaseContractTest(unittest.TestCase):
    @unittest.skipUnless(WORKER_CATALOG.is_dir(), 'local Worker release catalog unavailable')
    def test_latest_worker_catalog_is_importable(self):
        release = verify_catalog_release(WORKER_CATALOG)
        self.assertEqual('linux-x86_64', release.platform)
        self.assertRegex(release.source_commit, r'^[0-9a-f]{40}$')
        self.assertRegex(release.artifact_sha256, r'^[0-9a-f]{64}$')
        self.assertTrue(release.artifact_path.is_file())

    @unittest.skipUnless(WORKER_CATALOG.is_dir(), 'local Worker release catalog unavailable')
    def test_latest_windows_worker_catalog_is_importable(self):
        release = verify_catalog_release(
            WORKER_CATALOG, platform='windows-x86_64')
        self.assertEqual('windows-x86_64', release.platform)
        self.assertTrue(release.artifact_path.is_file())

    @unittest.skipUnless(WORKER_CATALOG.is_dir(), 'local Worker release catalog unavailable')
    def test_latest_intel_macos_worker_catalog_is_importable(self):
        release = verify_catalog_release(
            WORKER_CATALOG, platform='macos-x86_64')
        self.assertEqual('macos-x86_64', release.platform)
        self.assertTrue(release.artifact_path.is_file())

    @unittest.skipUnless(WORKER_CATALOG.is_dir(), 'local Worker release catalog unavailable')
    def test_tampered_worker_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / 'worker-releases'
            shutil.copytree(WORKER_CATALOG, copied)
            release = verify_catalog_release(copied)
            content = bytearray(release.artifact_path.read_bytes())
            content[-1] ^= 1
            release.artifact_path.write_bytes(content)
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                verify_catalog_release(copied)

    def test_hub_deployer_uses_worker_installer_for_release_path(self):
        source = (WEB / 'app' / 'services' / 'agent_deployer.py').read_text(
            encoding='utf-8')
        self.assertIn('def _deploy_claw_worker_release', source)
        self.assertIn('scripts/install-linux.sh', source)
        self.assertIn('worker_release_id', source)

    def test_frontend_requires_approved_worker_release(self):
        template = (WEB / 'templates' / 'openclaws.html').read_text(
            encoding='utf-8')
        self.assertIn('create-worker-release', template)
        self.assertIn('refreshWorkerReleaseCatalog', template)
        self.assertIn('approveLatestWorkerRelease', template)
        self.assertIn('worker_release_record_id: workerReleaseId', template)
        api_source = (WEB / 'app' / 'api' / 'worker_releases.py').read_text(
            encoding='utf-8')
        self.assertIn("'/worker-releases/refresh'", api_source)
        self.assertIn("confirm_unsigned", api_source)


@unittest.skipUnless(WORKER_CATALOG.is_dir(), 'local Worker release catalog unavailable')
class WorkerReleaseImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__, instance_path=self.temp.name)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            TESTING=True,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.temp.cleanup()

    def test_import_is_idempotent_and_keeps_artifact_path_private(self):
        environment = {
            'WORKER_RELEASE_REPOSITORY_PATH': str(WORKER_CATALOG.parents[1]),
            'WORKER_RELEASE_STORE_ROOT': str(Path(self.temp.name) / 'store'),
        }
        with patch.dict('os.environ', environment, clear=False):
            first, created = import_latest_candidate('admin')
            second, created_again = import_latest_candidate('admin')
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, WorkerRelease.query.count())
        self.assertNotIn('artifact_path', first.to_dict())
        self.assertTrue(Path(first.artifact_path).is_file())

    def test_import_windows_release_is_platform_scoped(self):
        environment = {
            'WORKER_RELEASE_REPOSITORY_PATH': str(WORKER_CATALOG.parents[1]),
            'WORKER_RELEASE_STORE_ROOT': str(Path(self.temp.name) / 'store'),
        }
        with patch.dict('os.environ', environment, clear=False):
            linux, _ = import_latest_candidate('admin')
            windows, created = import_latest_candidate(
                'admin', platform='windows-x86_64')
        self.assertTrue(created)
        self.assertEqual(linux.release_id, windows.release_id)
        self.assertEqual('windows-x86_64', windows.platform)
        self.assertEqual(2, WorkerRelease.query.count())

    def test_import_macos_release_is_platform_scoped(self):
        environment = {
            'WORKER_RELEASE_REPOSITORY_PATH': str(WORKER_CATALOG.parents[1]),
            'WORKER_RELEASE_STORE_ROOT': str(Path(self.temp.name) / 'store'),
        }
        with patch.dict('os.environ', environment, clear=False):
            macos, created = import_latest_candidate(
                'admin', platform='macos-x86_64')
        self.assertTrue(created)
        self.assertEqual('macos-x86_64', macos.platform)
        self.assertEqual(1, WorkerRelease.query.count())


if __name__ == '__main__':
    unittest.main()
