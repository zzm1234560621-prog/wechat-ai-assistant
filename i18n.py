"""界面语言（中文 / English）。只负责「现在用哪种语言」，文案就地二选一。

用法（三个界面都这么写，新增一句文案**不用登记到任何总表里**）：

    from i18n import t
    print(t("   [4] 停止 / 重启助手", "   [4] Stop / restart"))

存哪：`settings.json` 顶层 `language`（`zh` / `en`），默认 `zh`。
和 settings.json 其它键同一条规矩：**只写 settings.json，绝不回写带注释的 config.yaml**。

三条硬约束（照 `health` / `status_page` 那套来）：

  · 只读 settings.json，**不查微信库、不碰 hook、不起线程**；
  · 读不出来 / 值不认识 → **退回 zh 并说一句**，绝不因为语言配置把任何流程拦住；
  · 切换走 `settings.set_value`（原子写），和别的设置同一个写路径。

默认为什么是中文：这个助手的实际用户是中文用户；英文那一支是给公开仓库的英文读者、
以及 `/lang en` 之后的界面。**报错与日志先保持中文** —— 它们是对着代码排查用的证据，
翻译它们只会让「按日志搜代码」这件事变难（见 README「已知限制」）。
"""
import os

import settings as _settings

LANG_DEFAULT = "zh"
LANG_NAMES = {"zh": "中文", "en": "English"}

# 进程内缓存：t() 会在收消息那条线程上被调，不能每次都去读盘。
# `set_lang` 会刷新它。**外面手工改 settings.json 要重启才生效**（和别的配置一样）。
_cache = None

# 临时覆盖（自测 / 排障用）：设了它就不用动 settings.json。
_ENV = "WECHAT_ASSISTANT_LANG"


def normalize(value):
    """把各种写法归一成 `"zh"` / `"en"`；不认识返回 `""`（**绝不猜**）。"""
    s = str(value or "").strip().lower()
    if s in ("zh", "cn", "zh-cn", "zh_cn", "zh-hans", "chinese", "中文"):
        return "zh"
    if s in ("en", "en-us", "en_us", "english", "英文"):
        return "en"
    return ""


def current():
    """当前语言（`"zh"` / `"en"`）。读不出来一律退回默认。"""
    global _cache
    if _cache:
        return _cache
    got = normalize(os.environ.get(_ENV))
    if not got:
        try:
            got = normalize(_settings.load().get("language"))
        except Exception:                       # noqa: BLE001 —— 语言读不出来不许拦住任何流程
            got = ""
    _cache = got or LANG_DEFAULT
    return _cache


def set_lang(value):
    """切语言并落盘。返回 `(当前语言, 认不出来的原值)`。

    认不出来时**语言原地不动**（第二个返回值不是 None，调用方据此如实告诉用户），
    绝不静默改成默认值 —— 用户写了 `language: 日本語`，界面却悄悄变中文，比报错更坏。
    """
    global _cache
    got = normalize(value)
    if not got:
        return current(), value
    _settings.set_value("language", got)
    _cache = got
    return got, None


def t(zh, en):
    """按当前语言二选一。中文是默认那一支。

    参数顺序刻意是「中文在前」：这个项目里绝大多数用户看的是中文，
    多语言文案读起来也应该是「原文 + 译文」而不是反过来。
    """
    return en if current() == "en" else zh
