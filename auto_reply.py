"""自动回复：在指定会话里代替用户本人回消息。

和 bot.py 的分工：
  * bot.py 管「什么时候该回」——判定会话、防自回环、min_gap 冷却、发送或转审核；
  * 本模块只管「回什么」——拼上下文 → 让模型生成 → 清洗成能直接发出去的文本。

三条硬约束：
  * **只用 llm.chat()，不带工具**。自动回复里模型绝不该自己去给别人发消息。
  * 群聊里模型要能选择「不回复」，用严格 JSON 表达；解析不出来一律当静默——
    宁可少回一句，也不要在群里刷屏或发一句没头没尾的话。
  * 输出必须清洗成「能直接发进微信」的形态：单行、无 markdown、长度可控。
    清洗后为空**一律不发**（send_text 发空串在微信里是条空消息）。
"""
import json
import re

import live_history
import settings

# 单聊必回；群聊靠 SILENT 表达「这条我不该插话」
SILENT = None

_MODE_WORDS = ("self", "assistant")

_DEFAULT_SELF = (
    "你正在代替我本人回复微信消息。用我的口吻、第一人称、简短自然，"
    "就像平时打微信那样。不要暴露你是 AI，不要用书面语、列表或 markdown。"
    "不确定的事别编，就说「我回头确认下」。不要复述对方刚说过的话。"
)
_DEFAULT_ASSISTANT = (
    "你是我的微信 AI 助手，现在代我回复消息。礼貌、简洁，"
    "必要时说明自己是助手。不要用 markdown。"
)

_COMMON_RULES = """
【硬性要求】
- 只输出要发出去的那条消息本身。不要解释、不要前缀、不要引号、不要 markdown。
- 像真人发微信：口语、简短（一两句），不要长篇大论，不要分点列表。
- 直接回应对方，不要复述对方刚说过的话。
"""

_GROUP_RULES = """
【当前是群聊】
先判断最后一条消息是否在跟我说话、或是否需要我回应。
- 需要回应：只输出 {"reply": "你要发的内容"}
- 不需要回应（与我无关、别人之间的闲聊、别人之间道谢寒暄等）：只输出 {"reply": null}
- 除了这个 JSON，什么都不要输出。
例子：{"reply": "好的，我下午过去"}
例子：{"reply": null}
"""

_FENCE = re.compile(r"```[a-zA-Z0-9]*\n?(.*?)```", re.S)
_LEAD = re.compile(r"^(回复|答复|reply|我)\s*[:：]\s*", re.I)
_MD = re.compile(r"\*\*|__|`")


def is_group(chat_id):
    """群会话 id 以 @chatroom 结尾（aixed 的 Msg.roomid 就是这么判的）。"""
    return str(chat_id or "").endswith("@chatroom")


def contact_names(contacts):
    """wxid -> 显示名（备注优先），群里标发言人用。"""
    out = {}
    for c in contacts or []:
        wxid = str(c.get("wxid") or "")
        if wxid:
            out[wxid] = str(c.get("remark") or c.get("name") or wxid)
    return out


def build_transcript(msgs, names, limit=20):
    """把历史渲染成带说话人的文本。时间从早到晚，最后一条是刚收到的。"""
    lines = []
    for m in (msgs or [])[-int(limit):]:
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if m.get("is_self"):
            who = "我"
        else:
            # 群聊靠 sender（wxid）区分发言人；取不到就退回「对方」
            who = names.get(str(m.get("sender") or "")) or "对方"
        lines.append(f"[{m.get('time', '?')}] {who}: {content}")
    return "\n".join(lines)


def sanitize(text, max_chars=200):
    """把模型输出洗成能直接发进微信的一条消息。洗空了返回 ""（调用方据此静默）。"""
    if text is None:
        return ""
    t = str(text).strip()
    if not t:
        return ""
    t = _FENCE.sub(lambda m: m.group(1), t)      # 去代码围栏，保留围栏里的字
    t = _LEAD.sub("", t.strip())
    t = t.replace("\r", " ").replace("\n", " ")   # 一条微信消息里不塞换行
    t = _MD.sub("", t)
    t = t.strip().strip('"').strip("'").strip("“”").strip("「」")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    try:
        limit = max(1, int(max_chars))
    except (TypeError, ValueError):
        limit = 200
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def _extract_group_reply(raw):
    """从群聊回复里取出内容。返回 None 表示静默。

    模型大概率会老实给 JSON，但也可能套一层 ```json 围栏或在外面多说一句，
    所以先剥围栏再抓第一对花括号。
    **完全看不到 JSON 时**（文本里没有花括号）就把整段当回复——那说明模型
    直接答了，当静默会白白把功能废掉。解析失败才当静默（fail-safe）。
    """
    if not raw:
        return SILENT
    t = _FENCE.sub(lambda m: m.group(1), str(raw)).strip()
    if "{" not in t:
        return t.strip() or SILENT
    start, end = t.find("{"), t.rfind("}")
    if end <= start:
        return SILENT
    try:
        data = json.loads(t[start:end + 1])
    except (ValueError, TypeError):
        return SILENT
    if not isinstance(data, dict):
        return SILENT
    val = data.get("reply")
    if val is None:
        return SILENT
    return str(val)


def persona_for(rec, auto_cfg):
    """这个人设：聊天单独配的 > 全局对应 mode 的 > 代码里的兜底。"""
    own = str(rec.get("persona") or "").strip()
    if own:
        return own
    mode = str(rec.get("mode") or "self").lower()
    if mode == "assistant":
        return str(auto_cfg.get("persona_assistant") or "").strip() or _DEFAULT_ASSISTANT
    return str(auto_cfg.get("persona_self") or "").strip() or _DEFAULT_SELF


def make_reply(llm, rec, msgs, names, cfg, group=False):
    """生成一条可直接发送的回复。返回 "" 表示不该发（静默）。"""
    auto_cfg = cfg.get("auto_reply") or {}
    transcript = build_transcript(msgs, names, auto_cfg.get("context_messages", 20))
    if not transcript:
        return ""

    system = persona_for(rec, auto_cfg) + "\n" + _COMMON_RULES
    if group:
        system += _GROUP_RULES
        ask = "按要求的 JSON 输出，判断最后一条消息是否需要我回应。"
    else:
        ask = "请回复最后一条消息。"

    prompt = ("【最近的聊天记录】（时间从早到晚，最后一条是刚收到的）\n"
              f"{transcript}\n\n{ask}")
    raw = llm.chat(system, [{"role": "user", "content": prompt}])
    picked = _extract_group_reply(raw) if group else raw
    return sanitize(picked, auto_cfg.get("max_reply_chars", 200))


# ============================================================
#  配置读写（/auto 命令）
# ============================================================

def section(cfg):
    return dict(cfg.get("auto_reply") or {})


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
    return bool(section(cfg).get("enabled"))


def review_on(rec, cfg):
    """这个人设用不用审核：聊天单独配的优先，否则看全局。"""
    own = rec.get("review")
    return bool(section(cfg).get("review") if own is None else own)


# 只有这几个键由命令管理、也**只把**它们写进 settings.json。
# persona_* / min_gap / max_reply_chars 留在 config.yaml：settings.json 里
# 存一份副本的话，用户之后改 config.yaml 就再也不生效了。
_MANAGED = ("enabled", "review", "chats", "context_messages")


def _save(**changes):
    """把改动写进 settings.json 的 auto_reply 段（只写命令管的键）。

    settings.effective() 对 dict 做一层深合并，所以这里不需要写整段——
    config.yaml 里的 persona_* 等默认值仍然会生效。

    基准取自磁盘上的现值（不是传进来的 cfg）：cfg 可能是上一条命令之前的
    快照，用它当基准会把上一条命令的改动丢掉。
    """
    saved = settings.load().get("auto_reply")
    data = dict(saved) if isinstance(saved, dict) else {}
    data.update(changes)
    settings.set_value("auto_reply", {k: v for k, v in data.items() if k in _MANAGED})


def _split_target(rest):
    """'张三 self' / '张三' / 'wxid_xxx' -> (目标, mode 或 None)。

    昵称可能带空格，所以只把**最后一个词**是 self/assistant 时才当 mode 剥掉。
    """
    parts = str(rest or "").split()
    mode = None
    if len(parts) >= 2 and parts[-1].lower() in _MODE_WORDS:
        mode = parts[-1].lower()
        parts = parts[:-1]
    return " ".join(parts).strip(), mode


def _resolve(client, who, can_lookup=True):
    """昵称/备注/wxid/roomid -> (wxid, 显示名, 候选列表)。"""
    who = str(who or "").strip()
    if not who:
        return None, None, []
    if who.endswith("@chatroom"):
        return who, who, [{"wxid": who}]
    if who.lower().startswith("wxid_") or who == "filehelper":
        return who, who, [{"wxid": who}]
    if not can_lookup:
        return None, None, []
    try:
        cands = live_history.resolve_contact(client, who, limit=5)
    except Exception:
        cands = []
    if not cands:
        return None, None, []
    c = cands[0]
    return str(c.get("wxid")), str(c.get("remark") or c.get("name") or c.get("wxid")), cands


def _find(recs, who):
    """按 wxid 精确、再按显示名精确、再按名字包含，找一条记录。"""
    who = str(who or "").strip()
    if not who:
        return None
    for r in recs:
        if str(r.get("wxid")) == who:
            return r
    for r in recs:
        if str(r.get("name") or "") == who:
            return r
    hits = [r for r in recs if who in str(r.get("name") or "")]
    return hits[0] if len(hits) == 1 else None


def status_text(cfg):
    sec = section(cfg)
    recs = chat_list(cfg)
    lines = [
        f"自动回复：{'开启' if sec.get('enabled') else '关闭'}",
        f"审核模式：{'开启（草稿先发给你，回「确认」才发出去）' if sec.get('review') else '关闭（直接发给对方）'}",
        (f"上下文 {sec.get('context_messages', 20)} 条  |  "
         f"冷却 {sec.get('min_gap', 6)} 秒  |  单条上限 {sec.get('max_reply_chars', 200)} 字"),
        f"名单（{len(recs)}）：",
    ]
    if not recs:
        lines.append("  （空）发 /auto add <昵称|wxid|roomid> [self|assistant] 添加")
    for r in recs:
        rev = sec.get("review") if r.get("review") is None else r.get("review")
        kind = "（群）" if is_group(r.get("wxid")) else ""
        lines.append(f"  · {r.get('name') or r.get('wxid')}{kind}  "
                     f"人设={r.get('mode') or 'self'}  审核={'开' if rev else '关'}")
    return "\n".join(lines)


_USAGE = (
    "用法：\n"
    "/auto                    看状态和名单\n"
    "/auto on | off           开 / 关（关掉就你自己回）\n"
    "/auto add <昵称|wxid|roomid> [self|assistant]\n"
    "/auto del <昵称|wxid>\n"
    "/auto mode <昵称|wxid> self|assistant\n"
    "/auto review on|off [昵称|wxid]   不带对象则改全局\n"
    "/auto ctx <1~30>         上下文条数\n"
    "（群只能填 roomid，形如 xxx@chatroom）"
)


def summary_line(cfg):
    """一行话的状态摘要，给 agent 工具复述用。"""
    sec = section(cfg)
    recs = chat_list(cfg)
    if not recs:
        who = "名单是空的"
    else:
        names = "、".join(str(r.get("name") or r.get("wxid")) for r in recs[:6])
        more = "" if len(recs) <= 6 else f" 等 {len(recs)} 个"
        who = f"名单：{names}{more}"
    return (f"{'开' if sec.get('enabled') else '关'}着，{who}，"
            f"审核{'开' if sec.get('review') else '关'}")


def build_arg(action, who="", mode=None, review=None, context=None):
    """把 agent 工具的结构化参数拼成 /auto 的子命令串。

    让工具和命令走**同一条**实现（handle_command），省得两套逻辑各自跑偏。
    """
    a = str(action or "").strip().lower()
    who = str(who or "").strip()
    if a in ("on", "off", "status", ""):
        return a
    if a == "add":
        return " ".join(x for x in ("add", who, mode) if x)
    if a == "del":
        return f"del {who}".strip()
    if a == "mode":
        return f"mode {who} {mode or 'self'}".strip()
    if a == "review":
        if review is None:
            return "review"
        return f"review {'on' if review else 'off'} {who}".strip()
    if a == "ctx":
        if context is None:
            return "ctx"
        try:
            return f"ctx {int(context)}"
        except (TypeError, ValueError):
            return "ctx"
    return a


def handle_command(arg, cfg, client, can_lookup=True, name_hint=None):
    """处理 /auto 系列子命令。返回 (回复文本, 是否改了配置)。

    name_hint 是给 agent 工具用的：工具会先把昵称解析成 wxid（它的精确匹配比
    这里严格），再让 `/auto add` 按 wxid 走，显示名就会被记成 wxid_xxx，
    /auto 列表里看不到人名。带上原名当显示名即可。
    """
    parts = str(arg or "").split(maxsplit=1)
    sub = parts[0].strip().lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    sec = section(cfg)
    recs = chat_list(cfg)

    if not sub or sub in ("status", "list", "状态", "名单"):
        return status_text(cfg), False

    if sub in ("on", "开", "开启", "启用"):
        _save(enabled=True)
        return "自动回复已开启：名单里的人发消息，我代你回。", True

    if sub in ("off", "关", "关闭", "停"):
        _save(enabled=False)
        return "自动回复已关闭，这些聊天你自己回。", True

    if sub in ("add", "加", "添加"):
        who, mode = _split_target(rest)
        if not who:
            return _USAGE, False
        wxid, disp, cands = _resolve(client, who, can_lookup)
        if not wxid:
            hint = ("当前实时查库不可用，只能直接填 wxid（群填 roomid）。"
                    if not can_lookup else
                    f"没找到「{who}」。群请直接填 roomid（xxx@chatroom）。")
            return hint, False
        if len(cands) > 1:
            names = "；".join(f"{c.get('remark') or c.get('name')}({c.get('wxid')})"
                              for c in cands[:5])
            return f"「{who}」匹配到多个人，请用全名或直接给 wxid：\n{names}", False
        if wxid in {str(t) for t in (cfg.get("target_chats") or [])}:
            return (f"{disp} 已经是控制会话（target_chats）了：控制会话是你跟助手说话的地方，"
                    f"不能同时当自动回复对象。请二选一。"), False
        if wxid == "filehelper" or (str(cfg.get("self_wxid") or "") and wxid == str(cfg.get("self_wxid"))):
            return "文件传输助手 / 你自己不能加进自动回复名单（会自己回自己）。", False

        rec = next((r for r in recs if str(r.get("wxid")) == wxid), None)
        disp = str(name_hint or "").strip() or disp
        if rec is None:
            rec = {"wxid": wxid, "name": disp, "mode": mode or "self",
                   "review": None, "persona": ""}
            recs.append(rec)
            head = f"已加入自动回复：{disp}"
        else:
            if mode:
                rec["mode"] = mode
            head = f"{disp} 已在名单里"
        _save(chats=recs)
        tail = "" if sec.get("enabled") else "\n总开关还是关着的，记得发 /auto on。"
        return (f"{head}（人设={rec.get('mode')}，"
                f"self=假装你本人 / assistant=明说是助手）。{tail}"), True

    if sub in ("del", "delete", "remove", "删", "删除"):
        who, _ = _split_target(rest)
        rec = _find(recs, who)
        if rec is None:
            return f"名单里没有「{who}」。发 /auto 看名单。", False
        _save(chats=[r for r in recs if r is not rec])
        return f"已移出自动回复名单：{rec.get('name') or rec.get('wxid')}。", True

    if sub in ("mode", "人设"):
        who, mode = _split_target(rest)
        if not who or not mode:
            return "用法：/auto mode <昵称|wxid> self|assistant", False
        rec = _find(recs, who)
        if rec is None:
            return f"名单里没有「{who}」。", False
        rec["mode"] = mode
        _save(chats=recs)
        return f"{rec.get('name') or rec.get('wxid')} 的人设已改为 {mode}。", True

    if sub in ("review", "审核"):
        bits = rest.split()
        if not bits:
            return "用法：/auto review on|off [昵称|wxid]（不带对象则改全局）", False
        v = bits[0].lower()
        if v not in ("on", "off", "开", "关"):
            return "只认 on / off。例：/auto review on 张三", False
        on = v in ("on", "开")
        who = " ".join(bits[1:]).strip()
        if not who:
            _save(review=on)
            return (f"全局审核已{'开启' if on else '关闭'}。"
                    + ("自动回复的草稿会先发给你，你回「确认」才发出去。"
                       if on else "自动回复直接发给对方。")), True
        rec = _find(recs, who)
        if rec is None:
            return f"名单里没有「{who}」。", False
        rec["review"] = on
        _save(chats=recs)
        return f"{rec.get('name') or rec.get('wxid')} 的审核已{'开启' if on else '关闭'}。", True

    if sub in ("ctx", "context", "上下文"):
        try:
            n = max(1, min(int(rest), 30))
        except ValueError:
            return "用法：/auto ctx 20（1~30 之间的整数）", False
        _save(context_messages=n)
        return f"上下文条数已设为 {n}。", True

    return _USAGE, False
