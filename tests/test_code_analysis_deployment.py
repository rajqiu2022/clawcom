"""Live registration hotfixes must survive an additive specialty release."""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
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
