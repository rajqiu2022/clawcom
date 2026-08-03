DEFAULT_TIMIAI_PROJECT = 'gbt'

TIMIAI_PROJECTS = {
    'gbt': {
        'label': 'GBT',
        'config_keys': (
            'deploy_timiai_api_key_gbt',
            'hermes_timiai_api_key_gbt',
            'timiai_api_key_gbt',
        ),
        'env_names': (
            'DEPLOY_TIMIAI_API_KEY_GBT',
            'HERMES_TIMIAI_API_KEY_GBT',
            'TIMIAI_API_KEY_GBT',
        ),
    },
    'qqspeed_pc': {
        'label': 'QQ飞车端游',
        'config_keys': (
            'deploy_timiai_api_key_qqspeed_pc',
            'hermes_timiai_api_key_qqspeed_pc',
            'timiai_api_key_qqspeed_pc',
        ),
        'env_names': (
            'DEPLOY_TIMIAI_API_KEY_QQSPEED_PC',
            'HERMES_TIMIAI_API_KEY_QQSPEED_PC',
            'TIMIAI_API_KEY_QQSPEED_PC',
        ),
    },
    'contra': {
        'label': '魂斗罗',
        'config_keys': (
            'deploy_timiai_api_key_contra',
            'hermes_timiai_api_key_contra',
            'timiai_api_key_contra',
        ),
        'env_names': (
            'DEPLOY_TIMIAI_API_KEY_CONTRA',
            'HERMES_TIMIAI_API_KEY_CONTRA',
            'TIMIAI_API_KEY_CONTRA',
        ),
    },
}

_ALIASES = {
    '': DEFAULT_TIMIAI_PROJECT,
    'default': DEFAULT_TIMIAI_PROJECT,
    'gbt': 'gbt',
    'qq飞车端游': 'qqspeed_pc',
    'qqspeed_pc': 'qqspeed_pc',
    'qqspeed': 'qqspeed_pc',
    'racinggo': 'qqspeed_pc',
    '魂斗罗': 'contra',
    'contra': 'contra',
}


def normalize_timiai_project(value):
    key = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    normalized = _ALIASES.get(key)
    if normalized:
        return normalized
    raise ValueError('不支持的 TimiAI 项目：%s' % value)


def timiai_project_label(value):
    project = normalize_timiai_project(value)
    return TIMIAI_PROJECTS[project]['label']
