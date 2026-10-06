"""联系人分组：**自己维护的一份名单**，群发时可以按组发。

和 watch.py 是同一个形状（名单 + 命令 + 工具三件套），但管的事情不同：

    auto_reply   代我回某个人（会发消息给对方）
    watch        只告诉我某个人说了什么（不出站）
    groups       把联系人分成组，**只用来决定群发发给谁**

**为什么自己存一份，而不是直接用微信自带的「标签」**（2026-10-01 和用户定的）：
微信的标签在 `contact.db` 的 `contact_label` 里，那是**另一个进程的库**——读它得先
探清表结构（当时没人记录过它的列），探的时候还必须停 bot（轮询期间手工查库会把
微信搞崩，见 CLAUDE.md 的 hook 铁律）。所以先做一份自己的：数据放 settings.json
的 groups 段，随时可改、**一次库都不查**。之后接微信标签时两边共存，群发时都能点名。

本模块**不发消息、不碰 hook、不起线程**：它只读写配置 + 拼给人看的话。
出站那些事在 `agent_tools.prepare_broadcast` 里，走的是同一套待确认闸门。

⚠️ 唯一的例外是 `/分组 标签`（看**微信自带**的标签）：那要读一次库，走
`live_history.label_names()` / `contacts_in_label()`。自己建的分组**一次库都不查**。
"""
import re

import live_history
import settings

# 只有这个顶层键由本模块写。settings.effective() 对 dict 做一层深合并，
# 所以 config.yaml 里的 groups 默认值和这里存的不冲突。
_KEY = "groups"

# 组名**不能带空格**。命令是 `/分组 加 <组名> <人名、人名>`，带空格的组名就没法切了——
# 而含糊的分隔规则会让「加 大学 同学 张三」这种输入**静默错解**，不如直接拒。
# 人名的分隔符可以随便用（、，,;；/）。
_NAME_SEP = r"[、,，;；/]"

_USAGE = (
    "用法（组名不能带空格；多个人用「、」隔开）：\n"
    "  /groups                    看所有分组和成员\n"
    "  /groups add <组名> <人名、人名>   建一个组，或往已有的组里加人（不存在就建）\n"
    "  /groups remove <组名> <人名、人名>  从组里移人\n"
    "  /groups del <组名>           删掉整个组\n"
    "  /groups labels                看**微信自带**的标签（只读，不算分组）\n"
    "（中文子命令也还能用：/分组 建|加 / 移 / 删 / 标签）\n"
    "群发按组发：说「给大学同学组发…」，或 to=\"分组:大学同学\"；\n"
    "微信标签也能直接发：说「给亲人发…」，或 to=\"标签:亲人\"。"
)


def is_labels_arg(sub):
    """这个子命令是不是「看微信标签」。命令和工具两侧共用，别各写一份。"""
    return sub in ("标签", "微信标签", "labels", "label", "tags", "tag")


def all_groups(cfg):
    """`{组名: [{"wxid","name"}, ...]}`。条目按引用返回，命令里改完再 _save。

    成员**同时存 wxid 和显示名**：wxid 是真正用来发消息的（改备注也不会失效），
    显示名是给人和模型看的——只存 wxid 的话，没加载联系人表时就只能把一串
    原始 id 摆出来（CLAUDE.md 明令禁止把 id 当名字用）。
    """
    raw = (cfg or {}).get(_KEY)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, members in raw.items():
        gname = str(name or "").strip()
        if not gname or not isinstance(members, (list, tuple)):
            continue
        clean = []
        for m in members:
            if not isinstance(m, dict):
                continue
            wxid = str(m.get("wxid") or "").strip()
            if not wxid:
                continue
            clean.append({"wxid": wxid,
                          "name": str(m.get("name") or "").strip() or wxid})
        out[gname] = clean
    return out


def group_names(cfg):
    """所有组名（按名字排序，状态页里稳定）。"""
    return sorted(all_groups(cfg).keys())


def members(cfg, name):
    """某个组的成员列表；没有这个组返回 None（**和「空组」区分开**）。"""
    return all_groups(cfg).get(str(name or "").strip())


def _save(groups):
    """只写自己那一个顶层键。基准取磁盘现值由调用方负责（见 handle_command）。"""
    settings.set_value(_KEY, dict(groups or {}))


def status_text(cfg):
    gs = all_groups(cfg)
    if not gs:
        return ("还没有任何分组。\n"
                "建一个：/groups add 大学同学 张三、李四\n"
                "（群发时就能说「给大学同学组发…」）")
    lines = [f"分组（{len(gs)}）："]
    for name in group_names(cfg):
        ms = gs[name]
        who = "、".join(m["name"] for m in ms[:8])
        more = "" if len(ms) <= 8 else f" 等 {len(ms)} 人"
        lines.append(f"  · {name}（{len(ms)} 人）：{who}{more}")
    lines.append("")
    lines.append("群发按组发：说「给<组名>组发…」，或 to=\"分组:<组名>\"。")
    return "\n".join(lines)


def summary_line(cfg):
    names = group_names(cfg)
    if not names:
        return "分组：无"
    head = "、".join(names[:6])
    more = "" if len(names) <= 6 else f" 等 {len(names)} 个"
    return f"分组（{len(names)}）：{head}{more}"


def labels_text(client):
    """看**微信自带**的标签（只读）。成员关系只有库里才有，所以这里会查库。

    标签是微信那边维护的，本模块**不改它** —— 列出来是为了让用户知道
    「发标签」可以用哪些名字，以及每个标签里有几个人（人数超上限时提前知道）。
    """
    if client is None:
        return ("这条链路读不到微信标签（没有查库能力）。\n"
                "自己建分组照样能群发：/groups add 大学同学 张三、李四")
    try:
        labs = live_history.label_names(client)
    except Exception as e:
        return f"读微信标签失败：{e}。这多半是 hook 查库出问题了，先看看 /status。"
    if labs is None:
        # 「读不到」和「没有标签」必须分开说，混在一起用户会以为标签丢了
        return ("读**不到**微信标签（查库没成功，不是「你没有标签」）。"
                "先看看 /status 里 hook 正不正常；自己建的分组不受影响。")
    if not labs:
        return ("微信里还没有建过标签。\n"
                "（标签在微信「通讯录 → 标签」里建；建好后这里就能看到，"
                "群发时说「给<标签名>发…」即可。）")
    lines = [f"微信自带的标签（{len(labs)}，只读，不算分组）："]
    for l in labs:
        try:
            ws = live_history.contacts_in_label(client, l["name"])
        except Exception as e:
            lines.append(f"  · {l['name']}（读成员失败：{e}）")
            continue
        if ws is None:
            lines.append(f"  · {l['name']}（成员读不到：查库没成功）")
            continue
        lines.append(f"  · {l['name']}（{len(ws)} 人）")
    lines.append("")
    lines.append("群发按标签发：说「给<标签名>发…」，或 to=\"标签:<标签名>\"。")
    lines.append("⚠️ 标签在微信那边改；这里只能看。要自己攒一份名单就用 /groups add。")
    return "\n".join(lines)


def build_arg(action, group="", who=""):
    """把 agent 工具的结构化参数拼成 /分组 的子命令串。

    和 watch / auto_reply 一个套路：工具和命令走**同一条**实现，省得两套逻辑
    各自跑偏（分组的增删改要是两套，早晚对不上）。
    """
    a = str(action or "").strip().lower()
    group = str(group or "").strip()
    who = str(who or "").strip()
    if a in ("status", "list", "列表", "", "看"):
        return ""
    if a in ("labels", "label", "tags", "tag", "标签", "微信标签"):
        return "标签"
    # add 当**建组（不存在就建）**用：「把张三加进『大学同学』」时用户并不关心
    # 那个组是不是刚建的，让他先去建一次纯属折磨。
    if a in ("add", "create", "加", "添加", "建", "新建"):
        return " ".join(x for x in ("建", group, who) if x)
    if a in ("remove", "移", "移出", "踢"):
        return " ".join(x for x in ("移", group, who) if x)
    if a in ("del", "delete", "删", "删除"):
        return " ".join(x for x in ("删", group) if x)
    return a


def _split_names(rest):
    return [x.strip() for x in re.split(_NAME_SEP, str(rest or "")) if x.strip()]


def _space_hint(names):
    """人名里带空格时给一句**针对性的**提示。

    真正常见的错法是「/分组 建 大学 同学 张三」——用户以为组名能带空格。
    不点破的话我们会去查一个叫「同学 张三」的人，然后回一句「没找到」，
    用户根本想不到问题出在组名上。
    """
    bad = [n for n in names if " " in n or "\t" in n]
    if not bad:
        return ""
    return (f"（「{bad[0]}」里有空格。**组名不能带空格**，人名之间用「、」隔开，"
            f"例：/groups add 大学同学 张三、李四）")


def _resolve_many(resolve, names, resolve_each):
    """把一批人名解析成成员条目。返回 `(成员列表, 错误文本)`。

    **一个对不上就整批拒绝**（和群发一个道理）：只加一半、剩下的悄悄算了，
    用户以后按组群发时才发现少了人——那时候已经发出去了。
    `resolve_each` 为 False 时不做解析（直接当 wxid 用，自测用）。
    """
    out = []
    for nm in names:
        if not resolve_each:
            out.append({"wxid": nm, "name": nm})
            continue
        cand, err = resolve(nm)
        if err:
            # `resolve` 的错误文本**已经带人名了**（「没找到「王五」。」），
            # 别再套一层，否则会变成「「王五」没找到「王五」。」。
            return [], f"{err}所以我**一个人都没动**，请用全名重来一次。"
        wxid = str(cand.get("wxid") or "")
        if not wxid:
            return [], f"「{nm}」没解析出 wxid，我**一个人都没动**。"
        out.append({"wxid": wxid,
                    "name": str(cand.get("remark") or cand.get("name")
                                or wxid).strip() or wxid})
    return out, ""


def handle_command(arg, cfg, resolve=None, resolve_each=True, client=None):
    """处理 /分组 系列子命令。返回 (回复文本, 是否改了配置)。

    `resolve(who) -> (候选人, 错误文本)` 由调用方提供（重名时不静默取第一个）。
    `client` 只有「标签」那一支要用（要看微信自带的标签）；不传就如实说读不到。
    """
    parts = str(arg or "").split(maxsplit=1)
    sub = parts[0].strip().lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    gs = all_groups(cfg)

    if not sub or sub in ("status", "list", "状态", "列表", "名单"):
        return status_text(cfg), False

    if is_labels_arg(sub):
        return labels_text(client), False

    if sub in ("建", "新建", "加", "添加", "add", "create"):
        bits = rest.split(maxsplit=1)
        if len(bits) < 2:
            return _USAGE, False
        gname, namelist = bits[0].strip(), bits[1].strip()
        names = _split_names(namelist)
        if not names:
            return _USAGE, False
        hint = _space_hint(names)
        if hint:
            return f"{hint}", False
        add, err = _resolve_many(resolve, names, resolve_each)
        if err:
            return err, False
        cur = gs.get(gname) or []
        have = {m["wxid"] for m in cur}
        fresh = [m for m in add if m["wxid"] not in have]
        if not fresh:
            return (f"「{gname}」里本来就有这几个人（{len(cur)} 人），没动。"), False
        gs[gname] = cur + fresh
        _save(gs)
        who = "、".join(m["name"] for m in fresh)
        return (f"{'已建分组' if not cur else '已加进'}「{gname}」：{who}"
                f"（现有 {len(gs[gname])} 人）。\n"
                f"群发时说「给{gname}组发…」就行。"), True

    if sub in ("移", "移出", "踢", "remove"):
        bits = rest.split(maxsplit=1)
        if len(bits) < 2:
            return _USAGE, False
        gname, namelist = bits[0].strip(), bits[1].strip()
        if gname not in gs:
            return f"没有「{gname}」这个分组。发 /groups 看有哪些。", False
        names = _split_names(namelist)
        if not names:
            return _USAGE, False
        kill, err = _resolve_many(resolve, names, resolve_each)
        if err:
            return err, False
        gone = {m["wxid"] for m in kill}
        left = [m for m in gs[gname] if m["wxid"] not in gone]
        if len(left) == len(gs[gname]):
            return f"「{gname}」里没有这几个人，没动。", False
        removed = [m["name"] for m in gs[gname] if m["wxid"] in gone]
        if left:
            gs[gname] = left
        else:
            # 移空了就把组删掉：留一个空组只会在群发时撞「没有收件人」，
            # 而用户看到的是一句含糊的报错。
            del gs[gname]
        _save(gs)
        tail = f"「{gname}」空了，已经把组删掉。" if not left else f"「{gname}」还剩 {len(left)} 人。"
        return f"已从「{gname}」移出：{'、'.join(removed)}。{tail}", True

    if sub in ("删", "删除", "del", "delete"):
        gname = rest.strip()
        if not gname:
            return _USAGE, False
        if gname not in gs:
            return f"没有「{gname}」这个分组。发 /groups 看有哪些。", False
        n = len(gs.pop(gname))
        _save(gs)
        return f"已删掉分组「{gname}」（原来 {n} 人）。人本身没动。", True

    return _USAGE, False
