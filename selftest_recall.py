"""撤回原文回显（recall.py + bot 主循环接线）自测。

**不联网、不需要微信、不碰 hook。**

钉住的规矩（每一条都是真机上会咬人的）：

  * **判据必须是结构，不是文本**：一句正常聊天「他刚撤回了什么」**绝不能**
    被当成撤回系统提示 —— 否则我们会拿一条不相干的原文去回显，那是编；
  * **捞不到原文就如实说捞不到**，绝不拿别的消息顶上；
  * **不跨会话找原文**（把别人的消息当成他的原文 = 说错人）；
  * 缓冲**有界**：时间与条数都要夹取，而且写歪的值**退回默认并告警**，
    不许静默变成「无界」（那就是内存泄漏）；
  * 改配置时**不重建 Ring**，否则「改完配置之后那几条撤回」捞不到原文；
  * **接线必须真的在**：bot.py 里得真的 import + 调用，而且撤回那段要排在
    「盯着」通知**之前**（否则同一条提示先冒一条「👀 xx：[系统消息]」的噪音）；
  * **两处配置**（CLAUDE.md 的约定）：`config.example.yaml` 和本机 `config.yaml`
    都得有 `recall` 段，否则「开发机上好用、发布包里静默失效」。

用法：`.venv/Scripts/python.exe selftest_recall.py`
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import recall  # noqa: E402

_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}"
          f"{('  ' + str(extra)) if extra and not cond else ''}")


def t_judge():
    print("\n[1] 判据：结构 + 文本，缺一不可")
    check("10000 + 「撤回」-> 是",
          recall.is_recall(10000, "张三撤回了一条消息"))
    check("带引号的提示也认",
          recall.is_recall(10000, '"张三" 撤回了一条消息'))
    check("字符串类型的 local_type 也认",
          recall.is_recall("10000", "张三撤回了一条消息"))
    # ★ 最关键的一条反例：正常聊天里出现「撤回」两个字，绝不能当系统提示
    check("普通文本里提到「撤回」-> 不是",
          not recall.is_recall(1, "他刚撤回了什么？"))
    check("普通文本且含系统消息字样 -> 不是",
          not recall.is_recall(1, "[系统消息] 撤回"))
    check("10000 但没有「撤回」-> 不是（群公告/入群/踢人）",
          not recall.is_recall(10000, "群公告：明天九点开会"))
    check("类型缺失/空 -> 不是",
          not recall.is_recall(None, "撤回了一条消息")
          and not recall.is_recall(0, "撤回了一条消息")
          and not recall.is_recall("", "撤回了一条消息"))
    check("高位带子类型的 10000 也认",
          recall.is_recall((10000 << 32) | 10000, "张三撤回了一条消息"))
    # 已知限制，明写出来：只渲染成占位符的系统消息认不出来
    check("（已知限制）只渲染成 [系统消息] 的认不出来",
          not recall.is_recall(10000, "[系统消息]"))


def t_ring():
    print("\n[2] 缓冲：找得到 / 不越界 / 不串会话 / 有界")
    r = recall.Ring(seconds=600, maxlen=10)
    r.add("wxid_a", "第一条", 1000)
    r.add("wxid_a", "第二条", 1010)
    r.add("wxid_b", "别的会话的消息", 1020)

    hit = r.find("wxid_a", 1030)
    check("取同会话里更早的最近一条", hit and hit[1] == "第二条", hit)

    hit = r.find("wxid_a", 1005)
    check("比提示更晚的消息不算原文", hit and hit[1] == "第一条", hit)

    hit = r.find("wxid_c", 1030)
    check("不跨会话找（找不到就是 None）", hit is None, hit)

    r.add("wxid_a", "", 1030)
    r.add("wxid_a", "   ", 1030)
    check("空消息不入队", len(r) == 3, len(r))

    # 时间窗口
    r2 = recall.Ring(seconds=60, maxlen=50)
    r2.add("w", "很久以前", 1000)
    r2.add("w", "刚刚", 1100)
    hit = r2.find("w", 1105)
    check("过期条目被清掉（不会把很久以前的话当原文）",
          hit and hit[1] == "刚刚", hit)

    # 条数上限
    r3 = recall.Ring(seconds=9999, maxlen=3)
    for i in range(6):
        r3.add("w", f"m{i}", 2000 + i)
    texts = [it[1] for it in list(r3._q)]
    check("超过条数上限淘汰最老的", texts == ["m3", "m4", "m5"], texts)

    # configure **不能**清空
    r4 = recall.Ring(seconds=600, maxlen=10)
    r4.add("w", "改配置前攒的", 3000)
    r4.configure(seconds=120, maxlen=5)
    check("configure 保留已攒消息（不清空）",
          r4.find("w", 3001) is not None, len(r4))
    check("configure 改小上限后仍是新上限",
          r4.maxlen == 5 and r4.seconds == 120, (r4.maxlen, r4.seconds))


def t_text():
    print("\n[3] 文案：捞到就给原文，捞不到就如实说")
    got = recall.format_echo("张三", "晚上八点老地方见")
    check("有原文 -> 原文在里面", "晚上八点老地方见" in got, got)
    check("有原文 -> 带「撤回」字样", "撤回" in got, got)

    for empty in ("", None, "   "):
        got = recall.format_echo("张三", empty)
        check(f"没原文（{empty!r}）-> 如实说没留住",
              "没留住" in got, got)

    got = recall.format_echo("张三", "长" * 500, limit=10)
    check("超长截断并带省略号", got.endswith("…") and "长" * 11 not in got, got)

    got = recall.format_echo("", "x")
    check("没有显示名时用「某人」，不摆空字符串", "某人" in got, got)

    check("summary_line 带开关状态",
          "开" in recall.summary_line({"recall": {"enabled": True}})
          and "关" in recall.summary_line({"recall": {"enabled": False}}))


def t_conf():
    print("\n[4] 配置：默认 / 夹取 / 写歪的值")
    check("默认开", recall.enabled({}) is True)
    check("能关", recall.enabled({"recall": {"enabled": False}}) is False)
    check("默认 15 分钟 / 300 条",
          recall.buffer_seconds({}) == 900 and recall.buffer_max({}) == 300)
    check("写 0 被夹到下限（不是变成无界）",
          recall.buffer_seconds({"recall": {"buffer_seconds": 0}}) == recall.SEC_MIN)
    check("写 999999 被夹到上限",
          recall.buffer_max({"recall": {"buffer_max": 999999}}) == recall.MAX_MAX)
    check("写歪的值退回默认",
          recall.buffer_max({"recall": {"buffer_max": "abc"}}) == 300)
    check("字符串数字能用",
          recall.buffer_max({"recall": {"buffer_max": "50"}}) == 50)
    check("recall 段是标量时不炸、按默认走",
          recall.enabled({"recall": "yes"}) is True)


def t_wiring():
    print("\n[5] 接线：bot.py 真的调了，而且顺序对；两份配置都有 recall 段")
    bot = open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
    check("bot.py 有 import recall", "import recall" in bot)
    check("bot.py 建了 Ring", "recall.Ring(" in bot)
    check("bot.py 用了 is_recall", "recall.is_recall(" in bot)
    check("bot.py 往缓冲里记消息", "recall_ring.add(" in bot)
    check("bot.py 回显走 send()", "recall.format_echo(" in bot)
    check("reload_cfg 里同步了开关（nonlocal）",
          "nonlocal recall_on, recall_ring" in bot)
    check("启动横幅带撤回回显", "recall.summary_line(cfg)" in bot)

    i_recall = bot.find("recall.is_recall(")
    i_watch = bot.find("# 盯着：他发消息就通知我")
    check("撤回块排在「盯着」通知之前（否则先冒一条噪音通知）",
          0 <= i_recall < i_watch, (i_recall, i_watch))

    for name in ("config.example.yaml", "config.yaml"):
        p = os.path.join(BASE, name)
        if not os.path.isfile(p):
            check(f"{name} 存在", False)
            continue
        txt = open(p, encoding="utf-8").read()
        check(f"{name} 有顶层 recall 段", "\nrecall:\n" in txt)
        for key in ("enabled:", "buffer_seconds:", "buffer_max:"):
            check(f"{name} 的 recall 段有 {key}",
                  key in txt.split("\nrecall:\n", 1)[-1].split("\n#", 1)[0])


def main():
    print("=" * 66)
    print("撤回原文回显自测（无微信 / 不碰 hook / 不联网）")
    print("=" * 66)
    t_judge()
    t_ring()
    t_text()
    t_conf()
    t_wiring()
    print("=" * 66)
    print("全部通过 ✅" if _ok else "有失败 ❌")
    print("=" * 66)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
