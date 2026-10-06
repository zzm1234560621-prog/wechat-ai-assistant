"""盯着某个会话：他发消息就**通知我**，一个字都不回。

和 auto_reply 是互补的两条路：

    auto_reply  代我回他（会发消息给对方）
    watch       只告诉我他在说什么（不出站给对方）

同一会话不能同时加进两边——那会既通知又回复，看着很乱，所以加的时候会拦下来。

**为什么不开后台线程 / 不做成事件回调**：hook 不支持并发，所有出站动作
（包括「通知我」这条也要发微信消息）都必须发生在**收消息那条线程**上。
所以监听是在主循环里顺带做的，不额外起线程——和定时任务同一个道理。

名单存在 settings.json 的 watch 段（命令维护），config.yaml 给默认值。
"""
import re

import settings

MANAGED = ("enabled", "chats", "keywords")

# 关键词监听的两道护栏（**正则回溯爆炸会卡死收消息那条线程**，而那条线程一卡，
# 轮询、定时、看护全停——所以这里宁可拒绝，也不放一个可疑的正则进来）：
#   1. 正则本身的长度上限；
#   2. 只拿消息的**前** N 个字符去匹配（回溯代价随输入长度爆炸，截断是最有效的一道）。
# ⚠️ 「只扫前 N 个字符」是一个**真实语义限制**：关键词出现在很后面就匹配不到。
#    这条要如实告诉用户（见 _USAGE），不许含糊。
KEYWORD_MAX_LEN = 200
KEYWORD_SCAN_CHARS = 4000
# 明显的回溯炸弹形状：量词套在「自带量词的分组」外面，如 `(a+)+` / `(ab*)*`。
# 这是**启发式**，不是证明——判定正则是否灾难性回溯本来就不可判定。
# 所以它只是三道护栏里的一道，不能因为过了它就以为安全。
_BOMB = re.compile(r"\([^()]*[+*]\)[+*]")

_USAGE = (
    "用法：\n"
    "  /watch —— 看名单\n"
    "  /watch add <昵称|wxid|roomid> —— 他发消息就通知我（不回他）\n"
    "  /watch del <昵称|wxid>\n"
    "  /watch on|off —— 总开关\n"
    "  /watch keyword <正则> —— **任何会话**里出现这个模式就通知我（不回）\n"
    "  /watch keyword        —— 看已有关键词\n"
    "  /watch keyword del <正则>\n"
    "（中文子命令也还能用：/watch 加|删 / 开|关 / 关键词）\n"
    "群也能盯：群填 roomid（形如 xxx@chatroom）。\n"
    "⚠️ 关键词只看**文本消息**，而且只扫每条消息的**前 %d 个字符**"
    "（正则回溯会卡住收消息线程，所以必须有上限）。" % KEYWORD_SCAN_CHARS
)


def section(cfg):
    return dict((cfg or {}).get("watch") or {})


def chats(cfg):
    """{wxid: 条目}。条目按引用返回，命令里改完再 _save。"""
    out = {}
    for c in (section(cfg).get("chats") or []):
        if isinstance(c, dict) and str(c.get("wxid") or "").strip():
            out[str(c["wxid"])] = c
    return out


def chat_list(cfg):
    return list(chats(cfg).values())


def enabled(cfg):
    # 默认开：名单本来就是空的，开着也不会怎样；用户加人就是想让它生效
    return bool(section(cfg).get("enabled", True))


def keywords(cfg):
    """已有关键词条目 `[{"pattern": ..., "raw": ...}]`（按值返回，改完再 _save）。"""
    out = []
    for k in (section(cfg).get("keywords") or []):
        if isinstance(k, dict) and str(k.get("pattern") or "").strip():
            out.append({"pattern": str(k["pattern"]),
                        "raw": str(k.get("raw") or k["pattern"])})
    return out


def check_pattern(pattern):
    """校验一条用户给的正则。返回 `(是否可用, 一句人话)`。

    三道护栏（都要过）：长度、明显回溯炸弹、能不能编译。
    拒绝时**必须说清为什么**——用户不知道「回溯爆炸」是什么，要给他能改的方向。
    """
    p = str(pattern or "").strip()
    if not p:
        return False, "关键词是空的。用法：/watch 关键词 <正则>，例如 /盯着 关键词 报价|合同"
    if len(p) > KEYWORD_MAX_LEN:
        return False, (f"这个正则太长了（{len(p)} 字 > 上限 {KEYWORD_MAX_LEN}）。"
                       f"写短一点：只保留真正要匹配的那几个词。")
    if _BOMB.search(p):
        return False, ("这个正则里有**嵌套量词**（像 `(a+)+` 这种），它有可能会"
                       "「回溯爆炸」把收消息那条线程卡死。改成不含嵌套量词的写法，"
                       "比如把 `(ab+)+` 改成 `ab+`。")
    try:
        re.compile(p)
    except re.error as e:
        return False, f"这不是一个合法正则：{e}"
    return True, ""


def match_keywords(text, kws):
    """这条文本命中了哪些关键词。**只扫前 KEYWORD_SCAN_CHARS 个字符**（见文件头注释）。"""
    body = str(text or "")
    if not body:
        return []
    clip = body[:KEYWORD_SCAN_CHARS]
    out = []
    for kw in (kws or []):
        pat = str((kw or {}).get("pattern") or "")
        if not pat:
            continue
        try:
            if re.search(pat, clip):
                out.append(kw)
        except re.error:
            # 加的时候编译过了；这里再坏说明有人手改了配置文件。如实告警、跳过。
            print(f"⚠️ 关键词正则编译失败，已跳过：{pat!r}")
    return out


def format_keyword_hit(rec, who, text, limit=200):
    """关键词命中的通知文案。要说清**在哪个会话**命中的。"""
    pat = str((rec or {}).get("raw") or (rec or {}).get("pattern") or "")
    body = str(text or "").strip()
    if len(body) > limit:
        body = body[:limit] + "…"
    return f"🔔 关键词「{pat}」命中 —— {who}：{body}"


def _save(**changes):
    """只把命令管的键写进 settings.json 的 watch 段（基准取磁盘现值，不是传进来的 cfg）。"""
    saved = settings.load().get("watch")
    data = dict(saved) if isinstance(saved, dict) else {}
    data.update(changes)
    settings.set_value("watch", {k: v for k, v in data.items() if k in MANAGED})


def format_hit(rec, text, limit=200):
    """通知文案。太长的话截断——通知是让我知道「他说话了」，不是全文转播。"""
    name = str((rec or {}).get("name") or (rec or {}).get("wxid") or "某人")
    body = str(text or "").strip()
    if len(body) > limit:
        body = body[:limit] + "…"
    return f"👀 {name}：{body}"


def status_text(cfg):
    recs = chat_list(cfg)
    kws = keywords(cfg)
    lines = [f"盯着：{'开启' if enabled(cfg) else '已关闭'}",
             f"名单（{len(recs)}）："]
    if not recs:
        lines.append("  （空）发 /watch 加 <昵称|wxid> 添加")
    for r in recs:
        lines.append(f"  · {r.get('name') or r.get('wxid')}（{r.get('wxid')}）")
    lines.append(f"关键词（{len(kws)}）：")
    if not kws:
        lines.append("  （空）发 /watch 关键词 <正则> 添加，例如 /盯着 关键词 报价|合同")
    for k in kws:
        lines.append(f"  · {k['raw']}")
    lines.append("")
    lines.append("他们发消息我会通知你，但不会回他们"
                 + ("；关键词在**任何会话**里命中都会通知。" if kws else "。"))
    if kws:
        lines.append(f"⚠️ 关键词只看**文本**消息，而且只扫每条消息的前 "
                     f"{KEYWORD_SCAN_CHARS} 个字符（回溯会卡住收消息线程）。")
    return "\n".join(lines)


def summary_line(cfg):
    recs = chat_list(cfg)
    if not recs:
        return "盯着：无"
    who = "、".join(str(r.get("name") or r.get("wxid")) for r in recs[:6])
    more = "" if len(recs) <= 6 else f" 等 {len(recs)} 个"
    return f"盯着 {'开' if enabled(cfg) else '关'}着：{who}{more}"


def build_arg(action, who=""):
    """把 agent 工具的结构化参数拼成 /盯着 的子命令串（和命令走同一条实现）。"""
    a = str(action or "").strip().lower()
    who = str(who or "").strip()
    if a in ("status", "list", "列表", ""):
        return ""
    if a in ("add", "加", "添加"):
        return f"加 {who}".strip()
    if a in ("del", "delete", "删", "删除"):
        return f"删 {who}".strip()
    if a in ("keyword", "keywords", "关键词"):
        return f"关键词 {who}".strip()
    if a in ("keyword_del", "关键词删", "del_keyword"):
        return f"关键词 删 {who}".strip()
    if a in ("on", "开", "off", "关"):
        return "开" if a in ("on", "开") else "关"
    return a


def _find(recs, who):
    for r in recs:
        if who in (str(r.get("wxid")), str(r.get("name"))):
            return r
    return None


def handle_command(arg, cfg, resolve, name_hint=None):
    """处理 /盯着 系列子命令。返回 (回复文本, 是否改了配置)。

    resolve(who) -> (候选人, 错误文本)，由调用方提供（重名时不静默取第一个）。
    """
    parts = str(arg or "").split(maxsplit=1)
    sub = parts[0].strip().lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    recs = chat_list(cfg)

    if not sub or sub in ("status", "list", "状态", "列表", "名单"):
        return status_text(cfg), False

    if sub in ("on", "开", "开启"):
        _save(enabled=True, chats=recs)
        return "盯着已开启：名单里的人发消息我会通知你。", True

    if sub in ("off", "关", "关闭"):
        _save(enabled=False, chats=recs)
        return "盯着已关闭，名单保留着（发 /watch 开 恢复）。", True

    if sub in ("add", "加", "添加"):
        if not rest:
            return _USAGE, False
        cand, err = resolve(rest)
        if err:
            return err, False
        wxid = str(cand.get("wxid"))
        disp = str(name_hint or "").strip() or (cand.get("remark") or cand.get("name")
                                               or wxid)

        # 和自动回复名单互斥：两边都加会既通知又自动回复，看着像抽风
        import auto_reply
        if wxid in auto_reply.chats(cfg):
            return (f"{disp} 已经在「自动回复」名单里了。盯着是「只通知不回」，"
                    f"和自动回复是互斥的——想只收通知就先发 /auto del {disp}。"), False

        rec = _find(recs, wxid)
        if rec is None:
            recs.append({"wxid": wxid, "name": disp})
            _save(chats=recs, enabled=enabled(cfg))
            tail = "" if enabled(cfg) else "\n（盯着总开关是关着的，发 /watch 开 才会生效）"
            return f"已开始盯着：{disp}。他发消息我会通知你，不回他。{tail}", True
        if rec.get("name") != disp:      # 昵称变了顺手更新
            rec["name"] = disp
            _save(chats=recs, enabled=enabled(cfg))
            return f"{disp} 已经在盯着了（显示名更新了）。", True
        return f"{disp} 已经在盯着了。", False

    if sub in ("del", "delete", "删", "删除"):
        if not rest:
            return _USAGE, False
        cand, err = resolve(rest)
        wxid = str(cand.get("wxid")) if not err and cand else rest
        rec = _find(recs, wxid) or _find(recs, rest)
        if rec is None:
            return f"名单里没有「{rest}」。发 /watch 看名单。", False
        _save(chats=[r for r in recs if r is not rec], enabled=enabled(cfg))
        return f"已不再盯着：{rec.get('name') or rec.get('wxid')}。", True

    if sub in ("keyword", "keywords", "关键词", "词"):
        if not rest:
            kws = keywords(cfg)
            if not kws:
                return ("还没有关键词。\n" + _USAGE), False
            return ("已有关键词（**任何会话**里命中都会通知我）：\n"
                    + "\n".join(f"  · {k['raw']}" for k in kws)
                    + "\n\n删除：/watch 关键词 删 <正则>\n"
                      f"⚠️ 只扫**文本**消息的前 {KEYWORD_SCAN_CHARS} 个字符。"), False
        head, _, tail_arg = rest.partition(" ")
        if head.lower() in ("del", "delete", "删", "删除", "移除"):
            target = tail_arg.strip()
            if not target:
                return "用法：/watch 关键词 删 <正则>", False
            kws = keywords(cfg)
            kept = [k for k in kws if k["raw"] != target and k["pattern"] != target]
            if len(kept) == len(kws):
                return f"关键词里没有「{target}」。发 /watch 关键词 看已有的。", False
            _save(keywords=kept, chats=recs, enabled=enabled(cfg))
            return f"已删除关键词「{target}」。", True
        ok, why = check_pattern(rest)
        if not ok:
            return why, False
        kws = keywords(cfg)
        if any(k["pattern"] == rest for k in kws):
            return f"关键词「{rest}」已经在里面了。", False
        kws.append({"pattern": rest, "raw": rest})
        _save(keywords=kws, chats=recs, enabled=enabled(cfg))
        tail = "" if enabled(cfg) else "\n（盯着总开关是关着的，发 /watch 开 才会生效）"
        return (f"已加入关键词「{rest}」：任何会话里命中就通知我（不回他）。\n"
                f"⚠️ 只看**文本**消息，而且只扫每条消息的前 {KEYWORD_SCAN_CHARS} 个字符"
                f"——出现在很后面的词匹配不到。{tail}"), True

    return _USAGE, False


if __name__ == "__main__":
    # 纯逻辑自测：不碰微信、不碰 hook、不写 settings.json。
    # 跑： .venv/Scripts/python.exe watch.py
    def chk(cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            raise SystemExit(1)

    contacts = [{"wxid": "wxid_z", "name": "张三", "remark": "张三"},
                {"wxid": "wxid_l", "name": "李四", "remark": "李四"}]

    def resolve(who):
        hits = [c for c in contacts if who in (c["wxid"], c["name"], c["remark"])]
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"没找到「{who}」。"
        return None, f"「{who}」匹配到多个人，请用全名或直接给 wxid。"

    saved = {}
    cfg = {"watch": {"enabled": True, "chats": []}, "auto_reply": {"chats": []}}

    def _fake_save(**kw):
        saved.update(kw)
        if "chats" in kw:
            cfg["watch"]["chats"] = kw["chats"]
        if "enabled" in kw:
            cfg["watch"]["enabled"] = kw["enabled"]

    real_save = _save
    _save = _fake_save
    try:
        print("名单管理:")
        out, ch = handle_command("", cfg, resolve)
        chk(not ch and "（空）" in out, "空名单有提示")

        out, ch = handle_command("加 张三", cfg, resolve)
        chk(ch and "已开始盯着" in out, "加进名单")
        chk([c["wxid"] for c in cfg["watch"]["chats"]] == ["wxid_z"], "落的是 wxid 不是昵称")

        out, ch = handle_command("加 张三", cfg, resolve)
        chk(not ch and "已经在盯着" in out, "重复加不报错、也不重复落")

        out, ch = handle_command("加 王五", cfg, resolve)
        chk(not ch and "没找到" in out, "找不到的人不加")

        out, ch = handle_command("", cfg, resolve)
        chk("张三" in out and "（wxid_z）" in out, "列表显示名 + wxid")

        print("和自动回复互斥:")
        cfg["auto_reply"]["chats"] = [{"wxid": "wxid_l", "name": "李四"}]
        out, ch = handle_command("加 李四", cfg, resolve)
        chk(not ch and "自动回复" in out, "已在自动回复名单里的人会被拦下")

        print("开关:")
        out, ch = handle_command("关", cfg, resolve)
        chk(ch and enabled(cfg) is False, "关掉总开关")
        out, ch = handle_command("", cfg, resolve)
        chk("已关闭" in out, "状态里显示已关闭")
        chk(len(cfg["watch"]["chats"]) == 1, "关开关不丢名单")
        out, ch = handle_command("开", cfg, resolve)
        chk(ch and enabled(cfg) is True, "再打开")

        print("删:")
        out, ch = handle_command("删 张三", cfg, resolve)
        chk(ch and not cfg["watch"]["chats"], "按昵称删掉")

        print("通知文案:")
        hit = format_hit({"name": "张三"}, "在吗")
        chk(hit == "👀 张三：在吗", f"正常文案（实际 {hit!r}）")
        long_hit = format_hit({"name": "张三"}, "啊" * 300)
        chk(len(long_hit) < 230 and long_hit.endswith("…"), "超长会截断")
        chk("wxid_z" in format_hit({"wxid": "wxid_z"}, "喂"), "没有昵称时退回 wxid")
    finally:
        globals()["_save"] = real_save

    print("\n全部通过。")
