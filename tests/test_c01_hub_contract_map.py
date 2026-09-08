import json
import inspect
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


def _stub(name, **attrs):
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return _Noop()


_stub("flask_cors", CORS=_Noop)
_stub(
    "flask_socketio",
    SocketIO=_Noop,
    emit=_Noop(),
    join_room=_Noop(),
    leave_room=_Noop(),
)

from flask import Flask  # noqa: E402

from app.api import api_bp  # noqa: E402
from app.api import workflows as workflow_api  # noqa: E402
from app.models import (  # noqa: E402
    ResourceLease,
    WorkflowOperationIdempotency,
    WorkflowRun,
    WorkflowRunStep,
)


class C01HubContractMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = (
            ROOT
            / "tests"
            / "fixtures"
            / "deepflow_worker"
            / "c01_hub_contract_map_v1.json"
        )
        cls.mapping = json.loads(path.read_text(encoding="utf-8"))

    def test_mapping_references_real_model_fields(self):
        models = {
            "WorkflowRun": WorkflowRun,
            "WorkflowRunStep": WorkflowRunStep,
            "WorkflowOperationIdempotency": WorkflowOperationIdempotency,
            "ResourceLease": ResourceLease,
        }
        for section in self.mapping["models"].values():
            model = models[section["model"]]
            for field in section["fields"]:
                with self.subTest(model=model.__name__, field=field):
                    self.assertTrue(hasattr(model, field))

    def test_mapping_references_registered_hub_routes(self):
        app = Flask(__name__)
        app.register_blueprint(api_bp, url_prefix="/api/v1")
        routes = {
            rule.rule: rule.methods
            for rule in app.url_map.iter_rules()
        }
        for route in self.mapping["routes"].values():
            with self.subTest(path=route["path"]):
                self.assertIn(route["path"], routes)
                self.assertTrue(set(route["methods"]).issubset(
                    routes[route["path"]]
                ))

    def test_direct_claim_mapping_tracks_opt_in_implementation(self):
        gaps = {item["id"]: item for item in self.mapping["gaps"]}
        claim_source = inspect.getsource(workflow_api.claim_workflow_step)
        result_source = inspect.getsource(
            workflow_api.report_workflow_step_result
        )
        self.assertIn("step.step_type != 'worker_task'", claim_source)
        self.assertIn(
            "workflow_step_claim_required", result_source
        )
        self.assertEqual(
            ["worker_task", "agent_task_with_direct_execution_lease"],
            self.mapping["routes"]["step_claim"]["supported_step_types"],
        )
        self.assertFalse(
            self.mapping["worker_source"]["v4_store_used_by_agent_direct"]
        )
        self.assertEqual(
            "implemented_local",
            gaps["H01_AGENT_DIRECT_ADMISSION"]["status"],
        )
        self.assertEqual(
            "implemented_local",
            gaps["H02_AGENT_DIRECT_FENCING"]["status"],
        )
        self.assertNotEqual(
            "complete", gaps["W02_DIRECT_OPERATION_JOURNAL"]["status"])
        self.assertEqual(
            "implemented_local_unwired",
            gaps["W02_DIRECT_OPERATION_JOURNAL"]["status"],
        )

    def test_deepflow_contract_is_candidate_not_release(self):
        contract = self.mapping["deepflow_contract"]
        self.assertEqual("deepflow.racinggo.operations@1", contract["schema"])
        self.assertRegex(contract["contract_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(contract["production_release"])


if __name__ == "__main__":
    unittest.main()
