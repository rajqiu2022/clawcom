"""Live registration hotfixes must survive an additive specialty release."""
import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import deploy_code_analysis as deployment
from deploy_code_analysis import merge_api_registration


def test_split_live_imports_and_auth_cache_are_preserved():
    source = b"""from app.api import skills, knowledge
from app.api import automation_capabilities
from app.api import agent_teams
def require_auth():
    for cache_key in ('_collaboration_session', '_chat_guest_session', '_auth_claw', '_auth_user',
                      '_auth_user_super'):
        g.pop(cache_key, None)
    return None
"""
    result = merge_api_registration(source)
    ast.parse(result)
    for line in source.splitlines()[:3]:
        assert line in result
    assert b"'_resource_share_policies'" in result
    assert b'from app.api import code_analysis' in result
    assert merge_api_registration(result) == result


def test_unknown_auth_layout_is_rejected():
    with pytest.raises(RuntimeError):
        merge_api_registration(b'def require_auth():\n    return None\n')


def test_route_merge_adds_only_specialty_and_is_idempotent(tmp_path, monkeypatch):
    local = b"""@views_bp.route('/mobile')
def mobile_page():
    return render_template('mobile.html')
@views_bp.route('/code-analysis')
def code_analysis_page():
    return render_template('code_analysis.html')
"""
    path = tmp_path / 'web/app/views/__init__.py'
    path.parent.mkdir(parents=True)
    path.write_bytes(local)
    monkeypatch.setattr(deployment.release, 'ROOT', tmp_path)
    monkeypatch.setattr(deployment.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b'# baseline\n'))
    live = b"@views_bp.route('/live-hotfix')\ndef hotfix():\n    return 'kept'\n"
    result = deployment.candidate('app/views/__init__.py', live)
    assert live.rstrip() in result
    assert b'/code-analysis' in result
    assert b'/mobile' not in result
    assert deployment.candidate('app/views/__init__.py', result) == result
