"""Import and verify immutable Claw Worker release bundles."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from urllib.parse import urlsplit

from flask import current_app

from app import db
from app.models import WorkerRelease


_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')
PLATFORM = 'linux-x86_64'
SUPPORTED_PLATFORMS = (
    'linux-x86_64',
    'windows-x86_64',
    'macos-arm64',
    'macos-x86_64',
)
POSIX_PLATFORM_INSTALL_ENTRIES = {
    'linux-x86_64': 'scripts/install-linux.sh',
    'macos-arm64': 'scripts/install-macos.sh',
    'macos-x86_64': 'scripts/install-macos.sh',
}
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_FILES = 5000
MAX_PACKAGE_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class VerifiedWorkerRelease:
    release_id: str
    source_repository: str
    source_ref: str
    source_commit: str
    committed_at: str
    channel: str
    signature_status: str
    platform: str
    release_manifest_sha256: str
    platform_manifest_sha256: str
    package_manifest_sha256: str
    artifact_filename: str
    artifact_sha256: str
    artifact_size: int
    artifact_path: Path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _json_object(content: bytes, label: str) -> dict:
    try:
        value = json.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{label} 不是有效 UTF-8 JSON') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{label} 必须是 JSON 对象')
    return value


def _safe_child(root: Path, relative: str, label: str) -> Path:
    pure = PurePosixPath(str(relative or ''))
    if not relative or pure.is_absolute() or '..' in pure.parts:
        raise ValueError(f'{label} 路径非法')
    path = (root / Path(*pure.parts)).resolve(strict=True)
    if root != path and root not in path.parents:
        raise ValueError(f'{label} 越出发布根目录')
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'{label} 必须是普通文件')
    return path


def _digest_file_value(path: Path, expected_name: str) -> str:
    parts = path.read_text(encoding='ascii').strip().split()
    if len(parts) != 2 or parts[1] != expected_name or not _SHA256_RE.fullmatch(parts[0]):
        raise ValueError(f'{path.name} 格式非法')
    return parts[0]


def _verify_posix_archive(path: Path, package_sha256: str,
                          source_commit: str, platform: str) -> None:
    install_entry = POSIX_PLATFORM_INSTALL_ENTRIES[platform]
    label = 'Linux' if platform == PLATFORM else 'macOS'
    expected_manifest_name = 'claw-worker/release/package-manifest.json'
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        if (len(members) > MAX_PACKAGE_FILES + 1
                or any(item.size < 0 or item.size > MAX_PACKAGE_FILE_BYTES
                       for item in members)
                or sum(item.size for item in members) > MAX_PACKAGE_TOTAL_BYTES):
            raise ValueError(f'Worker {label} 归档超过安全大小限制')
        names = [member.name for member in members]
        if len(names) != len(set(names)) or expected_manifest_name not in names:
            raise ValueError(f'Worker {label} 归档成员非法')
        payload = {}
        package_bytes = None
        for member in members:
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or '..' in pure.parts or not member.isfile():
                raise ValueError(f'Worker {label} 归档包含不安全成员')
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f'Worker {label} 归档成员不可读')
            content = stream.read()
            if member.name == expected_manifest_name:
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError('Worker package Manifest 过大')
                package_bytes = content
            else:
                payload[member.name] = (content, member.mode & 0o777)
    if package_bytes is None or _sha256_bytes(package_bytes) != package_sha256:
        raise ValueError('Worker package Manifest SHA-256 不匹配')
    package = _json_object(package_bytes, 'package-manifest.json')
    if (package.get('source', {}).get('commit') != source_commit
            or package.get('target', {}).get('platform') != platform
            or package.get('target', {}).get('install_entry') != install_entry):
        raise ValueError('Worker package 来源或安装入口不匹配')
    files = package.get('files')
    if not isinstance(files, list):
        raise ValueError('Worker package 文件清单非法')
    expected = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('Worker package 文件项非法')
        relative = str(item.get('path') or '')
        pure = PurePosixPath(relative)
        archive_name = f'claw-worker/{relative}'
        if (not relative or pure.is_absolute() or '..' in pure.parts
                or archive_name in expected
                or not isinstance(item.get('size'), int)
                or not _SHA256_RE.fullmatch(str(item.get('sha256') or ''))
                or str(item.get('mode') or '') not in ('0644', '0755')):
            raise ValueError('Worker package 文件项非法')
        expected[archive_name] = item
    if set(expected) != set(payload):
        raise ValueError('Worker package payload 与 Manifest 不一致')
    for name, item in expected.items():
        content, mode = payload[name]
        if (len(content) != item['size']
                or _sha256_bytes(content) != item['sha256']
                or format(mode, '04o') != item['mode']):
            raise ValueError(f'Worker package 文件校验失败：{name}')


def _verify_windows_archive(path: Path, package_sha256: str,
                            source_commit: str) -> None:
    expected_manifest_name = 'claw-worker/release/package-manifest.json'
    with zipfile.ZipFile(path, 'r') as archive:
        members = archive.infolist()
        if (len(members) > MAX_PACKAGE_FILES + 1
                or any(item.file_size < 0 or item.file_size > MAX_PACKAGE_FILE_BYTES
                       for item in members)
                or sum(item.file_size for item in members) > MAX_PACKAGE_TOTAL_BYTES):
            raise ValueError('Worker Windows 归档超过安全大小限制')
        names = [item.filename for item in members]
        if (len(names) != len(set(names))
                or expected_manifest_name not in names
                or any(item.is_dir() for item in members)):
            raise ValueError('Worker Windows 归档成员非法')
        payload = {}
        package_bytes = None
        for item in members:
            pure = PurePosixPath(item.filename)
            unix_mode = (item.external_attr >> 16) & 0o170000
            if (pure.is_absolute() or '..' in pure.parts
                    or (unix_mode and unix_mode != 0o100000)):
                raise ValueError('Worker Windows 归档包含不安全成员')
            content = archive.read(item)
            if item.filename == expected_manifest_name:
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError('Worker package Manifest 过大')
                package_bytes = content
            else:
                payload[item.filename] = content
    if package_bytes is None or _sha256_bytes(package_bytes) != package_sha256:
        raise ValueError('Worker package Manifest SHA-256 不匹配')
    package = _json_object(package_bytes, 'package-manifest.json')
    if (package.get('source', {}).get('commit') != source_commit
            or package.get('target', {}).get('platform') != 'windows-x86_64'
            or package.get('target', {}).get('install_entry') != 'Setup.ps1'):
        raise ValueError('Worker package 来源或安装入口不匹配')
    files = package.get('files')
    if not isinstance(files, list):
        raise ValueError('Worker package 文件清单非法')
    expected = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('Worker package 文件项非法')
        relative = str(item.get('path') or '')
        pure = PurePosixPath(relative)
        archive_name = f'claw-worker/{relative}'
        if (not relative or pure.is_absolute() or '..' in pure.parts
                or archive_name in expected
                or not isinstance(item.get('size'), int)
                or not _SHA256_RE.fullmatch(str(item.get('sha256') or ''))):
            raise ValueError('Worker package 文件项非法')
        expected[archive_name] = item
    if set(expected) != set(payload):
        raise ValueError('Worker package payload 与 Manifest 不一致')
    for name, item in expected.items():
        content = payload[name]
        if (len(content) != item['size']
                or _sha256_bytes(content) != item['sha256']):
            raise ValueError(f'Worker package 文件校验失败：{name}')


def verify_catalog_release(release_root: str | Path,
                           release_id: str | None = None,
                           platform: str = PLATFORM) -> VerifiedWorkerRelease:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            'Worker release platform 仅支持 linux-x86_64 / windows-x86_64 / '
            'macos-arm64 / macos-x86_64')
    root = Path(release_root).resolve(strict=True)
    index_path = _safe_child(root, 'index.json', 'Worker release index')
    index_digest = _digest_file_value(
        _safe_child(root, 'index.json.sha256', 'Worker release index digest'),
        'index.json')
    if _sha256_file(index_path) != index_digest:
        raise ValueError('Worker release index SHA-256 不匹配')
    index = _json_object(index_path.read_bytes(), 'index.json')
    if index.get('schema_version') != 1 or not isinstance(index.get('releases'), list):
        raise ValueError('Worker release index schema 不支持')
    selected_id = str(release_id or index.get('latest_candidate') or '')
    rows = [row for row in index['releases']
            if isinstance(row, dict) and row.get('release_id') == selected_id]
    if len(rows) != 1:
        raise ValueError('Worker release_id 不存在或不唯一')
    row = rows[0]
    manifest_sha = str(row.get('manifest_sha256') or '').lower()
    if not _SHA256_RE.fullmatch(manifest_sha):
        raise ValueError('Worker release Manifest SHA-256 非法')
    release_path = _safe_child(root, row.get('manifest'), 'Worker release Manifest')
    if _sha256_file(release_path) != manifest_sha:
        raise ValueError('Worker release Manifest SHA-256 不匹配')
    release = _json_object(release_path.read_bytes(), 'release.json')
    source = release.get('source') if isinstance(release.get('source'), dict) else {}
    commit = str(source.get('commit') or '').lower()
    if (release.get('schema_version') != 1
            or not _COMMIT_RE.fullmatch(commit)
            or release.get('release_id') != f'worker-{commit}'
            or release.get('release_id') != selected_id
            or release.get('channel') != 'candidate'
            or release.get('approval_required') is not True
            or release.get('signature_status') not in ('unsigned', 'verified')):
        raise ValueError('Worker release 身份或审批合同非法')
    artifacts = [item for item in release.get('artifacts', [])
                 if isinstance(item, dict) and item.get('platform') == platform]
    if len(artifacts) != 1:
        raise ValueError(f'Worker {platform} artifact 不存在或不唯一')
    entry = artifacts[0]
    artifact_sha = str(entry.get('artifact_sha256') or '').lower()
    platform_sha = str(entry.get('manifest_sha256') or '').lower()
    size = entry.get('artifact_size')
    if (not _SHA256_RE.fullmatch(artifact_sha)
            or not _SHA256_RE.fullmatch(platform_sha)
            or not isinstance(size, int) or size <= 0
            or size > MAX_ARTIFACT_BYTES
            or entry.get('install_entry') != (
                POSIX_PLATFORM_INSTALL_ENTRIES.get(platform, 'Setup.ps1'))):
        raise ValueError(f'Worker {platform} artifact 元数据非法')
    artifact_path = _safe_child(release_path.parent, entry.get('artifact'),
                                'Worker Linux artifact')
    platform_path = _safe_child(release_path.parent, entry.get('manifest'),
                                'Worker Linux platform Manifest')
    if artifact_path.stat().st_size != size or _sha256_file(artifact_path) != artifact_sha:
        raise ValueError('Worker Linux artifact 大小或 SHA-256 不匹配')
    if _sha256_file(platform_path) != platform_sha:
        raise ValueError('Worker Linux platform Manifest SHA-256 不匹配')
    platform_manifest = _json_object(platform_path.read_bytes(), 'platform manifest.json')
    package_sha = str(platform_manifest.get('package_manifest', {}).get('sha256') or '')
    if (platform_manifest.get('release_id') != selected_id
            or platform_manifest.get('source_commit') != commit
            or platform_manifest.get('platform') != platform
            or platform_manifest.get('artifact', {}).get('file') != artifact_path.name
            or platform_manifest.get('artifact', {}).get('sha256') != artifact_sha
            or platform_manifest.get('artifact', {}).get('size') != size
            or not _SHA256_RE.fullmatch(package_sha)):
        raise ValueError('Worker platform Manifest 绑定非法')
    if platform in POSIX_PLATFORM_INSTALL_ENTRIES:
        _verify_posix_archive(artifact_path, package_sha, commit, platform)
    else:
        _verify_windows_archive(artifact_path, package_sha, commit)
    return VerifiedWorkerRelease(
        release_id=selected_id,
        source_repository=str(source.get('repository') or ''),
        source_ref=str(source.get('ref') or ''),
        source_commit=commit,
        committed_at=str(source.get('committed_at') or ''),
        channel=str(release.get('channel') or ''),
        signature_status=str(release.get('signature_status') or ''),
        platform=platform,
        release_manifest_sha256=manifest_sha,
        platform_manifest_sha256=platform_sha,
        package_manifest_sha256=package_sha,
        artifact_filename=artifact_path.name,
        artifact_sha256=artifact_sha,
        artifact_size=size,
        artifact_path=artifact_path,
    )


def _system_config_value(key: str) -> str:
    try:
        from sqlalchemy import text
        row = db.session.execute(text(
            'SELECT value FROM system_config WHERE config_key = :key'),
            {'key': key}).first()
        return str(row[0] or '').strip() if row else ''
    except Exception:
        return ''


def _repository_root() -> Path:
    local = (os.getenv('WORKER_RELEASE_REPOSITORY_PATH')
             or _system_config_value('worker_release_repository_path'))
    if local:
        root = Path(local).resolve(strict=True)
        if not (root / '.git').exists():
            raise ValueError('Worker release repository path 不是 Git 仓库')
        return root
    url = (os.getenv('WORKER_RELEASE_REPOSITORY_URL')
           or _system_config_value('worker_release_repository_url'))
    ref = (os.getenv('WORKER_RELEASE_SOURCE_REF')
           or _system_config_value('worker_release_source_ref')
           or 'v2-feature')
    if not url:
        raise ValueError('未配置 WORKER_RELEASE_REPOSITORY_PATH/URL')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('https', 'ssh')
            or parsed.username not in (None, 'git')
            or parsed.password is not None
            or not parsed.hostname):
        raise ValueError('Worker release repository URL 必须是不含凭据的 HTTPS/SSH 地址')
    allowed_hosts = {
        item.strip().casefold()
        for item in (os.getenv('WORKER_RELEASE_ALLOWED_GIT_HOSTS')
                     or _system_config_value('worker_release_allowed_git_hosts')
                     or 'git.woa.com').split(',')
        if item.strip()
    }
    if parsed.hostname.casefold() not in allowed_hosts:
        raise ValueError('Worker release repository host 不在允许列表')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref)
            or '..' in ref or ref.endswith('/') or ref.startswith('-')):
        raise ValueError('Worker release source ref 非法')
    cache = Path(os.getenv('WORKER_RELEASE_CHECKOUT_ROOT')
                 or _system_config_value('worker_release_checkout_root')
                 or (Path(current_app.instance_path) / 'worker-release-source'))
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / '.git').exists():
        subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout',
                        '--branch', ref, '--single-branch', url, str(cache)],
                       check=True, capture_output=True, text=True, timeout=180)
    else:
        actual = subprocess.run(['git', '-C', str(cache), 'remote', 'get-url', 'origin'],
                                check=True, capture_output=True, text=True,
                                timeout=30).stdout.strip()
        if actual != url:
            raise ValueError('Worker release checkout origin 与配置不一致')
    subprocess.run(['git', '-C', str(cache), 'fetch', '--prune', 'origin', ref],
                   check=True, capture_output=True, text=True, timeout=180)
    subprocess.run(['git', '-C', str(cache), 'checkout', '--detach', 'FETCH_HEAD'],
                   check=True, capture_output=True, text=True, timeout=60)
    return cache.resolve(strict=True)


def _release_store_root() -> Path:
    root = Path(os.getenv('WORKER_RELEASE_STORE_ROOT')
                or _system_config_value('worker_release_store_root')
                or (Path(current_app.instance_path) / 'worker-releases'))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def import_latest_candidate(actor: str, platform: str = PLATFORM) -> tuple[WorkerRelease, bool]:
    repository = _repository_root()
    candidate = verify_catalog_release(
        repository / 'dist' / 'worker-releases', platform=platform)
    destination = _release_store_root() / candidate.release_id / platform
    destination.mkdir(parents=True, exist_ok=True)
    final_artifact = destination / candidate.artifact_filename
    if final_artifact.exists():
        if (_sha256_file(final_artifact) != candidate.artifact_sha256
                or final_artifact.stat().st_size != candidate.artifact_size):
            raise ValueError('Hub Worker release store 已存在同名但内容不同的制品')
    else:
        fd, temporary = tempfile.mkstemp(prefix='.worker-artifact.', dir=destination)
        os.close(fd)
        try:
            shutil.copyfile(candidate.artifact_path, temporary)
            if (_sha256_file(Path(temporary)) != candidate.artifact_sha256
                    or Path(temporary).stat().st_size != candidate.artifact_size):
                raise ValueError('Worker artifact 写入 Hub store 后校验失败')
            os.replace(temporary, final_artifact)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    record = WorkerRelease.query.filter_by(
        release_id=candidate.release_id, platform=platform).first()
    created = record is None
    if record is None:
        record = WorkerRelease(release_id=candidate.release_id, platform=platform)
        db.session.add(record)
    immutable = {
        'source_repository': candidate.source_repository,
        'source_ref': candidate.source_ref,
        'source_commit': candidate.source_commit,
        'committed_at': candidate.committed_at,
        'channel': candidate.channel,
        'signature_status': candidate.signature_status,
        'release_manifest_sha256': candidate.release_manifest_sha256,
        'platform_manifest_sha256': candidate.platform_manifest_sha256,
        'package_manifest_sha256': candidate.package_manifest_sha256,
        'artifact_filename': candidate.artifact_filename,
        'artifact_sha256': candidate.artifact_sha256,
        'artifact_size': candidate.artifact_size,
    }
    if not created:
        for key, value in immutable.items():
            if getattr(record, key) != value:
                raise ValueError(f'已导入 Worker release 的不可变字段发生漂移：{key}')
    for key, value in immutable.items():
        setattr(record, key, value)
    record.artifact_path = str(final_artifact)
    if created:
        record.approval_status = 'candidate'
        record.imported_by = actor
    db.session.commit()
    return record, created


def verify_record_artifact(record: WorkerRelease) -> Path:
    if (not _COMMIT_RE.fullmatch(record.source_commit or '')
            or record.release_id != f'worker-{record.source_commit}'
            or not _SHA256_RE.fullmatch(record.release_manifest_sha256 or '')
            or not _SHA256_RE.fullmatch(record.platform_manifest_sha256 or '')
            or not _SHA256_RE.fullmatch(record.package_manifest_sha256 or '')
            or not _SHA256_RE.fullmatch(record.artifact_sha256 or '')
            or Path(record.artifact_filename or '').name != record.artifact_filename):
        raise ValueError('已批准的 Claw Worker 版本元数据非法')
    path = Path(record.artifact_path).resolve(strict=True)
    store = _release_store_root()
    if store != path and store not in path.parents:
        raise ValueError('已批准的 Claw Worker 制品不在 Hub release store 内')
    if (not path.is_file() or path.is_symlink()
            or path.stat().st_size != record.artifact_size
            or _sha256_file(path) != record.artifact_sha256):
        raise ValueError('已批准的 Claw Worker 制品校验失败')
    return path


def approved_release(record_id: int | None = None) -> WorkerRelease:
    query = WorkerRelease.query.filter_by(
        platform=PLATFORM, approval_status='approved')
    if record_id:
        record = query.filter_by(id=int(record_id)).first()
    else:
        record = query.filter_by(is_default=True).first()
    if not record:
        raise ValueError('没有已批准的 Linux Claw Worker 发布版本')
    verify_record_artifact(record)
    return record
