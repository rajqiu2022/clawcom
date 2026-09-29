"""Approve the pinned Worker 3ae536f Windows artifact in Hub."""

import json

from worker_release_approval import publish_release


CATALOG_COMMIT = '37817a86ba256a285c8f96d25d57cd85b62b331f'
SOURCE_COMMIT = '3ae536f8dc53073f0a1b3a080190deac1a0b2b4d'
MANIFEST_SHA = '3ffd6f2e3c9907d66446f79299526e0a176ed5ebf47f0c8dc112b95ab4432451'
ARTIFACTS = {
    'windows-x86_64': '792258c2a58f9d01d067e5e8d80d5261d2cde366a7a5cf5c055abd2152758999',
}


if __name__ == '__main__':
    print(json.dumps(publish_release(
        catalog_commit=CATALOG_COMMIT,
        source_commit=SOURCE_COMMIT,
        manifest_sha=MANIFEST_SHA,
        artifacts=ARTIFACTS,
    ), ensure_ascii=False, sort_keys=True))
