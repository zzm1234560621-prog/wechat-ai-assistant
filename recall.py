"""撤回原文回显：别人撤回消息时，把**原文**告诉你。

为什么不靠 hook 的「防撤回」字节补丁（`Weixin.dll+0x22D09E7` 那条 `NOP;JMP`）：
2026-10-03 实测，那个补丁打在**收发共用**的撤回处理分支上
（磁盘原始 `0f 84 …` = `je`，内存里是 `90 e9 …` = `nop; jmp`）。代价是
**你自己的撤回也可能被它吃掉**；而且它每次微信启动都会被 DLL 重新打上，
既不可控也不可配。

这里换成项目侧的做法：bot 本来就每 `poll_interval` 秒把新消息看一遍，
那就顺手把**最近见过的消息**留在一个小环形缓冲里；收到「撤回」系统提示时，
从缓冲里把原文捞出来告诉你。**你自己的撤回照常能用。**

三条铁律（和项目其它模块一致）：

  1. **纯内存**：不查库、不落盘、不起线程（hook 不支持并发，见 CLAUDE.md）；
  2. 判据是**结构**（`local_type == 10000` 系统消息），**不是**「文本里有『撤回』」
     —— 否则你正常说一句「他刚撤回了什么」就会被当成系统提示，
     然后我们拿一条不相干的原文去回显，那是**编**；
  3. **捞不到原文就如实说捞不到**，绝不拿别的消息顶上。

⚠️ 已知限制（不许含糊）：判据的第二道是「系统消息的文本里带『撤回』两字」。
   10000 这一档还装着群公告、入群、踢人、拍一拍之类的通知，只按结构判会把它们
   全当成撤回。真机样本目前**没取到**（探针跑的时候 hook 的数据目录是坏的），
   所以这里对「没见过形状的系统消息」会打一次日志，方便一眼核对假设 ——
   见 `note_system()`。
"""
import collections

# local_type 低 32 位的「系统消息」档
RECALL_TYPE = 10000

# 缓冲上限的夹取范围。**宁可少留几天，也不许无界增长** ——
# 这是每条消息的全文，跑一整天不设上限就是内存泄漏。
SEC_MIN, SEC_MAX = 60, 86400
MAX_MIN, MAX_MAX = 20, 2000


def section(cfg):
    """取 `recall` 段。**不是字典就当没有**（写歪了不许炸）。

    项目里 `privacy.redact` 那条规矩同源：用户把整段写成标量（`recall: true`）
    是很常见的笔误，那时候 `dict(标量)` 会抛异常，把主循环带走。
    """
    if not isinstance(cfg, dict):
        return {}
    sec = cfg.get("recall")
    return dict(sec) if isinstance(sec, dict) else {}


def enabled(cfg):
    """默认**开**：它只把原文回显到**你自己的控制会话**，不发给出站的任何人。"""
    return bool(section(cfg).get("enabled", True))


def _opt_int(sec, key, default, lo, hi):
    """读一个整数配置并夹取。**写歪的值退回默认并告警**，不许静默变成别的语义。

    （`int(v or default)` 那种写法会把 0 当"没配"——项目里踩过，见 CLAUDE.md。）
    """
    raw = sec.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        print(f"⚠️ recall.{key} 不是数字（{raw!r}），按默认 {default} 处理")
        return default
    if v < lo or v > hi:
        print(f"⚠️ recall.{key}={v} 超出 [{lo}, {hi}]，夹到 "
              f"{max(lo, min(hi, v))}")
        return max(lo, min(hi, v))
    return v


def buffer_seconds(cfg):
    return _opt_int(section(cfg), "buffer_seconds", 900, SEC_MIN, SEC_MAX)


def buffer_max(cfg):
    return _opt_int(section(cfg), "buffer_max", 300, MAX_MIN, MAX_MAX)


def is_recall(local_type, content):
    """这条消息是不是「撤回」系统提示。**两道判据缺一不可**（见文件头注释）。"""
    try:
        lt = int(local_type or 0)
    except (TypeError, ValueError):
        return False
    if (lt & 0xFFFFFFFF) != RECALL_TYPE:
        return False
    return "撤回" in str(content or "")


_system_seen = collections.deque(maxlen=50)     # 只为日志去重，不参与判定


def note_system(content):
    """遇到一条**没被当成撤回**的系统消息时调用：同一种形状只报一次。

    为什么要有它：判据的第二个条件（系统消息文本里带「撤回」）建立在一个
    还没被真机样本验证过的假设上。把没见过的形状打出来，
    「功能没反应」时才能一眼看出是假设错了、还是根本没收到这类消息。
    """
    body = str(content or "").strip()[:80]
    if body in _system_seen:
        return False
    _system_seen.append(body)
    return True


class Ring:
    """最近见过的消息的小环形缓冲。**纯内存**：不查库、不落盘、不起线程。"""

    def __init__(self, seconds=900, maxlen=300):
        self.seconds = int(seconds)
        self.maxlen = int(maxlen)
        self._q = collections.deque(maxlen=self.maxlen)

    def configure(self, seconds=None, maxlen=None):
        """就地改配置，**不清空已攒的消息**。

        为什么要就地改：`reload_cfg()` 在你每次改配置后都会跑，
        重建 Ring 会把刚攒的上下文全丢掉，于是「改完配置之后那几条撤回
        就捞不到原文了」——这种缺只在改配置之后出现，最难查。
        """
        if seconds is not None:
            self.seconds = int(seconds)
        if maxlen is not None and int(maxlen) != self.maxlen:
            self.maxlen = int(maxlen)
            kept = list(self._q)[-self.maxlen:]
            self._q = collections.deque(kept, maxlen=self.maxlen)

    def __len__(self):
        return len(self._q)

    def _prune(self, now):
        if not self.seconds:
            return
        cut = float(now or 0) - self.seconds
        while self._q and self._q[0][2] < cut:
            self._q.popleft()

    def add(self, talker, content, ts, speaker="", local_type=1):
        """记一条**见过的**消息。内容为空的不记（空消息没有回显价值）。"""
        body = str(content or "")
        if not body.strip():
            return
        self._prune(ts)
        self._q.append((str(talker or ""), body, float(ts or 0.0),
                        str(speaker or ""), local_type))

    def find(self, talker, before_ts, speaker=""):
        """找**同会话里、比这条提示更早的最近一条**。找不到返回 None。

        优先同一发言人的那条（群聊里提示通常就是那个人撤的）；
        没有同一发言人的，就退到该会话最近的一条。
        **不跨会话找** —— 那会把别人的消息当成他的原文，等于编。
        """
        self._prune(before_ts)
        talker = str(talker or "")
        before = float(before_ts or 0.0)
        newest = None
        for it in reversed(self._q):
            if it[0] != talker or it[2] > before:
                continue
            if newest is None:
                newest = it
            if speaker and it[3] == speaker:
                return it
        return newest


def format_echo(who, original, limit=300):
    """回显文案。**捞不到原文时如实说捞不到**，绝不拿别的消息顶上。"""
    name = str(who or "").strip() or "某人"
    body = str(original or "").strip()
    if not body:
        return (f"↩️ {name}撤回了一条消息 —— **原文没留住**"
                f"（这条在我开机以来没经过我这里；可能是 bot 启动前发的，"
                f"或者那会儿没轮询到）")
    if len(body) > limit:
        body = body[:limit] + "…"
    return f"↩️ {name}撤回了一条消息，原文：{body}"


def summary_line(cfg):
    if not enabled(cfg):
        return "撤回回显：关"
    return (f"撤回回显：开（留 {buffer_seconds(cfg)//60} 分钟 / "
            f"{buffer_max(cfg)} 条）")


def status_line(cfg):
    return (f"撤回回显　{'开启' if enabled(cfg) else '已关闭'}"
            f"　· 原文缓冲 {buffer_seconds(cfg)//60} 分钟 / {buffer_max(cfg)} 条"
            f"　（改 config.yaml 的 recall 段）")
