"""界面语言（i18n）自测：中英切换、落盘、三个界面的骨架都跟着走。

为什么要有这一份（2026-10-06 加中英双语时定的）：
  * 语言是**运行期**状态，`/lang en` 之后 `t()` 必须立刻变 —— 模块级常量在 import 时
    就拼死了，切完还是旧语言，那种「看起来生效了、其实没有」正是本项目最怕的形状；
  * 认不出来的值（`/lang 日本語`）**不许静默改回默认**：用户会以为命令没生效；
  * 三个界面（控制台 / 微信 `/bot` 面板 / 只读状态页）都要真的跟着走，
    只改一处等于「换了台电脑就静默失效」的老毛病。

⚠️ 全程**不碰真 settings.json**：把 `settings.SETTINGS_PATH` 指到临时目录。
   语言是要落盘的，隔离掉才不会把用户配置改掉（和 `/clear` 那条教训同源）。
不需要微信、不碰 hook、不联网。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import settings

# 必须在 import i18n / bot / console **之前**改道：它们都从 settings 读路径。
_TMP = tempfile.mkdtemp(prefix="i18n_selftest_")
settings.SETTINGS_PATH = os.path.join(_TMP, "settings.json")

import i18n                                                    # noqa: E402
import bot                                                     # noqa: E402
import console                                                 # noqa: E402
import status_page                                             # noqa: E402

_ok = True


def chk(label, cond, detail=""):
    global _ok
    if cond:
        print(f"  ✅ {label}")
    else:
        _ok = False
        print(f"  ❌ {label}  {detail}")


def sec(title):
    print(f"\n── {title} ──")


def reset(lang=None):
    """把进程内缓存清掉，必要时伪造 settings.json。"""
    i18n._cache = None
    os.environ.pop("WECHAT_ASSISTANT_LANG", None)
    if lang is None:
        if os.path.exists(settings.SETTINGS_PATH):
            os.remove(settings.SETTINGS_PATH)
    else:
        settings.set_value("language", lang)
        i18n._cache = None


def main():
    sec("1 · normalize：写法归一，认不出就是认不出")
    for raw, want in (("zh", "zh"), ("中文", "zh"), ("ZH", "zh"), ("zh-CN", "zh"),
                      ("en", "en"), ("English", "en"), ("EN-US", "en"),
                      ("日本語", ""), ("", ""), (None, ""), ("  en  ", "en")):
        chk(f"normalize({raw!r}) == {want!r}", i18n.normalize(raw) == want, i18n.normalize(raw))

    sec("2 · 默认语言 + 落盘 + 认不出的值不动")
    reset()
    chk("没配过的时候是中文（默认）", i18n.current() == "zh", i18n.current())
    got, bad = i18n.set_lang("en")
    chk("set_lang('en') 返回新语言、没有坏值", got == "en" and bad is None, (got, bad))
    chk("current() 立刻变（不是等重启）", i18n.current() == "en", i18n.current())
    chk("写进了 settings.json", settings.load().get("language") == "en", settings.load())
    got, bad = i18n.set_lang("日本語")
    chk("认不出的值：**原地不动**并如实返回", i18n.current() == "en" and bad == "日本語", (i18n.current(), bad))
    chk("认不出的值**不写盘**", settings.load().get("language") == "en", settings.load())
    i18n.set_lang("zh")
    chk("切回中文也落盘", settings.load().get("language") == "zh", settings.load())

    sec("3 · t() 二选一 + 环境变量覆盖")
    chk("中文时取第一支", i18n.t("中文", "English") == "中文", i18n.t("中文", "English"))
    i18n.set_lang("en")
    chk("英文时取第二支", i18n.t("中文", "English") == "English", i18n.t("中文", "English"))
    # 环境变量只在**没配过**时生效：它是排障/自测用的临时开关，不该盖住用户的选择。
    reset("zh")
    os.environ["WECHAT_ASSISTANT_LANG"] = "en"
    chk("env 覆盖优先于设置", i18n.current() == "en", i18n.current())
    os.environ.pop("WECHAT_ASSISTANT_LANG")
    i18n._cache = None
    chk("去掉 env 后回落到设置里的中文", i18n.current() == "zh", i18n.current())

    sec("4 · 微信 `/bot` 面板：骨架跟着走，值不动")
    reset("zh")
    zh = bot.bot_dashboard({}, None)
    i18n.set_lang("en")
    en = bot.bot_dashboard({}, None)
    chk("中文面板标题", "助手控制台" in zh, zh.splitlines()[0])
    chk("英文面板标题", "Assistant console" in en, en.splitlines()[0])
    chk("英文面板真的换了标签", "Auto-reply" in en and "Auto-reply" not in zh)
    chk("`/bot` 路由这类**值**不翻（中文命令名照旧）", "/bot 模型" in en)
    chk("`_yn` 跟着语言（开/关 ↔ on/off）", ("**on**" in en or "off" in en) and ("**开**" in zh or "关" in zh))
    menu_en = bot.bot_menu()
    chk("`/bot 功能` 那张表是函数、每次现拼（切完立刻变）", "controls these" in menu_en.splitlines()[0], menu_en.splitlines()[0])
    chk("表里有「语言」这一行", "/lang en" in menu_en)

    sec("5 · `/lang` 命令 + `/help` 一致")
    reset("zh")
    out, _ = bot.handle_command("/lang", None, {}, True)
    chk("裸 `/lang` 回用法与当前语言", "当前界面语言" in out and "zh" in out, out)
    out, _ = bot.handle_command("/lang en", None, {}, True)
    chk("`/lang en` 用**新语言**回话", "Interface language is now" in out, out)
    out, _ = bot.handle_command("/lang 日本語", None, {}, True)
    chk("`/lang 日本語` 如实拒绝、当前语言不变", i18n.current() == "en" and "only" in out, out)
    chk("`/help` 里写了 `/lang`（不然用户永远不知道有这命令）", "/lang" in bot.HELP_TEXT)
    reset("zh")

    sec("6 · 本地控制台：两种语言都渲染得出、[L] 在菜单里")
    import builtins
    import io
    from contextlib import redirect_stdout

    def render(lang):
        os.environ["WECHAT_ASSISTANT_LANG"] = lang
        i18n._cache = None
        buf = io.StringIO()
        old_input = builtins.input
        # console.py 用的是**内置** input()（不是 console.input）：立刻选「退出」，不进交互循环
        builtins.input = lambda *a, **k: "0"
        try:
            with redirect_stdout(buf):
                console.menu()
        finally:
            builtins.input = old_input
        os.environ.pop("WECHAT_ASSISTANT_LANG", None)
        i18n._cache = None
        return buf.getvalue()

    zh_menu = render("zh")
    en_menu = render("en")
    chk("中文菜单有标题与 [9]", "微信 AI 助手 · 控制台" in zh_menu and "[9]" in zh_menu)
    chk("英文菜单有标题与 [9]", "WeChat AI Assistant · Console" in en_menu and "[9]" in en_menu)
    chk("英文菜单是英文（不是原样中文）", "Show logs" in en_menu and "看日志" not in en_menu)
    chk("两种语言都能看到 [L] 切换入口", "[L]" in zh_menu and "[L]" in en_menu)
    chk("菜单编号没被改动（[0]~[9] 都在）", all(f"[{i}]" in zh_menu for i in range(10)))

    sec("7 · 只读状态页：骨架跟着走，快照值不翻")
    snap = {"app": "微信 AI 助手", "login_ok": True, "poll_count": 12,
            "hook_errors": None, "note": "连不上 30001（WinError 10061）"}
    html_zh = status_page.render_html(snap, "zh")
    html_en = status_page.render_html(snap, "en")
    chk("中文页 title 是中文", "微信 AI 助手 · 状态" in html_zh)
    chk("英文页 title 是英文", "WeChat AI Assistant · Status" in html_en)
    chk("快照里的值**原样透传**（不翻译数据）", "连不上 30001（WinError 10061）" in html_en
        and "微信 AI 助手" in html_en)
    chk("页面上有 中文/English 两个链接（只读、无 JS）",
        "?lang=zh" in html_en and "?lang=en" in html_en and "<script" not in html_en.lower())
    chk("状态页是只读的：render 不改设置", settings.load().get("language") == "zh", settings.load())

    print("\n" + "=" * 50)
    print("全部通过 ✅" if _ok else "有失败 ❌")
    print("=" * 50)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
