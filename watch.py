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
import settings

MANAGED = ("enabled", "chats")

_USAGE = (
    "用法：\n"
    "  /盯着 —— 看名单\n"
    "  /盯着 加 <昵称|wxid|roomid> —— 他发消息就通知我（不回他）\n"
    "  /盯着 删 <昵称|wxid>\n"
    "  /盯着 开|关 —— 总开关\n"
    "群也能盯：群填 roomid（形如 xxx@chatroom）。"
)


# ---------------- 读取 ----------------

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


def _save(**changes):
    """只把命令管的键写进 settings.json 的 watch 段（基准取磁盘现值，不是传进来的 cfg）。"""
    saved = settings.load().get("watch")
    data = dict(saved) if isinstance(saved, dict) else {}
    data.update(changes)
    settings.set_value("watch", {k: v for k, v in data.items() if k in MANAGED})


# ---------------- 触发 ----------------

def format_hit(rec, text, limit=200):
    """通知文案。太长的话截断——通知是让我知道「他说话了」，不是全文转播。"""
    name = str((rec or {}).get("name") or (rec or {}).get("wxid") or "某人")
    body = str(text or "").strip()
    if len(body) > limit:
        body = body[:limit] + "…"
    return f"👀 {name}：{body}"


# ---------------- 展示 ----------------

def status_text(cfg):
    recs = chat_list(cfg)
    lines = [f"盯着：{'开启' if enabled(cfg) else '已关闭'}", f"名单（{len(recs)}）："]
    if not recs:
        lines.append("  （空）发 /盯着 加 <昵称|wxid> 添加")
    for r in recs:
        lines.append(f"  · {r.get('name') or r.get('wxid')}（{r.get('wxid')}）")
    lines.append("")
    lines.append("他们发消息我会通知你，但**不会回**他们。")
    return "\n".join(lines)


def summary_line(cfg):
    recs = chat_list(cfg)
    if not recs:
        return "盯着：无"
    who = "、".join(str(r.get("name") or r.get("wxid")) for r in recs[:6])
    more = "" if len(recs) <= 6 else f" 等 {len(recs)} 个"
    return f"盯着 {'开' if enabled(cfg) else '关'}着：{who}{more}"


# ---------------- 命令 / 工具 ----------------

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
        return "盯着已关闭，名单保留着（发 /盯着 开 恢复）。", True

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
            return (f"{disp} 已经在**自动回复**名单里了。盯着是「只通知不回」，"
                    f"和自动回复是互斥的——想只收通知就先发 /auto del {disp}。"), False

        rec = _find(recs, wxid)
        if rec is None:
            recs.append({"wxid": wxid, "name": disp})
            _save(chats=recs, enabled=enabled(cfg))
            tail = "" if enabled(cfg) else "\n（盯着总开关是关着的，发 /盯着 开 才会生效）"
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
            return f"名单里没有「{rest}」。发 /盯着 看名单。", False
        _save(chats=[r for r in recs if r is not rec], enabled=enabled(cfg))
        return f"已不再盯着：{rec.get('name') or rec.get('wxid')}。", True

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
