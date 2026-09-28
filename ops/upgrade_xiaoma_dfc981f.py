"""Run the proven scoped Claw #12 upgrade with the dfc981f release."""

import argparse
import hashlib
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / '_upgrade_xiaoma_worker_785b834.py'
RELEASE = (Path('F:/Code/claw-worker-windows-v2/dist/worker-releases') /
           'worker-dfc981f89d92e7bcbf517b41badd7a6958abf692')
REPLACEMENTS = {
    "REMOTE = '/var/lib/claw-worker/claw-12/upgrade-785b834-20260928.sh'":
        "REMOTE = '/var/lib/claw-worker/claw-12/upgrade-dfc981f-20260928.sh'",
    "SOURCE_COMMIT = '785b8346035e3339e34e254f6b0cc68b407e484e'":
        "SOURCE_COMMIT = 'dfc981f89d92e7bcbf517b41badd7a6958abf692'",
    "ARTIFACT_SHA = '521799176a7127e65d3259e783c98918e689b03491ca731151532fea9031cb09'":
        "ARTIFACT_SHA = '2325eda35ab5f4bd64984d02cd371922920319a219841e2840a022b9cccdf2c5'",
    "MANIFEST_SHA = '0ce21c2e6b860ccc85a11d5e2b1436ff0c1c85fa0c4ddf31cbd6d13992128d31'":
        "MANIFEST_SHA = 'f44904a8e31effc0b628dc09a5833c42798eefa7476a9b81b8169926365b425e'",
    "RELEASE_SHA = 'c253402cefedb3e274d746a4fb721d203afbea7f530a2ad160a139d5ba024cd5'":
        "RELEASE_SHA = 'ba6f6d40c027baf0f8737ffa2e667ea7953501ad963a6e8f8293f47e95fb39c0'",
    "grep -q 'worker-0c897e8a745b1347e06640d6ec889ff3f79b57fb'":
        "grep -q 'worker-785b8346035e3339e34e254f6b0cc68b407e484e'",
}


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    for relative, expected in (
        ('release.json', 'ba6f6d40c027baf0f8737ffa2e667ea7953501ad963a6e8f8293f47e95fb39c0'),
        ('linux-x86_64/manifest.json', 'f44904a8e31effc0b628dc09a5833c42798eefa7476a9b81b8169926365b425e'),
        ('linux-x86_64/claw-worker-linux-x86_64-dfc981f89d92.tar.gz',
         '2325eda35ab5f4bd64984d02cd371922920319a219841e2840a022b9cccdf2c5'),
    ):
        assert sha(RELEASE / relative) == expected, relative
    source = SOURCE.read_text(encoding='utf-8')
    for old, new in REPLACEMENTS.items():
        assert source.count(old) == 1, old
        source = source.replace(old, new, 1)
    compile(source, str(SOURCE), 'exec')
    if not args.apply:
        print('PREFLIGHT_OK release hashes, expected source markers and script syntax')
        return
    exec(compile(source, str(SOURCE), 'exec'), {'__file__': str(SOURCE)})


if __name__ == '__main__':
    main()
