"""Refresh and approve the immutable Worker 857878a release in Hub."""

import json

from worker_release_approval import publish_release


CATALOG_COMMIT = 'd1ffbc7a4e2623efebf7c940379ece24323e02a1'
SOURCE_COMMIT = '857878aa4bfcfa108e556ae2012184528711e2a9'
MANIFEST_SHA = '95af9ec929e40ff35bc1dd1dc12e9f3db7f9220383682fbf7f96c22f56dfef10'
ARTIFACTS = {
    'windows-x86_64': '203dccaa96d184c22e7f5245664f635b9e1b6fcbdda4fac96d48045b15923637',
    'linux-x86_64': '2ce2d21dd2c6f9305ab3bec481e33c3bd1bd4e3dcc7a1b8f54178a610b2d2ede',
    'macos-arm64': '619c5fc502dd3d1fb448d14a30f6afc29e123b7ca08c7a4f009288820605657a',
    'macos-x86_64': '394add2da376562f4b4df1d8f2571970abcc9abe787a30a08a3cc0374540bcb0',
}


if __name__ == '__main__':
    print(json.dumps(publish_release(
        catalog_commit=CATALOG_COMMIT,
        source_commit=SOURCE_COMMIT,
        manifest_sha=MANIFEST_SHA,
        artifacts=ARTIFACTS,
    ), ensure_ascii=False, sort_keys=True))
