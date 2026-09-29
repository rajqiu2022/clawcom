"""Approve the pinned Worker 3536094 multi-platform release in Hub."""

import json

from worker_release_approval import publish_release


CATALOG_COMMIT = '61cbae8a7d94a53270168983c4f490118be707ee'
SOURCE_COMMIT = '3536094d04879d5b2c93a799e6e3d086a7627cc1'
MANIFEST_SHA = '2a67277c53938a4920f2e32eee1053daaa81704260af5ff6d892a94fdbe538c5'
ARTIFACTS = {
    'windows-x86_64': '01ec49fcb460311a829940cebe2ce90784bb9524dd6c226d6ef327894a4519cf',
    'linux-x86_64': '3bbbfc663150cd23c528f6e21af1f402c9bed7981854b8ef7bc61e34ae598abd',
    'macos-arm64': '63d308107aa17f31b073c43401d26cb56ff1ee8b570844dbaffeeff216281b7b',
    'macos-x86_64': '88770a0f3c7015c7f4f0ac6b52724552c40a3007162298a0828fd4f820ff2898',
}


if __name__ == '__main__':
    print(json.dumps(publish_release(
        catalog_commit=CATALOG_COMMIT,
        source_commit=SOURCE_COMMIT,
        manifest_sha=MANIFEST_SHA,
        artifacts=ARTIFACTS,
    ), ensure_ascii=False, sort_keys=True))
