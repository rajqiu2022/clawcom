"""Hermes/Venus model choices shared by API and deploy renderer."""

DEFAULT_HERMES_LLM_PROVIDER = "venus"
DEFAULT_HERMES_LLM_MODEL = "venus"

HERMES_LLM_MODELS = {
    "venus": {
        "label": "Venus（默认）",
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

_ALIASES = {
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


def normalize_hermes_model(value: str) -> str:
    """Normalize UI/user input to one stored Hermes model key."""
    key = (value or "").strip().lower()
    key = key.replace("_", "-")
    if key in HERMES_LLM_MODELS:
        return key
    normalized = _ALIASES.get(key)
    if normalized:
        return normalized
    raise ValueError(f"不支持的大模型：{value}")


def hermes_model_label(value: str) -> str:
    key = normalize_hermes_model(value)
    return HERMES_LLM_MODELS[key]["label"]


def hermes_config_model(value: str) -> str:
    key = normalize_hermes_model(value)
    return HERMES_LLM_MODELS[key]["config_model"]


def hermes_context_length(value: str) -> int:
    key = normalize_hermes_model(value)
    return int(HERMES_LLM_MODELS[key]["context_length"])
