import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from ops.codex_runtime_release import verify_contract, write_contract


def _wheel(path: Path, name: str, version: str, payload: bytes = b'ok') -> None:
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(
            f'{dist_info}/METADATA',
            f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n',
        )
        archive.writestr(f'{name.replace("-", "_")}/payload.bin', payload)


class CodexRuntimeReleaseTest(unittest.TestCase):
    def test_approved_linux_target_matches_unified_host(self):
        config_path = (Path(__file__).resolve().parents[1] / 'ops' /
                       'codex-runtime-linux-x86_64.json')
        config = json.loads(config_path.read_text(encoding='utf-8'))
        self.assertEqual('311', config['python_version'])
        self.assertEqual('cp311', config['abi'])

    def _runtime(self, root: Path) -> Path:
        runtime = root / 'runtime'
        wheelhouse = runtime / 'wheelhouse'
        wheelhouse.mkdir(parents=True)
        _wheel(
            wheelhouse / 'openai_codex-0.144.4-py3-none-any.whl',
            'openai-codex', '0.144.4',
        )
        _wheel(
            wheelhouse / 'openai_codex_cli_bin-0.144.4-py3-none-any.whl',
            'openai-codex-cli-bin', '0.144.4',
        )
        _wheel(
            wheelhouse / 'pydantic-2.12.5-py3-none-any.whl',
            'pydantic', '2.12.5',
        )
        return runtime

    def test_contract_locks_every_wheel_and_verifies_offline(self):
        with tempfile.TemporaryDirectory() as temp_name:
            runtime = self._runtime(Path(temp_name))
            manifest = write_contract(
                runtime,
                sdk_version='0.144.4',
                platform='manylinux_2_17_x86_64',
                python_version='310',
                implementation='cp',
                abi='cp310',
            )

            verified = verify_contract(runtime)
            lock = (runtime / 'requirements-codex.lock').read_text('utf-8')

        self.assertEqual(manifest, verified)
        self.assertIn('openai-codex==0.144.4 --hash=sha256:', lock)
        self.assertIn('pydantic==2.12.5 --hash=sha256:', lock)
        self.assertEqual(3, len(manifest['artifacts']))

    def test_verifier_rejects_a_tampered_wheel(self):
        with tempfile.TemporaryDirectory() as temp_name:
            runtime = self._runtime(Path(temp_name))
            write_contract(
                runtime,
                sdk_version='0.144.4',
                platform='manylinux_2_17_x86_64',
                python_version='310',
                implementation='cp',
                abi='cp310',
            )
            wheel = next((runtime / 'wheelhouse').glob('pydantic-*.whl'))
            wheel.write_bytes(wheel.read_bytes() + b'tampered')

            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                verify_contract(runtime)

    def test_verifier_rejects_manifest_lock_hash_tampering(self):
        with tempfile.TemporaryDirectory() as temp_name:
            runtime = self._runtime(Path(temp_name))
            write_contract(
                runtime,
                sdk_version='0.144.4',
                platform='manylinux_2_17_x86_64',
                python_version='310',
                implementation='cp',
                abi='cp310',
            )
            manifest_path = runtime / 'codex-runtime-manifest.json'
            manifest = json.loads(manifest_path.read_text('utf-8'))
            manifest['requirements_sha256'] = '0' * 64
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'lock SHA-256 mismatch'):
                verify_contract(runtime)

    def test_verifier_rejects_manifest_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_name:
            runtime = self._runtime(Path(temp_name))
            write_contract(
                runtime,
                sdk_version='0.144.4',
                platform='manylinux_2_17_x86_64',
                python_version='310',
                implementation='cp',
                abi='abi3',
            )
            manifest_path = runtime / 'codex-runtime-manifest.json'
            manifest = json.loads(manifest_path.read_text('utf-8'))
            manifest['requirements'] = '../requirements-codex.lock'
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'paths are not canonical'):
                verify_contract(runtime)


if __name__ == '__main__':
    unittest.main()
