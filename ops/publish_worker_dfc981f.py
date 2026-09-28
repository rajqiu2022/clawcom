"""Import and approve the immutable Worker build for this rollout."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _publish_worker_release_785b834 as publish


publish.CATALOG_COMMIT = 'b21c5953d2ed49943badf3da8b438316e446bcc2'
publish.SOURCE_COMMIT = 'dfc981f89d92e7bcbf517b41badd7a6958abf692'
publish.RELEASE_ID = 'worker-' + publish.SOURCE_COMMIT
publish.MANIFEST_SHA = 'ba6f6d40c027baf0f8737ffa2e667ea7953501ad963a6e8f8293f47e95fb39c0'
publish.ARTIFACTS = {
    'windows-x86_64': 'ee6702360618cb8a8375716f32a192997042de33400b6656261ef27416d729b7',
    'linux-x86_64': '2325eda35ab5f4bd64984d02cd371922920319a219841e2840a022b9cccdf2c5',
    'macos-arm64': '1e4c001b81d690b62dae1b05b856bbd0a7c23663b3c9b0ce42c1de22ceeb7df9',
    'macos-x86_64': '358ddbee0a1ea741d6d2a0fa8db2bf7ff4a51954bf51a25b340f408183fa9ad1',
}


if __name__ == '__main__':
    publish.main()
