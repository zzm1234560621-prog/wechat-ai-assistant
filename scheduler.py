"""定时任务：到点自动给某人发消息（以及将来打电话）。

**为什么不做成后台线程**：hook 不支持并发（并发调用会把微信搞崩，见 CLAUDE.md），
而轮询主循环本身是单线程的。所以定时器只在主循环**那一次 tick** 里跑，
天然和收消息串行——绝不会出现「定时任务正在发消息、同时又在轮询」的情况。

任务存在 settings.json 的 schedule 段（和 auto_reply 一样由命令维护），
config.yaml 里给默认值。字段：

    id         短标识，命令里用（t1 / t2 …）
    action     text=发文本；call=发起语音通话（**目前发不出去**，见下面 execute）
    to         对方 wxid（创建时就解析好并落盘，tick 里不再查库）
    to_name    显示名，只用于回显
    text       发的内容（action=text 用）
    repeat     once | daily | weekly | interval
    at         "HH:MM"（daily/weekly）
    date       "YYYY-MM-DD"（once）
    weekdays   [0..6]，周一=0（weekly）
    every_minutes  间隔分钟数（interval）
    enabled    true/false
    next_ts    下次触发的 epoch 秒（float）
    last_ts    上次触发的 epoch 秒
"""
import re
import time
from datetime import datetime, timedelta

import settings

MANAGED = ("enabled", "tasks")

# 每周几：周一是 0（跟 datetime.weekday() 一致）
_WEEKDAY = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
_WEEKDAY_CN = "一二三四五六日"

_USAGE = (
    "用法（也可以直接跟助手说「明天9点提醒我给张三发…」）：\n"
    "  /定时 —— 看列表\n"
    "  /定时 加 <时间> <对象> <内容> —— 加一个发文本的\n"
    "  /定时 加通话 <时间> <对象> —— 加一个打电话的（该功能还没打通）\n"
    "  /定时 删 <编号> —— 删掉\n"
    "  /定时 开|关 <编号|all> —— 恢复 / 暂停\n"
    "时间写法：9:00 是每天，明天9:00 是只一次，"
    "10-02 9:00 也是只一次，每周一 9:00 是每周，每30分钟 是每隔一段。\n"
    "例：/定时 加 明天9:00 张三 记得带伞"
)


# ---------------- 读取 ----------------

def section(cfg):
    return (cfg or {}).get("schedule") or {}


def tasks(cfg):
    return [t for t in (section(cfg).get("tasks") or []) if isinstance(t, dict)]


def enabled(cfg):
    return bool(section(cfg).get("enabled", True))


def _save(**changes):
    """只把命令管的键写进 settings.json 的 schedule 段。

    settings.effective() 对 dict 做一层深合并，所以不用写整段。
    """
    data = settings.load().get("schedule")
    data = dict(data) if isinstance(data, dict) else {}
    data.update(changes)
    settings.set_value("schedule", {k: v for k, v in data.items() if k in MANAGED})


def _next_id(recs):
    used = {str(r.get("id")) for r in recs}
    i = 1
    while f"t{i}" in used:
        i += 1
    return f"t{i}"


# ---------------- 时间解析 ----------------

def _norm(s):
    """全角冒号/空格归一，省得用户输入法不同就解析不了。"""
    return (str(s or "").strip()
            .replace("：", ":").replace("　", " ")
            .replace("点钟", ":").replace("点", ":").strip())


def _hhmm(s):
    """解析 "9:00" / "09:00" / "9"（只有小时）。返回 (h, m)。"""
    s = str(s or "").strip().rstrip("分").strip()
    try:
        if ":" in s:
            a, b = s.split(":", 1)
            h, m = int(a), int(b or 0)
        else:
            h, m = int(s), 0
    except ValueError:
        raise ValueError(f"时间「{s}」没看懂。例：9:00、09:30")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间「{s}」不对：小时要 0~23、分钟要 0~59。")
    return h, m


def parse_when(when, now=None):
    """把「时间写法」解析成任务字段。失败抛 ValueError（消息直接给用户看）。

    关键字和时间**允许连写**（「明天9:00」「每周一9:00」），所以用正则从头上吃。
    """
    now = now or datetime.now()
    s = _norm(when)
    if not s:
        raise ValueError("要写时间。例：明天9:00 / 9:00 / 每周一 9:00 / 每30分钟")

    # 每周X [HH:MM]
    m = re.match(r"^(?:每周|每星期)\s*([一二三四五六日天1-7])\s*(.*)$", s)
    if m:
        h, mi = _hhmm(m.group(2).strip() or "9:00")
        return {"repeat": "weekly", "weekdays": [_WEEKDAY[m.group(1)]],
                "at": f"{h:02d}:{mi:02d}"}

    # 间隔式
    m = re.match(r"^每\s*(\d+)\s*(分钟|分|小时|钟头)$", s)
    if m:
        n = int(m.group(1))
        mult = 1 if m.group(2) in ("分钟", "分") else 60
        if mult == 1 and n < 1:
            raise ValueError("间隔至少要 1 分钟。")
        return {"repeat": "interval", "every_minutes": n * mult}

    # 今天 / 明天 / 后天 [HH:MM] → 一次性
    m = re.match(r"^(今天|明天|后天)\s*(.*)$", s)
    if m:
        hm = m.group(2).strip()
        if not hm:
            raise ValueError(f"「{m.group(1)}」后面要跟时间，例：{m.group(1)}9:00")
        h, mi = _hhmm(hm)
        delta = {"今天": 0, "明天": 1, "后天": 2}[m.group(1)]
        d = (now + timedelta(days=delta)).date()
        return {"repeat": "once", "date": d.isoformat(), "at": f"{h:02d}:{mi:02d}"}

    # 每天 [HH:MM]
    m = re.match(r"^(每天|每日)\s*(.*)$", s)
    if m:
        hm = m.group(2).strip()
        if not hm:
            raise ValueError("「每天」后面要跟时间，例：每天9:00")
        h, mi = _hhmm(hm)
        return {"repeat": "daily", "at": f"{h:02d}:{mi:02d}"}

    # 到这儿还以「每」开头，说明是错写法（每天/每周/每N分钟都已排掉）
    if s.startswith("每"):
        raise ValueError(f"没看懂「{s}」。间隔写「每30分钟」/「每2小时」，"
                         f"每周写「每周一 9:00」。")

    # 具体日期 [YYYY-]MM-DD [HH:MM] → 一次性
    m = re.match(r"^(\d{1,4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2})\s*(.*)$", s)
    if m:
        date_s, hm = m.group(1), (m.group(2).strip() or "9:00")
        bits = [int(x) for x in date_s.split("-")]
        try:
            if len(bits) == 3:
                d = datetime(bits[0], bits[1], bits[2]).date()
            else:
                d = datetime(now.year, bits[0], bits[1]).date()
                if d < now.date():      # 只写月日时，过了就理解成明年
                    d = d.replace(year=now.year + 1)
        except ValueError:
            raise ValueError(f"日期「{date_s}」不存在。例：2026-10-02 9:00、10-02 9:00")
        h, mi = _hhmm(hm)
        return {"repeat": "once", "date": d.isoformat(), "at": f"{h:02d}:{mi:02d}"}

    # 光一个 HH:MM（或 HH）→ 每天
    h, mi = _hhmm(s)
    return {"repeat": "daily", "at": f"{h:02d}:{mi:02d}"}


def initial_next(spec, now=None):
    """新任务的下次触发时间（epoch）。"""
    now = now or datetime.now()
    rep = spec.get("repeat")
    if rep == "interval":
        return now.timestamp() + int(spec.get("every_minutes") or 0) * 60
    return _next_after(spec, now)


def _next_after(spec, after):
    """算出 `after` 之后的下一次触发时间；没有下次（一次性已过）返回 None。"""
    rep = spec.get("repeat")
    if rep == "interval":
        base = spec.get("last_ts") or after.timestamp()
        return base + int(spec.get("every_minutes") or 0) * 60
    h, m = _hhmm(spec.get("at") or "9:00")
    if rep == "once":
        d = spec.get("date")
        if not d:
            return None
        try:
            y, mo, da = (int(x) for x in str(d).split("-"))
            cand = datetime(y, mo, da, h, m)
        except (ValueError, TypeError):
            return None
        return cand.timestamp() if cand > after else None

    if rep == "weekly":
        days = spec.get("weekdays") or [0]
        for delta in range(0, 15):
            day = (after + timedelta(days=delta))
            if day.weekday() in days:
                cand = day.replace(hour=h, minute=m, second=0, microsecond=0)
                if cand > after:
                    return cand.timestamp()
        return None

    # daily
    cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= after:
        cand += timedelta(days=1)
    return cand.timestamp()


# ---------------- 展示 ----------------

def describe(t):
    rep = t.get("repeat")
    if rep == "interval":
        n = int(t.get("every_minutes") or 0)
        when = f"每{n}分钟" if n < 60 else f"每{n // 60}小时"
    elif rep == "weekly":
        wd = "".join(_WEEKDAY_CN[int(i)] for i in (t.get("weekdays") or []) if 0 <= int(i) <= 6)
        when = f"每周{wd} {t.get('at', '')}"
    elif rep == "once":
        when = f"{t.get('date', '')} {t.get('at', '')}"
    else:
        when = f"每天 {t.get('at', '')}"

    what = "打电话" if t.get("action") == "call" else "发消息"
    body = f"「{t.get('text', '')[:20]}」" if t.get("action") != "call" else ""
    nx = t.get("next_ts")
    nxt = ""
    if nx and t.get("enabled", True):
        nxt = "，下次 " + datetime.fromtimestamp(float(nx)).strftime("%m-%d %H:%M")
    flag = "" if t.get("enabled", True) else "（已暂停）"
    return (f"[{t.get('id')}] {when} {what}给 {t.get('to_name') or t.get('to')}"
            f"{body}{nxt}{flag}")


def status_text(cfg):
    recs = tasks(cfg)
    head = f"定时任务：{'开启' if enabled(cfg) else '已关闭'}"
    if not recs:
        return head + "\n还没有任务。\n\n" + _USAGE
    lines = [head, ""]
    lines += ["  " + describe(t) for t in recs]
    lines += ["", "改完发 /定时 看最新状态。"]
    return "\n".join(lines)


def summary_line(cfg):
    recs = tasks(cfg)
    if not recs:
        return "定时：无"
    n = sum(1 for t in recs if t.get("enabled", True))
    return f"定时：{n}/{len(recs)} 个开启" + ("" if enabled(cfg) else "（总开关关着）")


# ---------------- 执行 ----------------

def run_due(cfg, now, send_text, notify=None, call=None):
    """跑一遍到点的任务。**在主循环那次 tick 里调用**（单线程）。

    send_text(wxid, text)  发文本
    notify(text)           把结果发到控制会话（可选）
    call(wxid, name)       发起语音通话；返回 None 表示成功，返回字符串表示失败原因。
                           没给 call 就说明还没打通，如实报错、**不降级成发文本**。
    返回本次触发的任务 id 列表。
    """
    if not enabled(cfg):
        return []
    now = now or datetime.now()
    now_ts = now.timestamp()
    recs = tasks(cfg)
    fired = []
    dirty = False

    for t in recs:
        if not t.get("enabled", True):
            continue
        nx = t.get("next_ts")
        if nx is None or float(nx) > now_ts:
            continue

        tid = str(t.get("id"))
        name = t.get("to_name") or t.get("to")
        try:
            if t.get("action") == "call":
                # 语音通话的发送路径还没打通（见记忆里的逆向记录）。
                # 这里**必须报错**，不能悄悄改成发文本——那会骗用户。
                if call is None:
                    err = "语音通话的发送功能还没做出来（本机逆向没打通发送路径）"
                else:
                    err = call(t.get("to"), name)
                if err and notify:
                    notify(f"⏰ 定时任务 [{tid}] 到点了：本来要给 {name} 打电话，"
                           f"但没有执行——{err}")
            else:
                send_text(t.get("to"), t.get("text") or "")
                if notify:
                    notify(f"⏰ 定时任务 [{tid}]：已给 {name} 发出「{(t.get('text') or '')[:30]}」")
        except Exception as e:  # 一条任务炸了不能拖垮主循环
            if notify:
                notify(f"⏰ 定时任务 [{tid}] 执行失败：{e}")

        # 记一次触发时间，再算下次
        t["last_ts"] = now_ts
        nxt = _next_after(t, now)
        if nxt is None:
            # 一次性任务：跑完就停，但**不删**——留着让用户看得到
            t["next_ts"] = None
            t["enabled"] = False
        else:
            t["next_ts"] = nxt
        fired.append(tid)
        dirty = True

    if dirty:
        _save(tasks=recs)
    return fired


# ---------------- 命令 / 工具 ----------------

def build_arg(action, when="", who="", text="", target="", mode="call"):
    """把 agent 工具的结构化参数拼成 /定时 的子命令串。

    和 auto_reply.build_arg 同一个套路：工具和命令走**同一条**实现。
    """
    a = str(action or "").strip().lower()
    if a in ("list", "status", "列表", ""):
        return ""
    if a in ("add", "加", "添加"):
        head = "addcall" if str(mode or "").lower() in ("call", "通话", "电话") else "add"
        return " ".join(x for x in (head, when, who, text) if str(x).strip())
    if a in ("del", "delete", "删", "删除"):
        return f"del {target or who}".strip()
    if a in ("on", "开", "off", "关"):
        return f"{'on' if a in ('on', '开') else 'off'} {target or who}".strip()
    return a


def handle_command(arg, cfg, resolve, can_lookup=True, name_hint=None):
    """处理 /定时 系列子命令。返回 (回复文本, 是否改了配置)。

    resolve(who) -> (候选人 dict, 错误文本)，由调用方提供
    （bot 用联系人快照、agent 用 ToolBox._one，重名时都会要求用户说清楚）。
    """
    parts = str(arg or "").split(maxsplit=1)
    sub = parts[0].strip().lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    recs = tasks(cfg)

    if not sub or sub in ("status", "list", "状态", "列表"):
        return status_text(cfg), False

    if sub in ("on", "开", "开启"):
        _save(enabled=True if not rest else section(cfg).get("enabled", True),
              tasks=_toggle(recs, rest, True) or recs)
        if not rest:
            return "定时总开关已打开。", True
        return f"已恢复：{_touched(recs, rest)}", True

    if sub in ("off", "关", "关闭"):
        if not rest:
            _save(enabled=False, tasks=recs)
            return "定时总开关已关闭，所有任务都不会触发。", True
        return f"已暂停：{_touched(_toggle(recs, rest, False) or recs, rest)}", True

    if sub in ("del", "delete", "删", "删除"):
        if not rest:
            return "用法：/定时 删 <编号>（编号见 /定时）", False
        keep = [t for t in recs if str(t.get("id")) != rest]
        if len(keep) == len(recs):
            return f"没有编号 {rest} 的任务。发 /定时 看列表。", False
        _save(tasks=keep)
        return f"已删除任务 {rest}。", True

    if sub in ("add", "加", "添加", "addcall", "加通话", "加电话"):
        want_call = sub in ("addcall", "加通话", "加电话")
        # 时间可能是两个词（「明天 9:00」「2026-10-02 9:00」），先按前缀吃掉
        bits = rest.split()
        if not bits:
            return _USAGE, False
        take = 2 if bits[0].lower() in ("今天", "明天", "后天", "每天", "每日") else 1
        if take == 1 and bits[0].startswith(("每周", "每星期")):
            take = 2
        when = " ".join(bits[:take])
        tail = bits[take:]
        try:
            spec = parse_when(when)
        except ValueError as e:
            return str(e), False

        if want_call:
            who = " ".join(tail).strip()
            text = ""
        else:
            if not tail:
                return _USAGE, False
            who = tail[0]
            text = " ".join(tail[1:]).strip()
            if not text:
                return "要发的内容不能空。用法：/定时 加 <时间> <对象> <内容>", False

        cand, err = resolve(who)
        if err:
            return err, False
        wxid = str(cand.get("wxid"))
        disp = str(name_hint or "").strip() or (cand.get("remark") or cand.get("name")
                                               or wxid)
        if not can_lookup and not wxid:
            return "当前查不到联系人，请直接填 wxid。", False

        task = {"id": _next_id(recs), "action": "call" if want_call else "text",
                "to": wxid, "to_name": disp, "text": text, "enabled": True,
                "last_ts": None}
        task.update(spec)
        task["next_ts"] = initial_next(task)
        recs.append(task)
        _save(tasks=recs, enabled=True)
        tail_msg = ("" if enabled(cfg) else
                    "\n（定时总开关是关着的，发 /定时 开 才会生效）")
        warn = ("\n\n⚠️ 语音通话的发送路径还没打通，到点只会给你报错，不会真打出去。"
                if want_call else "")
        return (f"已加定时任务 [{task['id']}]：{describe(task)}\n"
                f"改完发 /定时 看列表。{tail_msg}{warn}"), True

    return _USAGE, False


def _toggle(recs, which, on):
    if not which or which.lower() == "all":
        for t in recs:
            t["enabled"] = on
            if on and t.get("next_ts") is None:
                t["next_ts"] = initial_next(t)
        return recs
    for t in recs:
        if str(t.get("id")) == which:
            t["enabled"] = on
            if on and t.get("next_ts") is None:
                t["next_ts"] = initial_next(t)
    return recs


def _touched(recs, which):
    if not which or which.lower() == "all":
        return "全部任务"
    return "、".join(str(t.get("id")) for t in recs if str(t.get("id")) == which) or which


if __name__ == "__main__":
    # 纯逻辑自测：不碰微信、不碰 hook。跑： .venv/Scripts/python.exe scheduler.py
    from datetime import datetime as D
    base = D(2026, 10, 1, 8, 0, 0)   # 2026-10-01 是周四

    def chk(cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            raise SystemExit(1)

    print("parse_when:")
    chk(parse_when("9:00") == {"repeat": "daily", "at": "09:00"}, "9:00 → 每天")
    chk(parse_when("每天9:00")["repeat"] == "daily", "每天9:00")
    chk(parse_when("明天9:00", base)["date"] == "2026-10-02", "明天9:00 → 10-02")
    chk(parse_when("今天 9:30", base)["at"] == "09:30", "今天 9:30")
    chk(parse_when("每周一 9:00")["weekdays"] == [0], "每周一 → 0")
    chk(parse_when("每周日 9:00")["weekdays"] == [6], "每周日 → 6")
    chk(parse_when("2026-12-25 8:00")["date"] == "2026-12-25", "绝对日期")
    chk(parse_when("每30分钟") == {"repeat": "interval", "every_minutes": 30}, "每30分钟")
    chk(parse_when("每2小时")["every_minutes"] == 120, "每2小时")
    chk(parse_when("9：00")["at"] == "09:00", "全角冒号")
    for bad in ("", "25:00", "每x分钟", "每周八 9:00"):
        try:
            parse_when(bad)
            chk(False, f"「{bad}」应该报错")
        except ValueError:
            chk(True, f"「{bad}」正确报错")

    print("下次触发时间:")
    d = {"repeat": "daily", "at": "09:00"}
    chk(D.fromtimestamp(_next_after(d, base)).strftime("%m-%d %H:%M") == "10-01 09:00",
        "每天 9:00，8 点时算 → 今天 9:00")
    chk(D.fromtimestamp(_next_after(d, D(2026, 10, 1, 9, 30))).strftime("%m-%d %H:%M") == "10-02 09:00",
        "已过 9:00 → 明天")
    w = {"repeat": "weekly", "weekdays": [0], "at": "09:00"}
    chk(D.fromtimestamp(_next_after(w, base)).strftime("%m-%d %a") == "10-05 Mon",
        "每周一，周四算 → 下周一")
    o = {"repeat": "once", "date": "2026-10-01", "at": "07:00"}
    chk(_next_after(o, base) is None, "一次性且已过 → None（不再触发）")
    o2 = {"repeat": "once", "date": "2026-10-02", "at": "07:00"}
    chk(_next_after(o2, base) is not None, "一次性未到 → 有下次")

    print("执行 run_due:")
    sent = []
    notes = []
    cfg = {"schedule": {"enabled": True, "tasks": [
        {"id": "t1", "action": "text", "to": "wxid_a", "to_name": "张三",
         "text": "记得带伞", "repeat": "daily", "at": "08:30", "enabled": True,
         "next_ts": base.timestamp() - 1, "last_ts": None},
        {"id": "t2", "action": "call", "to": "wxid_b", "to_name": "李四",
         "text": "", "repeat": "once", "date": "2026-10-01", "at": "08:00",
         "enabled": True, "next_ts": base.timestamp() - 1, "last_ts": None},
        {"id": "t3", "action": "text", "to": "wxid_c", "to_name": "王五",
         "text": "还没到", "repeat": "daily", "at": "23:00", "enabled": True,
         "next_ts": base.timestamp() + 9999, "last_ts": None},
    ]}}
    # 把 _save 挡掉，自测不写 settings.json
    _real_save = _save
    globals()["_save"] = lambda **kw: None
    try:
        fired = run_due(cfg, base, lambda to, tx: sent.append((to, tx)),
                        notify=notes.append)
    finally:
        globals()["_save"] = _real_save
    chk(fired == ["t1", "t2"], f"触发 t1/t2，没触发 t3（实际 {fired}）")
    chk(sent == [("wxid_a", "记得带伞")], "文本任务真发了")
    chk(all("李四" in n and "没有执行" in n for n in notes if "李四" in n),
        "通话任务**如实报错**，没有偷偷改成发文本")
    chk(len(sent) == 1, "通话任务没有发出任何文本")
    t1 = cfg["schedule"]["tasks"][0]
    chk(t1["next_ts"] > base.timestamp(), "重复任务已排下次")
    t2 = cfg["schedule"]["tasks"][1]
    chk(t2["enabled"] is False and t2["next_ts"] is None, "一次性任务跑完自动停")

    print("命令层 handle_command:")
    contacts = [{"wxid": "wxid_z", "name": "张三", "remark": "张三"},
                {"wxid": "wxid_zf", "name": "张三丰", "remark": "张三丰"},
                {"wxid": "wxid_l", "name": "李四", "remark": "李四"}]

    def resolve(who):
        # 精确匹配优先（跟 agent_tools.resolve_contacts 的语义一致）
        hits = [c for c in contacts if who in (c["wxid"], c["name"], c["remark"])]
        if not hits:
            hits = [c for c in contacts if who and who in (c["name"] or "")]
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"没找到「{who}」。"
        return None, f"「{who}」匹配到多个人，请用全名或直接给 wxid。"

    saved = {}
    c2 = {"schedule": {"enabled": True, "tasks": []}}

    def _fake_save(**kw):
        # 模拟「命令写盘 + 主循环 reload_cfg」这一圈：生产里 cfg 是靠
        # reload_cfg 重新读 settings.json 才拿到新任务的。
        saved.update(kw)
        if "tasks" in kw:
            c2["schedule"]["tasks"] = kw["tasks"]
        if "enabled" in kw:
            c2["schedule"]["enabled"] = kw["enabled"]

    try:
        globals()["_save"] = _fake_save
        out, ch = handle_command("加 明天9:00 张三 记得带伞", c2, resolve)
        chk(ch and "已加定时任务" in out, "「加」建出任务")
        tk = saved.get("tasks", [{}])[-1]
        chk(tk.get("to") == "wxid_z" and tk.get("text") == "记得带伞"
            and tk.get("repeat") == "once" and tk.get("date") == "2026-10-02",
            f"任务字段正确（实际 to={tk.get('to')} date={tk.get('date')}）")
        chk(tk.get("next_ts") and tk["next_ts"] > 0, "算出了 next_ts")

        out, ch = handle_command("加 明天9:00 张 你好", c2, resolve)
        chk(not ch and "匹配到多个人" in out, "重名**不静默取第一个**")

        out, ch = handle_command("加 明天25:00 李四 你好", c2, resolve)
        chk(not ch and "0~23" in out, "时间写错当场拒绝")

        out, ch = handle_command("加通话 明天9:00 李四", c2, resolve)
        chk(ch and "还没打通" in out, "建通话任务时明确警告没打通")

        out, ch = handle_command("加 每30分钟 李四 打卡", c2, resolve)
        chk(ch and saved.get("tasks", [{}])[-1].get("every_minutes") == 30,
            "间隔式任务")

        out, ch = handle_command("删 t1", c2, resolve)
        chk(ch and "已删除" in out, "按编号删")
        out, ch = handle_command("删 t99", c2, resolve)
        chk(not ch and "没有编号" in out, "删不存在的编号要报错")

        out, ch = handle_command("", c2, resolve)
        chk("定时任务" in out, "不带参数 = 看列表")
    finally:
        globals()["_save"] = _real_save

    print("\n全部通过。")
