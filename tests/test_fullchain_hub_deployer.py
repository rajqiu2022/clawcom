import importlib.util
import subprocess
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops"
sys.path.insert(0, str(OPS))
try:
    import paramiko  # noqa: F401
except ModuleNotFoundError:
    sys.modules["paramiko"] = types.ModuleType("paramiko")

spec = importlib.util.spec_from_file_location(
    "deploy_fullchain_hub_runtime_fixes",
    OPS / "deploy_fullchain_hub_runtime_fixes.py",
)
deploy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(deploy)


def _git_source(revision: str, path: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"{revision}:web/{path}"], cwd=ROOT
    ).decode("utf-8")


def test_deployer_preserves_remote_additions_and_applies_all_runtime_fixes() -> None:
    from deploy_multiflow_p0_compat import merge_python

    for path in deploy.FILES:
        base = _git_source(deploy.BASE_COMMIT, path)
        remote = base + "\nREMOTE_DEPLOYMENT_SENTINEL = True\n"
        local = (ROOT / "web" / path).read_text(encoding="utf-8")
        merged = merge_python(remote, base, local)
        deploy._verify_markers(path, merged)
        assert "REMOTE_DEPLOYMENT_SENTINEL = True" in merged


def test_deployer_backfills_existing_notebook_recency() -> None:
    assert "app/api/knowledge_notebooks.py" in deploy.FILES
    source = deploy.NOTEBOOK_RECENCY_BACKFILL
    assert "func.max(KnowledgeEntry.updated_at)" in source
    assert "notebook.updated_at = latest_page_at" in source
