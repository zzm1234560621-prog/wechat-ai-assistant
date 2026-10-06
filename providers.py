"""可选的大模型服务商预设。setup_llm.py（.bat 向导）与 bot.py（微信 /provider）共用。"""

PROVIDER_PRESETS = [
    {
        "short": "DeepSeek",
        "name": "DeepSeek（深度求索）",
        "provider": "openai",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "key_url": "https://platform.deepseek.com",
        "note": "便宜、中文好，**收图**；key 形如 sk- 加 32 位字符",
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
        "name": "智谱 GLM（glm-4-flash-250414 免费）",
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash-250414",
        "models": ["glm-4-flash-250414", "glm-4.7-flash"],
        "key_url": "https://open.bigmodel.cn",
        "note": "**两个都免费**（官方定价页标「免费」）。默认 250414：快（0.2~0.5s）、工具调用正常；"
                "glm-4.7-flash 更聪明但**限流严重**（常 429）。⚠️ 都**只收文本、不收图**",
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
    {
        # 只能追加在末尾：中间编号一动，记住「/provider 6 = OpenAI」的人就会配错。
        "short": "OpenRouter",
        "name": "OpenRouter（聚合，含免费档）",
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "models": ["nvidia/nemotron-3-ultra-550b-a55b:free",
                   "qwen/qwen3.8-27b:free",
                   "openrouter/free"],
        "key_url": "https://openrouter.ai/settings/keys",
        "note": "一个 key 通吃多家模型（key 形如 sk-or-v1-）。"
                "⚠️ 免费档（id 以 :free 结尾）：**20 次/分、50 次/天**（累计充值 ≥10 刀才放宽）；"
                "上游多数会**拿提示词去训练**，而本助手发的是真实聊天记录 —— 用前先在账号里关掉训练授权。"
                "模型页路由选「highest tool-calling accuracy」，太图便宜会让功能被静默省掉。",
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
