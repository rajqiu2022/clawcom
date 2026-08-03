from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
API_FILE = ROOT / "web" / "app" / "api" / "memories.py"
INIT_FILE = ROOT / "web" / "app" / "api" / "__init__.py"
AGENT_CLIENT = ROOT / "web" / "app" / "api" / "agent_client.py"


class MemoryApiContractTests(unittest.TestCase):
    def test_memory_routes_and_registration_exist(self):
        api = API_FILE.read_text(encoding="utf-8")
        init = INIT_FILE.read_text(encoding="utf-8")

        assert "/openclaws/<int:claw_id>/memories/mutations" in api
        assert "/openclaws/<int:claw_id>/memories/delta" in api
        assert "Idempotency-Key" in api
        assert "process_mutation_request(" in api
        assert "decode_cursor(" in api
        assert "memory_api_enabled" in api
        assert "memories" in init

    def test_sidecar_config_can_advertise_memory_sync_without_breaking_existing_fields(self):
        source = AGENT_CLIENT.read_text(encoding="utf-8")
        config_route = source.split("def claw_sidecar_config", 1)[1].split(
            "@agent_bp.route('/<int:claw_id>/sidecar-deployment-verify'",
            1,
        )[0]

        assert "build_memory_sync_config" in config_route
        assert "shared_memory_capability" in config_route
        assert "payload['memory_sync']" in config_route
        assert "payload.setdefault('capabilities', {})['shared_memory']" in config_route
        assert "payload['memo_index']" in config_route


if __name__ == "__main__":
    unittest.main()
