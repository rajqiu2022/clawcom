"""Hermes LLM provider / model choices shared by API and deploy renderer."""

DEFAULT_HERMES_LLM_PROVIDER = "venus"
DEFAULT_HERMES_LLM_MODEL = "venus"
DEFAULT_TIMIAI_LLM_MODEL = "deepseek-v4-pro-r1"

HERMES_LLM_PROVIDERS = {
    "venus": {
        "label": "Venus",
        "api_mode": "chat_completions",
        "base_url": "http://v2.open.venus.oa.com/llmproxy",
        "api_key_env": "VENUS_API_KEY",
    },
    "timiai": {
        "label": "TimiAI",
        "api_mode": "chat_completions",
        "base_url": "http://api.timiai.woa.com/ai_api_manage/llmproxy",
        "api_key_env": "TIMIAI_API_KEY",
    },
}

VENUS_LLM_MODELS = {
    "venus": {
        "label": "Venus（默认 GLM 5.1）",
        "config_model": "glm-5.1",
        "context_length": 128000,
    },
    "kimi-k2.6": {
        "label": "Kimi 2.6",
        "config_model": "kimi-k2.6",
        "context_length": 200000,
    },
    "glm-5.1": {
        "label": "GLM 5.1",
        "config_model": "glm-5.1",
        "context_length": 128000,
    },
    "deepseek-v4-flash": {
        "label": "DeepSeek V4 Flash",
        "config_model": "deepseek-v4-flash",
        "context_length": 128000,
    },
    "deepseek-v4-pro": {
        "label": "DeepSeek V4 Pro",
        "config_model": "deepseek-v4-pro",
        "context_length": 128000,
    },
    "hunyuan-v3": {
        "label": "Hunyuan V3",
        "config_model": "hy3-preview",
        "context_length": 128000,
    },
}

TIMIAI_LLM_MODELS = {
    "deepseek-v4-pro": {
        "label": "DeepSeek V4 Pro",
        "config_model": "deepseek-v4-pro",
        "context_length": 128000,
    },
    "glm-5.2": {
        "label": "GLM 5.2",
        "config_model": "glm-5.2",
        "context_length": 128000,
    },
    "glm-5v-turbo": {
        "label": "GLM 5V Turbo",
        "config_model": "glm-5v-turbo",
        "context_length": 128000,
    },
    "claude-sonnet-4.6": {
        "label": "Claude Sonnet 4.6",
        "config_model": "claude-sonnet-4.6",
        "context_length": 128000,
    },
    "gemini-3.1-pro-preview": {
        "label": "Gemini 3.1 Pro Preview",
        "config_model": "gemini-3.1-pro-preview",
        "context_length": 128000,
    },
    # GBT 项目专用（TimiAI 侧模型名带 -r1 / -stb 后缀，与其它项目不同）
    "deepseek-v4-pro-r1": {
        "label": "DeepSeek V4 Pro (R1)",
        "config_model": "deepseek-v4-pro-r1",
        "context_length": 128000,
    },
    "deepseek-v4-flash-r1": {
        "label": "DeepSeek V4 Flash (R1)",
        "config_model": "deepseek-v4-flash-r1",
        "context_length": 128000,
    },
    "gemini-3.1-pro-preview-stb": {
        "label": "Gemini 3.1 Pro Preview (STB)",
        "config_model": "gemini-3.1-pro-preview-stb",
        "context_length": 128000,
    },
    # QQ飞车端游 / 魂斗罗 专属
    "kimi-k3": {
        "label": "Kimi K3",
        "config_model": "kimi-k3",
        "context_length": 200000,
    },
    "minimax-m3": {
        "label": "MiniMax M3",
        "config_model": "minimax-m3",
        "context_length": 200000,
    },
}

# TimiAI 的模型授权按项目隔离；相似的显示名在不同项目可能对应不同
# upstream model key。服务端与前端必须共用同一语义，不能把 GBT 的 -r1
# 模型写入 QQ飞车端游或魂斗罗实例。
TIMIAI_MODEL_KEYS_BY_PROJECT = {
    "gbt": (
        "deepseek-v4-pro-r1",
        "claude-sonnet-4.6",
        "gemini-3.1-pro-preview-stb",
        "deepseek-v4-flash-r1",
        "kimi-k3",
        "minimax-m3",
    ),
    "qqspeed_pc": (
        "deepseek-v4-pro",
        "glm-5.2",
        "glm-5v-turbo",
        "claude-sonnet-4.6",
        "gemini-3.1-pro-preview",
        "kimi-k3",
        "minimax-m3",
    ),
    "contra": (
        "deepseek-v4-pro",
        "glm-5.2",
        "glm-5v-turbo",
        "claude-sonnet-4.6",
        "gemini-3.1-pro-preview",
        "minimax-m3",
    ),
}

# 兼容旧 import
HERMES_LLM_MODELS = VENUS_LLM_MODELS

HERMES_VISION_DEFAULTS = {
    "venus": {"provider": "venus", "model": "glm-5.1"},
    "timiai": {"provider": "timiai", "model": "deepseek-v4-pro-r1"},
}

_VENUS_ALIASES = {
    "": DEFAULT_HERMES_LLM_MODEL,
    "venus": "venus",
    "kimi2.6": "kimi-k2.6",
    "kimi-2.6": "kimi-k2.6",
    "kimi-k2.6": "kimi-k2.6",
    "glm5.1": "glm-5.1",
    "glm-5.1": "glm-5.1",
    "deepseekv4": "deepseek-v4-pro",
    "deepseek-v4": "deepseek-v4-pro",
    "deepseek v4": "deepseek-v4-pro",
    "deepseekv4flash": "deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek v4 flash": "deepseek-v4-flash",
    "deepseekv4pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek v4 pro": "deepseek-v4-pro",
    "hunyuanv3": "hunyuan-v3",
    "hunyuan-v3": "hunyuan-v3",
    "hunyuan v3": "hunyuan-v3",
}

_TIMIAI_ALIASES = {
    "": DEFAULT_TIMIAI_LLM_MODEL,
    "claude-sonnet-4.6": "claude-sonnet-4.6",
    "claude sonnet 4.6": "claude-sonnet-4.6",
    "glm5.2": "glm-5.2",
    "glm-5.2": "glm-5.2",
    "glm 5.2": "glm-5.2",
    "glm-5v-turbo": "glm-5v-turbo",
    "glm5v-turbo": "glm-5v-turbo",
    "gemini-3.1-pro-preview": "gemini-3.1-pro-preview",
    "gemini 3.1 pro preview": "gemini-3.1-pro-preview",
    # GBT 专用别名
    "deepseek-v4-pro-r1": "deepseek-v4-pro-r1",
    "deepseekv4pro-r1": "deepseek-v4-pro-r1",
    "deepseek-v4-flash-r1": "deepseek-v4-flash-r1",
    "deepseekv4flash-r1": "deepseek-v4-flash-r1",
    "gemini-3.1-pro-preview-stb": "gemini-3.1-pro-preview-stb",
    "kimik3": "kimi-k3",
    "kimi3": "kimi-k3",
    "kimi-k3": "kimi-k3",
    "kimi k3": "kimi-k3",
    "minimaxm3": "minimax-m3",
    "minimax-m3": "minimax-m3",
    "minimax m3": "minimax-m3",
    "minimax": "minimax-m3",
}


def normalize_hermes_provider(value: str) -> str:
    key = (value or DEFAULT_HERMES_LLM_PROVIDER).strip().lower()
    if key in HERMES_LLM_PROVIDERS:
        return key
    raise ValueError(f"不支持的大模型平台：{value}")


def hermes_models_for_provider(provider: str) -> dict:
    provider = normalize_hermes_provider(provider)
    return TIMIAI_LLM_MODELS if provider == "timiai" else VENUS_LLM_MODELS


def default_hermes_model(provider: str) -> str:
    provider = normalize_hermes_provider(provider)
    return DEFAULT_TIMIAI_LLM_MODEL if provider == "timiai" else DEFAULT_HERMES_LLM_MODEL


def timiai_model_keys_for_project(project: str):
    key = str(project or "").strip().lower().replace("-", "_")
    if key not in TIMIAI_MODEL_KEYS_BY_PROJECT:
        raise ValueError(f"不支持的 TimiAI 项目：{project}")
    return TIMIAI_MODEL_KEYS_BY_PROJECT[key]


def default_timiai_model_for_project(project: str) -> str:
    return timiai_model_keys_for_project(project)[0]


def normalize_timiai_model_for_project(value: str, project: str) -> str:
    model = normalize_hermes_model(value, "timiai")
    if model not in timiai_model_keys_for_project(project):
        raise ValueError(
            f"TimiAI 项目 {project} 不支持模型：{value}")
    return model


def normalize_hermes_model(value: str, provider: str = None) -> str:
    """Normalize UI/user input to one stored Hermes model key."""
    raw = (value or "").strip().lower().replace("_", "-")
    if provider:
        provider = normalize_hermes_provider(provider)
        models = hermes_models_for_provider(provider)
        aliases = _TIMIAI_ALIASES if provider == "timiai" else _VENUS_ALIASES
        if raw in models:
            return raw
        normalized = aliases.get(raw)
        if normalized:
            return normalized
        raise ValueError(f"不支持的大模型：{value}")

    if raw in VENUS_LLM_MODELS:
        return raw
    if raw in TIMIAI_LLM_MODELS:
        return raw
    normalized = _VENUS_ALIASES.get(raw) or _TIMIAI_ALIASES.get(raw)
    if normalized:
        return normalized
    raise ValueError(f"不支持的大模型：{value}")


def hermes_provider_api_mode(provider: str) -> str:
    return HERMES_LLM_PROVIDERS[normalize_hermes_provider(provider)]["api_mode"]


def hermes_provider_label(provider: str) -> str:
    return HERMES_LLM_PROVIDERS[normalize_hermes_provider(provider)]["label"]


def hermes_model_label(value: str, provider: str = None) -> str:
    provider = normalize_hermes_provider(provider or _infer_provider(value))
    key = normalize_hermes_model(value, provider)
    return hermes_models_for_provider(provider)[key]["label"]


def hermes_display_label(value: str, provider: str = None) -> str:
    """Card/UI label with platform prefix."""
    provider = normalize_hermes_provider(provider or _infer_provider(value))
    return f"{hermes_provider_label(provider)} · {hermes_model_label(value, provider)}"


def _infer_provider(model_value: str) -> str:
    raw = (model_value or "").strip().lower().replace("_", "-")
    if raw in TIMIAI_LLM_MODELS or raw in _TIMIAI_ALIASES:
        return "timiai"
    return DEFAULT_HERMES_LLM_PROVIDER


def hermes_config_model(value: str, provider: str = None) -> str:
    provider = normalize_hermes_provider(provider or _infer_provider(value))
    key = normalize_hermes_model(value, provider)
    return hermes_models_for_provider(provider)[key]["config_model"]


def hermes_context_length(value: str, provider: str = None) -> int:
    provider = normalize_hermes_provider(provider or _infer_provider(value))
    key = normalize_hermes_model(value, provider)
    return int(hermes_models_for_provider(provider)[key]["context_length"])


def hermes_vision_config(provider: str) -> dict:
    provider = normalize_hermes_provider(provider)
    return dict(HERMES_VISION_DEFAULTS[provider])
