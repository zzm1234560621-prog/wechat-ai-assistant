"""可选的大模型服务商预设。setup_llm.py（.bat 向导）和 bot.py（微信命令）共用这一份，
避免两边各写一份导致不一致。
"""

PROVIDER_PRESETS = [
    {
        "short": "DeepSeek",
        "name": "DeepSeek（深度求索）",
        "provider": "openai",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_url": "https://platform.deepseek.com",
        "note": "便宜、中文好。key 形如 sk- 加 32 位字符",
    },
    {
        "short": "Claude 官方",
        "name": "Claude（Anthropic 官方）",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-5",
        "models": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"],
        "key_url": "https://console.anthropic.com",
        "note": "效果最好。国内直连可能不通，需要网络条件",
    },
    {
        "short": "通义千问",
        "name": "通义千问（阿里云百炼）",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "key_url": "https://bailian.console.aliyun.com",
        "note": "国内直连稳定",
    },
    {
        "short": "Kimi",
        "name": "Kimi（月之暗面）",
        "provider": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        "key_url": "https://platform.moonshot.cn",
        "note": "长文本强",
    },
    {
        "short": "智谱 GLM",
        "name": "智谱 GLM",
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-plus"],
        "key_url": "https://open.bigmodel.cn",
        "note": "glm-4-flash 有免费额度",
    },
    {
        "short": "OpenAI",
        "name": "OpenAI 官方",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "key_url": "https://platform.openai.com",
        "note": "国内直连通常不通",
    },
    {
        "short": "本地 Ollama",
        "name": "本地 Ollama（不花钱）",
        "provider": "openai",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5",
        "models": ["qwen2.5", "llama3.1", "gemma2"],
        "key_url": "无需 key（随便填 ollama）",
        "note": "需先装 Ollama 并 ollama pull 一个模型",
    },
]


def by_index(n):
    """按 1 开始的编号取预设，越界返回 None。"""
    try:
        i = int(n) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= i < len(PROVIDER_PRESETS):
        return PROVIDER_PRESETS[i]
    return None


def menu_lines(prefix="  "):
    """给 .bat / 微信共用的编号菜单文本。"""
    out = []
    for i, p in enumerate(PROVIDER_PRESETS, 1):
        out.append(f"{prefix}[{i}] {p['short']}  —— {p['note']}")
    return out
