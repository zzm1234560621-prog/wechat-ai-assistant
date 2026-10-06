"""定时任务：到点给某人发消息 / 到点提醒我 / 到点让助手答一个问题。

**为什么不做成后台线程**：hook 不支持并发（并发调用会把微信搞崩，见 CLAUDE.md），
而轮询主循环本身是单线程的。所以定时器只在主循环**那一次 tick** 里跑，
天然和收消息串行——绝不会出现「定时任务正在发消息、同时又在轮询」的情况。

任务存在 settings.json 的 schedule 段（和 auto_reply 一样由命令维护），
config.yaml 里给默认值。字段：

    id         短标识，命令里用（t1 / t2 …）
    action     text=给某人发文本；remind=到点提醒我（发到控制会话）；
               ask=到点把 text 当提问跑一遍助手，答案回控制会话；
               call=发起语音通话（**目前发不出去**，见下面 run_due）
    to         对方 wxid（创建时就解析好并落盘，tick 里不再查库；remind/ask 为空）
    to_name    显示名，只用于回显
    text       发的内容（text=要发的话；remind=提醒的话；ask=要问的话）
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
    "  /schedule —— 看列表\n"
    "  /schedule add <时间> <对象> <内容> —— 加一个发文本的\n"
    "  /schedule remind <时间> <内容> —— 到点**提醒我**（发回本会话），不用填对象\n"
    "  /schedule ask <时间> <问题> —— 到点让助手答这个问题，答案发回本会话\n"
    "  /schedule del <编号> —— 删掉\n"
    "  /schedule on|off —— 总开关（只有这两个子命令，不能带编号）\n"
    "时间写法：9:00 是每天，明天9:00 是只一次，"
    "10-02 9:00 也是只一次，每周一 9:00 是每周，每30分钟 是每隔一段，"
    "10分钟后 / 10分钟之后 / 半小时后 / 2小时后 / 3天后 是只一次（从**现在**起算）。\n"
    "例：/schedule add 明天9:00 张三 记得带伞\n"
    "    /schedule remind 10分钟之后 喝水\n"
    "    /schedule ask 每天8:00 整理一下谁还没回我、昨天有什么漏的\n"
    "（中文子命令也还能用：/定时 加 / 加提醒 / 加提问 / 删 / 开|关）"
)



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
    """挑一个没用过的编号。

    为什么不用「第一个空位」（以前是 t1 空着就发 t1）：编号被删掉后**复用**的话，
    「删第 2 个」这种说法就有歧义了——你刚看到的是 [t2]，删完再发命令，
    那个 t2 已经是另一个任务了，很容易删错人、改错人。
    所以从现有最大编号往后接着排，编号只增不复用。
    """
    used = []
    for r in recs:
        m = re.fullmatch(r"t(\d+)", str(r.get("id") or ""))
        if m:
            used.append(int(m.group(1)))
    n = (max(used) + 1) if used else 1
    # 还要躲开**本次运行里删掉过的**编号：删除和新增在同一个 tick 里发生时
    # （定时任务动作里跑 agent 加/删任务），只看盘上现存的列表会以为 t2 空着，
    # 于是新任务又叫 t2 —— 旧 t2 可能还挂在别处（比如本轮结算的合并表里），
    # 编号一撞，「删 t2」就删错东西。记性只保留本次运行，不整份写盘。
    while f"t{n}" in _RETIRED_IDS:
        n += 1
    return f"t{n}"


# 本次运行里被删掉的编号（只为不复用，不落盘）。坏/极端情况下也不会无限涨。
_RETIRED_IDS = []
_RETIRED_MAX = 500


def _retire_id(tid):
    tid = str(tid or "")
    if tid and tid not in _RETIRED_IDS:
        _RETIRED_IDS.append(tid)
        if len(_RETIRED_IDS) > _RETIRED_MAX:
            del _RETIRED_IDS[0]



def _norm(s):
    """全角冒号/空格归一，省得用户输入法不同就解析不了。"""
    return (str(s or "").strip()
            .replace("：", ":").replace("　", " ")
            .replace("点钟", ":").replace("点", ":").strip())


def _hhmm(s):
    """解析 "9:00" / "09:00" / "9"（只有小时）/ "9点半"。返回 (h, m)。"""
    s = str(s or "").strip().rstrip("分").strip()
    try:
        if ":" in s:
            a, b = s.split(":", 1)
            b = b.strip()
            h = int(a)
            # 「9点半」会被 _norm 归一化成「9:半」
            m = 30 if b == "半" else int(b or 0)
        else:
            h, m = int(s), 0
    except ValueError:
        raise ValueError(f"时间「{s}」没看懂。例：9:00、09:30、9点半")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间「{s}」不对：小时要 0~23、分钟要 0~59。")
    return h, m


# 中文数字：「十分钟后」「半小时后」「两小时后」都得认
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 相对**一次性**：「10分钟后」「半个小时后」「2小时后」「3天后」「过20分钟后」
# ⚠️ 和上面「每N分钟」区分：那是**重复**规则，这一支只触发一次。
_REL_RE = re.compile(
    r"^(?:(?:过|再)\s*){0,2}(\d+|[一二两三四五六七八九十]+|半)\s*个?\s*"
    r"(分钟|分|小时|钟头|天)\s*(?:后|以后|之后)$")


def _rel_minutes(num_s, unit):
    """把「10 分钟 / 半 小时 / 2 天」换算成分钟数；看不懂抛 ValueError。"""
    half = num_s == "半"
    if half:
        if unit in ("分钟", "分"):
            raise ValueError("「半分钟后」太短了（调度精度是一分钟），写成「1分钟后」。")
        n = 1
    elif num_s.isdigit():
        n = int(num_s)
    else:
        n = _CN_NUM.get(num_s)
        if n is None:
            raise ValueError(f"时间里的数字「{num_s}」没看懂。例：10分钟后、半小时后")
    if n <= 0:
        raise ValueError("时间要是正数。")
    if unit in ("分钟", "分"):
        return n
    if unit in ("小时", "钟头"):
        return n * 30 if half else n * 60
    return n * 720 if half else n * 24 * 60      # 天


def parse_when(when, now=None):
    """把「时间写法」解析成任务字段。失败抛 ValueError（消息直接给用户看）。

    关键字和时间**允许连写**（「明天9:00」「每周一9:00」），所以用正则从头上吃。
    """
    now = now or datetime.now()
    s = _norm(when)
    if not s:
        raise ValueError("要写时间。例：明天9:00 / 9:00 / 每天8:00 / 每周一 9:00 / "
                         "10分钟后 / 每30分钟")

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

    # 相对一次性：「10分钟后」「半小时后」「2小时后」「3天后」
    # 以前没有这一支，用户说「10分钟后」会掉到最下面的 _hhmm() 兜底，
    # 报「时间「10分钟后」没看懂」——**是缺分支，不是有意拒绝**。
    # 实现方式：换算成绝对时刻，复用现成的 once（date + at）表示法，
    # 这样 next_ts 照旧是墙上时钟、落盘后重启也不会漂。
    m = _REL_RE.match(s)
    if m:
        mins = _rel_minutes(m.group(1), m.group(2))
        target = now + timedelta(minutes=mins)
        # 调度精度只到分钟，**向上取整**：宁可晚十几秒，也绝不比用户说的更早触发
        if target.second or target.microsecond:
            target = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return {"repeat": "once", "date": target.date().isoformat(),
                "at": f"{target.hour:02d}:{target.minute:02d}"}

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

    act = t.get("action")
    body = f"「{(t.get('text') or '')[:24]}」"
    if act == "call":
        what = f"打电话给 {t.get('to_name') or t.get('to')}"
    elif act == "remind":
        # 提醒我是发到控制会话的，没有「发给谁」这回事
        what = f"提醒你：{body}"
    elif act == "ask":
        # 提问式的答案是回控制会话的，没有「发给谁」这回事
        what = f"问你：{body}"
    else:
        what = f"发消息给 {t.get('to_name') or t.get('to')} {body}"

    nx = t.get("next_ts")
    nxt = ""
    if nx and t.get("enabled", True):
        nxt = "，下次 " + datetime.fromtimestamp(float(nx)).strftime("%m-%d %H:%M")
    flag = "" if t.get("enabled", True) else "（已暂停）"
    return f"[{t.get('id')}] {when} {what}{nxt}{flag}"


def status_text(cfg):
    recs = tasks(cfg)
    head = f"定时任务：{'开启' if enabled(cfg) else '已关闭'}"
    if not recs:
        return head + "\n还没有任务。\n\n" + _USAGE
    lines = [head, ""]
    lines += ["  " + describe(t) for t in recs]
    lines += ["", "改完发 /schedule 看最新状态。"]
    return "\n".join(lines)


def summary_line(cfg):
    recs = tasks(cfg)
    if not recs:
        return "定时：无"
    n = sum(1 for t in recs if t.get("enabled", True))
    return f"定时：{n}/{len(recs)} 个开启" + ("" if enabled(cfg) else "（总开关关着）")



def run_due(cfg, now, send_text, notify=None, call=None, ask=None):
    """跑一遍到点的任务。**在主循环那次 tick 里调用**（单线程）。

    send_text(wxid, text)  发文本
    notify(text)           把一段话发到控制会话（提醒我、以及各种告警都走它）
    call(wxid, name)       发起语音通话；返回 None 表示成功，返回字符串表示失败原因。
                           没给 call 就说明还没打通，如实报错、**不降级成发文本**。
    ask(prompt)            把这句话当提问跑一次助手，返回答复（失败就抛异常）。
                           答案回控制会话——「定时给我一份整理」用的就是这条。
    返回本次触发的任务 id 列表。
    """
    if not enabled(cfg):
        return []
    now = now or datetime.now()
    now_ts = now.timestamp()
    recs = tasks(cfg)

    def _warn(text):
        """给用户报错。notify 自己炸了也不能拖垮这一轮。"""
        if notify:
            try:
                notify(text)
            except Exception as e:
                print(f"[定时] ⚠️ 告警发不出去：{e}")

    fire = []
    touched = {}
    for t in recs:
        if not t.get("enabled", True):
            continue
        nx = t.get("next_ts")
        try:
            if nx is None or float(nx) > now_ts:
                continue
        except (TypeError, ValueError):
            # next_ts 是坏的（比如手工编辑 settings.json 写成了 "明天"）。
            # 只跳过这一条——不能让它把整轮都带走（见下面的 _next_after）。
            _bad_task(t, f"next_ts 不是数字（{nx!r}），这条没法算下次时间", _warn)
            continue
        try:
            # 注意：**先算 next 再落字段**。_next_after 会解析 at / 日期，
            # 数据坏掉时在这里抛异常——那时 last_ts/next_ts 还没被改，
            # 这条任务就停在原地，等用户修好数据下次还能正常触发。
            nxt = _next_after(t, now)
        except Exception as e:
            # **一条坏任务不许停摆整轮**。以前这里没兜住：_hhmm 抛 ValueError，
            # 整轮一个任务都不排、不执行、不写盘，bot 只 print_exc，
            # 用户那边毫无提示，而且 next_ts 永远留在过去，每 5 秒重犯一次。
            _bad_task(t, f"时间数据有问题：{e}", _warn)
            continue
        t["last_ts"] = now_ts
        if nxt is None:
            # 一次性任务：跑完就停，但**不删**——留着让用户看得到
            t["next_ts"] = None
            t["enabled"] = False
        else:
            t["next_ts"] = nxt
        # 记下「这一轮改了哪几条任务的什么字段」，执行完之后照这个合并回盘上。
        touched[str(t.get("id"))] = {"next_ts": t["next_ts"],
                                     "last_ts": t["last_ts"],
                                     "enabled": t.get("enabled", True)}
        fire.append(t)

    fired = []
    for t in fire:
        tid = str(t.get("id"))
        name = t.get("to_name") or t.get("to")
        try:
            act = t.get("action")
            if act == "remind":
                # 提醒我：把这句话**原样**发到控制会话（notify）。
                # 不跑模型——让模型复述一遍，提醒内容就可能走样；这里要的是
                # 「到点把用户自己写的那句话还给他」。
                _warn(f"⏰ 提醒：{t.get('text') or ''}")
            elif act == "ask":
                if ask is None:
                    _warn(f"⏰ 定时任务 [{tid}] 到点了，但没法执行——"
                          f"主循环没提供 ask 回调。")
                else:
                    answer = ask(t.get("text") or "")
                    _warn(answer)
            elif act == "call":
                # 语音通话的发送路径还没打通（见记忆里的逆向记录）。
                # 这里**必须报错**，不能悄悄改成发文本——那会骗用户。
                if call is None:
                    err = "语音通话的发送功能还没做出来（本机逆向没打通发送路径）"
                else:
                    err = call(t.get("to"), name)
                if err:
                    _warn(f"⏰ 定时任务 [{tid}] 到点了：本来要给 {name} 打电话，"
                          f"但没有执行——{err}")
            else:
                send_text(t.get("to"), t.get("text") or "")
                _warn(f"⏰ 定时任务 [{tid}]：已给 {name} 发出「{(t.get('text') or '')[:30]}」")
        except Exception as e:  # 一条任务炸了不能拖垮主循环
            _warn(f"⏰ 定时任务 [{tid}] 执行失败：{e}")
        fired.append(tid)

    # **最后**才写盘，而且只把本轮真正改过的那几条任务的字段并回盘上。
    #
    # 为什么不能像以前那样 `_save(tasks=recs)`：recs 是 tick 一开始的快照，
    # 而动作（ask 那条会跑一整轮 agent）中间可能加/删任务，那些改动已经落盘了。
    # 拿旧列表整份盖回去 = 动作里新增的任务丢了、删掉的复活。现在以**盘上的
    # 最新列表**为基准，只覆盖我们自己算出来的 next_ts/last_ts/enabled：
    #   * 动作新增的任务：盘上有、我们没碰它的字段 → 原样保留（不丢）；
    #   * 动作删掉的任务：盘上已经没有 → 我们根本不会把它写回去（不复活）；
    #   * 本轮触发的任务：字段照着合并进去，不会在下一 tick 又触发一遍。
    # 同时上面的改动是**就地改 recs**（cfg 里那份），主循环即使不 reload_cfg
    # 也不会重复触发——这条和以前一致，别改成只写盘。
    if touched:
        _merge_save(touched)
    return fired


# 坏任务告警节流：轮询间隔默认 5 秒，不节流的话同一条坏数据会每 5 秒刷一条。
# 只在「距上次告警够久」或「错误内容变了」时再报——同一件事不重复刷屏，
# 但用户修好之前也不会彻底静默（否则又是「静默失效」）。
_BAD_WARN_GAP = 600.0
_bad_warned = {}


def _bad_task(t, reason, warn):
    tid = str(t.get("id"))
    now = time.time()
    prev = _bad_warned.get(tid)
    if prev and prev[0] == reason and now - prev[1] < _BAD_WARN_GAP:
        return
    try:
        warn(f"⏰ 定时任务 [{tid}] 数据有问题，本轮**只跳过它自己**，"
             f"其他任务照常：{reason}。发 /schedule del {tid} 可删掉，或 /schedule 看列表核对。")
    except Exception:
        # warn 不该抛，真抛了就算了：不能因为「报警失败」把调度搞停
        pass
    _bad_warned[tid] = (reason, now)


def _merge_save(touched):
    """把本轮改动合并进盘上的 tasks（见 run_due 末尾的说明）。"""
    try:
        base = settings.load().get("schedule")
        on_disk = tasks({"schedule": base if isinstance(base, dict) else {}})
    except Exception:
        on_disk = []
    if not on_disk:
        # 盘上读不出来（或本来就是空的）就别猜，宁可少写一次也不能拿旧列表盖盘的。
        return
    for t in on_disk:
        upd = touched.get(str(t.get("id")))
        if upd:
            t.update(upd)
    _save(tasks=on_disk)



# 「提醒我」的几种自称。出现在 `<对象>` 位置上时，目标是**控制会话**（自己），
# 不是一个叫「我」的联系人 —— 去查联系人只会得到一句「没找到「我」」。
_SELF_WORDS = ("我本人", "我自己", "自个儿", "自己", "本人", "俺", "我")


def _is_self(who):
    return _norm(who) in _SELF_WORDS


def _drop_self_token(words):
    """去掉「<对象> 位置上那个**整词**自称」，返回剩下的正文。

    ⚠️ **只在词这一级判断。** 以前这里是按「正文开头有没有『我』这个字」削的
    （`_strip_self_lead`），真机自测抓出来：`/定时 加提醒 2分钟之后 我是部署自检…`
    被削成「是部署自检…」—— **用户自己写的话被吃掉一个字**。
    自称只有**单独一个词**站在对象位时才算对象，正文里的「我」一个字都不许动。
    """
    ws = list(words)
    if ws and _is_self(ws[0]):
        ws = ws[1:]
    return " ".join(ws).strip()


def build_arg(action, when="", who="", text="", target="", mode="text"):
    """把 agent 工具的结构化参数拼成 /定时 的子命令串。

    和 auto_reply.build_arg 同一个套路：工具和命令走**同一条**实现。
    """
    a = str(action or "").strip().lower()
    if a in ("list", "status", "列表", ""):
        return ""
    if a in ("add", "加", "添加"):
        m = str(mode or "").lower()
        if m in ("call", "通话", "电话"):
            head = "addcall"
        elif m in ("remind", "提醒"):
            # 提醒我：没有「发给谁」，who 位置的东西（往往是「我」）丢掉，
            # 否则它会变成提醒正文的一部分。
            head, who = "addremind", ""
        elif m in ("ask", "提问"):
            head = "ask"
        else:
            head = "add"
        return " ".join(x for x in (head, when, who, text) if str(x).strip())
    if a in ("del", "delete", "删", "删除"):
        return f"del {target or who}".strip()
    if a in ("on", "开", "off", "关"):
        return f"{'on' if a in ('on', '开') else 'off'} {target or who}".strip()
    return a


def handle_command(arg, cfg, resolve, can_lookup=True, name_hint=None, now=None):
    """处理 /定时 系列子命令。返回 (回复文本, 是否改了配置)。

    resolve(who) -> (候选人 dict, 错误文本)，由调用方提供
    （bot 用联系人快照、agent 用 ToolBox._one，重名时都会要求用户说清楚）。

    `now` 只为**自测**能固定「明天 / 10分钟之后」而存在，生产不传。
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
            return ("定时总开关已关闭，所有任务都不会触发。"
                    "（再发 /schedule 开 恢复时，每个任务会按各自时间**重新排下一次**，"
                    "不会把暂停期间漏掉的补发出去。）"), True
        return f"已暂停：{_touched(_toggle(recs, rest, False) or recs, rest)}", True

    if sub in ("del", "delete", "删", "删除"):
        if not rest:
            return "用法：/schedule del <编号>（编号见 /schedule）", False
        keep = [t for t in recs if str(t.get("id")) != rest]
        if len(keep) == len(recs):
            return f"没有编号 {rest} 的任务。发 /schedule 看列表。", False
        _save(tasks=keep)
        _retire_id(rest)      # 本次运行内不再把 rest 发给新任务（编号撞了就删错人）
        return f"已删除任务 {rest}。", True

    if sub in ("add", "加", "添加", "addcall", "加通话", "加电话",
               "addremind", "加提醒", "提醒", "remind", "加提问", "ask"):
        want_call = sub in ("addcall", "加通话", "加电话")
        want_remind = sub in ("addremind", "加提醒", "提醒", "remind")
        want_ask = sub in ("加提问", "ask")
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
            spec = parse_when(when, now)
        except ValueError as e:
            return str(e), False

        if want_call:
            who = " ".join(tail).strip()
            text = ""
        elif want_remind:
            # 提醒我：整段剩下的话就是提醒内容。只把**对象位上那个整词自称**去掉
            #（「加提醒 10分钟之后 我 喝水」），正文里的字一个都不动。
            text = _drop_self_token(tail)
            who = ""
            if not text:
                return "要说清楚提醒什么。例：/schedule remind 10分钟之后 喝水", False
        elif want_ask:
            # 提问式：整段剩下的话就是问题，不用解析对象（答案回控制会话）
            text = " ".join(tail).strip()
            who = ""
            if not text:
                return "要说清楚问什么。例：/schedule ask 每天8:00 整理谁还没回我", False
        else:
            if not tail:
                return _USAGE, False
            who = tail[0]
            text = " ".join(tail[1:]).strip()
            if _is_self(who):
                # 「10分钟之后提醒我喝水」走的就是这条路：<对象> 位置写的是「我」，
                # 意思是**提醒我自己**（发到控制会话），不是去找一个叫「我」的人。
                # 注意正文**就是从 tail[1:] 来的**，那个「我」已经被当成对象吃掉了，
                # 所以**不要**再去削正文开头（否则「我是自检」会被削成「是自检」）。
                want_remind = True
                who = ""
            if not text:
                return f"要发的内容不能空。{_USAGE}", False

        wxid = disp = ""
        if not want_ask and not want_remind:
            cand, err = resolve(who)
            if err:
                return err, False
            wxid = str(cand.get("wxid"))
            disp = str(name_hint or "").strip() or (cand.get("remark") or cand.get("name")
                                                   or wxid)
            if not can_lookup and not wxid:
                return "当前查不到联系人，请直接填 wxid。", False

        action = "call" if want_call else (
            "remind" if want_remind else ("ask" if want_ask else "text"))
        task = {"id": _next_id(recs), "action": action,
                "to": wxid, "to_name": disp, "text": text, "enabled": True,
                "last_ts": None}
        task.update(spec)
        task["next_ts"] = initial_next(task, now)
        recs.append(task)
        # **绝不在这里写 enabled=True**：总开关是用户自己的意图，加一个任务不该
        # 顺手把它打开。以前硬写 True，而下面文案又说「总开关是关着的」——
        # 落盘和文案自相矛盾：用户以为「加了不生效」，实际上总开关已被打开，
        # 下一次 tick 所有**本来就该跑**的任务会一起触发（含群发）。
        # 这里只写 tasks，总开关保持磁盘上的原状；文案也就跟着变成真话。
        _save(tasks=recs)
        tail_msg = ("" if enabled(cfg) else
                    "\n（定时总开关是关着的，这次只登记了任务，不会触发；"
                    "发 /schedule 开 才会生效）")
        warn = ("\n\n⚠️ 语音通话的发送路径还没打通，到点只会给你报错，不会真打出去。"
                if want_call else "")
        return (f"已加定时任务 [{task['id']}]：{describe(task)}\n"
                f"改完发 /schedule 看列表。{tail_msg}{warn}"), True

    return _USAGE, False


def _toggle(recs, which, on):
    def _rearm(t):
        """恢复一个任务时，重算一个**将来**的触发点。

        为什么要重算：暂停期间 next_ts 一直留在过去，恢复时若原样留着，
        下一次 tick（5 秒内）就会按那个过期时间**补跑一次**——用户只是「开回来」，
        却收到一条本该在暂停期间发的消息，群里就是一次没人想要的群发。
        """
        nx = t.get("next_ts")
        if on and (nx is None or float(nx) <= datetime.now().timestamp()):
            t["next_ts"] = initial_next(t)

    if not which or which.lower() == "all":
        for t in recs:
            t["enabled"] = on
            _rearm(t)
        return recs
    for t in recs:
        if str(t.get("id")) == which:
            t["enabled"] = on
            _rearm(t)
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
    # 相对现在的一次性：三种说法都要认（「之后」和「后」是一回事）
    for rel, want in (("10分钟后", "08:10"), ("10分钟之后", "08:10"),
                      ("半小时后", "08:30"), ("2小时以后", "10:00")):
        got = parse_when(rel, base)
        chk(got.get("repeat") == "once" and got.get("at") == want
            and got.get("date") == "2026-10-01",
            f"「{rel}」→ {want}（实际 {got}）")
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
        {"id": "t5", "action": "remind", "to": "", "to_name": "",
         "text": "喝水", "repeat": "once", "date": "2026-10-01", "at": "08:00",
         "enabled": True, "next_ts": base.timestamp() - 1, "last_ts": None},
        {"id": "t3", "action": "text", "to": "wxid_c", "to_name": "王五",
         "text": "还没到", "repeat": "daily", "at": "23:00", "enabled": True,
         "next_ts": base.timestamp() + 9999, "last_ts": None},
        {"id": "t4", "action": "ask", "to": "", "to_name": "", "text": "整理谁还没回我",
         "repeat": "daily", "at": "08:00", "enabled": True,
         "next_ts": base.timestamp() - 1, "last_ts": None},
    ]}}
    # 把 _save 挡掉，自测不写 settings.json
    _real_save = _save
    globals()["_save"] = lambda **kw: None
    asked = []

    def _ask(q):
        asked.append(q)
        return f"（整理结果：{q} → 3 人没回）"

    try:
        fired = run_due(cfg, base, lambda to, tx: sent.append((to, tx)),
                        notify=notes.append, ask=_ask)
    finally:
        globals()["_save"] = _real_save
    chk(fired == ["t1", "t2", "t5", "t4"],
        f"触发 t1/t2/t5/t4，没触发 t3（实际 {fired}）")
    chk(asked == ["整理谁还没回我"], "提问式任务把 text 当问题传下去了")
    chk(any("整理结果" in n for n in notes), "提问的答案回控制会话了")
    chk(sent == [("wxid_a", "记得带伞")], "文本任务真发了")
    chk(all("李四" in n and "没有执行" in n for n in notes if "李四" in n),
        "通话任务**如实报错**，没有偷偷改成发文本")
    chk(any("提醒：喝水" in n for n in notes), "提醒我：原文进了控制会话")
    chk(len(sent) == 1, "通话/提醒任务都没有发给任何联系人（提醒只进控制会话）")
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

        # 时间**注入 base**：不然「明天9:00」会跟着跑测试那天变，
        # 这个自测隔一天就红一次（以前就这样，只是没人在这个文件里跑它）。
        def _cmd(arg):
            return handle_command(arg, c2, resolve, now=base)

        out, ch = _cmd("加 明天9:00 张三 记得带伞")
        chk(ch and "已加定时任务" in out, "「加」建出任务")
        tk = saved.get("tasks", [{}])[-1]
        chk(tk.get("to") == "wxid_z" and tk.get("text") == "记得带伞"
            and tk.get("repeat") == "once" and tk.get("date") == "2026-10-02",
            f"任务字段正确（实际 to={tk.get('to')} date={tk.get('date')}）")
        chk(tk.get("next_ts") and tk["next_ts"] > 0, "算出了 next_ts")

        out, ch = _cmd("加 明天9:00 张 你好")
        chk(not ch and "匹配到多个人" in out, "重名**不静默取第一个**")

        out, ch = _cmd("加 明天25:00 李四 你好")
        chk(not ch and "0~23" in out, "时间写错当场拒绝")

        out, ch = _cmd("加通话 明天9:00 李四")
        chk(ch and "还没打通" in out,
            "建通话任务时明确警告没打通（这条命令的**描述**已从 TOOLS / 文档里拿掉，"
            "代码保留：老任务到点仍如实报错）")

        out, ch = _cmd("加提醒 明天9:00 喝水")
        tk = saved.get("tasks", [{}])[-1]
        chk(ch and tk.get("action") == "remind" and tk.get("text") == "喝水"
            and not tk.get("to"), f"加提醒：不用填对象（实际 {tk}）")

        # 回归（2026-10-04 真机自测抓出来的）：正文开头的「我」**不许**被当成对象削掉
        out, ch = _cmd("加提醒 明天9:00 我是自检：原文一个字都别动")
        tk = saved.get("tasks", [{}])[-1]
        chk(ch and tk.get("text") == "我是自检：原文一个字都别动",
            f"正文开头的「我」不许被削（实际 {tk.get('text')!r}）")
        out, ch = _cmd("加 明天9:00 我 我自己写的正文")
        tk = saved.get("tasks", [{}])[-1]
        chk(ch and tk.get("action") == "remind" and tk.get("text") == "我自己写的正文",
            f"对象位那个「我」被吃掉后，正文照原样（实际 {tk.get('text')!r}）")

        out, ch = _cmd("加 10分钟之后 我 吃药")
        tk = saved.get("tasks", [{}])[-1]
        chk(ch and tk.get("action") == "remind" and tk.get("text") == "吃药"
            and not tk.get("to"),
            f"「10分钟之后 我 吃药」= 提醒我自己（实际 action={tk.get('action')} "
            f"to={tk.get('to')!r} text={tk.get('text')!r}）")
        chk("提醒你" in out, "列表里把提醒显示成「提醒你…」")

        out, ch = _cmd("加 每30分钟 李四 打卡")
        chk(ch and saved.get("tasks", [{}])[-1].get("every_minutes") == 30,
            "间隔式任务")

        out, ch = _cmd("加提问 每天8:00 整理谁还没回我")
        tk = saved.get("tasks", [{}])[-1]
        chk(ch and tk.get("action") == "ask" and tk.get("text") == "整理谁还没回我"
            and not tk.get("to"), "提问式任务：不用填对象，问题原样存下")
        chk("问你" in out, "列表里把提问式任务显示成「问你…」")

        out, ch = _cmd("删 t1")
        chk(ch and "已删除" in out, "按编号删")
        out, ch = _cmd("删 t99")
        chk(not ch and "没有编号" in out, "删不存在的编号要报错")

        out, ch = _cmd("")
        chk("定时任务" in out, "不带参数 = 看列表")
    finally:
        globals()["_save"] = _real_save

    print("\n全部通过。")
