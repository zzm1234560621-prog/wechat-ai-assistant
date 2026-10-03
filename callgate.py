"""打电话的安全闸门：能力开关 / 免打扰时段 / 频率上限。

**为什么单独一个模块**：打电话是**不可逆、且会响到别人手机**的动作，
比「发消息」恶劣得多——发错了至少对方还能忽略，电话是直接把人叫起来。
所以这个能力必须有三道闸，而且默认全关：

  1. `agent.call_voip`（能力闸）—— **默认关**，严格 `is True` 才算开
     （和 `search.enabled` / `privacy.redact` 同一档：写 `"true"` / `1` 一律当关，
     免得配置里一个手误就把电话打出去）。
  2. 免打扰时段 `agent.call_quiet_hours`（默认 `"23:00-07:00"`）—— 跨零点要判对；
     配成空串 = 不设免打扰（用户自己明确要）。
  3. 频率上限 `agent.call_max_per_day`（默认 3）—— 按**最近 24 小时**算，
     账本落 `data/calls.jsonl`（只记时间/被叫 wxid/显示名，**不记通话内容**）。

**为什么账本要落盘而不是只放内存**：bot 会因为掉登录、hook 掉线而重启，
内存计数一重启就归零 —— 那就等于没有上限（模型可以靠「重启一次」把闸绕过去）。
落盘的代价是「手删文件即可重置」，这可以接受：这是用户自己的机器。

**这里没有任何「自动重试」**：打失败了就如实说失败，绝不自己再拨一次 ——
第一次也许已经接通了，重拨就会让对方接到两通电话。
"""
import json
import os
import time
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# 自测会把这个常量改到临时目录（和 usage.USAGE_PATH 同一姿势），
# 绝不污染用户真实账本。
CALL_LOG_PATH = os.path.join(PROJECT_DIR, "data", "calls.jsonl")

# 最近多少秒内算「一天」。用滑动 24 小时而不是「自然日」：
# 自然日在午夜清零，23:50 打满 3 通、00:10 又能打 3 通 —— 那不是上限。
WINDOW_SEC = 24 * 3600

DEFAULT_CAP = 3
DEFAULT_QUIET = "23:00-07:00"

# 账本最多留多少行（超了就只读尾部）：它只用来数最近 24 小时。
_MAX_LINES = 500


def _agent(cfg):
    return (cfg or {}).get("agent") or {}


def enabled(cfg):
    """能力闸。**严格 is True**：默认关，写歪的值也当关。"""
    return _agent(cfg).get("call_voip") is True


def cap(cfg):
    """每天上限。配成非数字/<=0 时回退默认值 —— 绝不回退成「无上限」。"""
    raw = _agent(cfg).get("call_max_per_day", DEFAULT_CAP)
    if isinstance(raw, bool):
        return DEFAULT_CAP
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CAP
    return n if n > 0 else DEFAULT_CAP


def quiet_windows(cfg):
    """解析免打扰时段，返回 [(start_min, end_min), ...]；没配就用默认。

    写法 `"23:00-07:00"`（跨零点）或 `"12:00-14:00,23:00-07:00"`（多段）。
    **空串 = 不设免打扰**（用户明确要 24 小时都能打，尊重他）。
    解析不了的段**直接丢掉**——但不会因此把整个免打扰变成「关」：
    宁可少禁一段，也不能因为写错一段就变成随便打。
    """
    raw = _agent(cfg).get("call_quiet_hours", DEFAULT_QUIET)
    if raw is None:
        raw = DEFAULT_QUIET
    raw = str(raw).strip()
    if raw == "":
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        a, b = part.split("-", 1)
        try:
            sa = _hhmm(a)
            sb = _hhmm(b)
        except ValueError:
            continue
        out.append((sa, sb))
    return out


def _hhmm(s):
    s = str(s).strip()
    if ":" not in s:
        raise ValueError(s)
    h, m = s.split(":", 1)
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(s)
    return h * 60 + m


def in_quiet(cfg, now=None):
    """现在是否落在免打扰时段里。跨零点的段要判对。

    返回 (是否免打扰, 命中的那一段或 None)。
    """
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    for a, b in quiet_windows(cfg):
        if a == b:
            # 起止相同 = 全天禁（写 "00:00-00:00" 就是这个意思）
            return True, (a, b)
        if a < b:
            if a <= cur < b:
                return True, (a, b)
        else:
            # 跨零点：23:00-07:00 → [23:00, 24:00) ∪ [00:00, 07:00)
            if cur >= a or cur < b:
                return True, (a, b)
    return False, None


def recent_count(now=None, path=None):
    """最近 24 小时内**已经拨出去**的次数（从账本数）。

    账本坏了/读不出来一律当 0，但**不会因此把上限当成没配**——
    读不出来时宁可少判几次，也不让「文件坏了」变成「随便打」。
    """
    now = now or time.time()
    path = path or CALL_LOG_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-_MAX_LINES:]
    except OSError:
        return 0
    n = 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
            ts = float(rec.get("ts") or 0)
        except (ValueError, TypeError):
            continue
        if now - ts < WINDOW_SEC:
            n += 1
    return n


def record(wxid, name="", now=None, path=None):
    """记一笔「已经拨出去了」。**必须真的拨出去了才记** —— 提前记会把额度吃掉。"""
    now = now or time.time()
    path = path or CALL_LOG_PATH
    rec = {"ts": now, "wxid": str(wxid or ""), "name": str(name or "")}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        # 记账失败**不能**把电话这件事变成假成功：调用方已经拨出去了，
        # 只是额度没记上。所以只告警，不抛。
        print(f"[call] ⚠️ 拨号账本写不进去（额度可能算不准）：{e}")


def check(cfg, now=None):
    """三道闸一起判。返回 (能不能打, 拒绝原因或 None)。

    顺序：能力闸 → 免打扰 → 频率。**先能力闸**，因为那是「用户根本没开」，
    报「免打扰」会让他以为功能是开的、只是时间不对。
    """
    if not enabled(cfg):
        return False, ("打电话这个能力**没开启**（config.yaml 的 agent.call_voip "
                       "默认是 false）。要用得由**你自己**把它改成 true。")
    now_dt = now or datetime.now()
    quiet, win = in_quiet(cfg, now_dt)
    if quiet:
        a, b = win
        return False, (f"现在是免打扰时段（{a // 60:02d}:{a % 60:02d}-"
                       f"{b // 60:02d}:{b % 60:02d}），**没有拨出去**。"
                       f"要改时段改 config.yaml 的 agent.call_quiet_hours。")
    c = cap(cfg)
    used = recent_count(now_dt.timestamp() if isinstance(now_dt, datetime) else now_dt)
    if used >= c:
        return False, (f"最近 24 小时已经打了 {used} 通，到上限了"
                       f"（agent.call_max_per_day={c}），**这一通没有拨**。")
    return True, None


def describe(cfg):
    """给 /状态 之类用的单行摘要。"""
    on = "开" if enabled(cfg) else "关"
    q = quiet_windows(cfg)
    qs = "、".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}"
                   for a, b in q) or "无"
    return f"打电话：{on}｜免打扰 {qs}｜上限 {cap(cfg)} 通/24h｜已用 {recent_count()}"
